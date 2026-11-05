"""Allowlisting HTTPS proxy for agent runs. Standard library only.

Thinair sends this file's source to `python -u -c` inside a short-lived sandbox container (see
`egress.py`), so it must stay self-contained. It accepts only `CONNECT <host>:443` for host names
on the allowlist and tunnels the bytes untouched (no TLS interception, nothing decrypted). IP
literals, other ports and plain HTTP requests are refused. Each first use of a host is printed as
`[egress] allowed <host>` / `[egress] blocked <host>` for the job log.

Usage: python egress_proxy.py [--port 3128] [--upstream-port 443] host [*.domain ...]
"""

import asyncio
import ipaddress
import sys

READY = "[egress] ready"


def host_allowed(host: str, allow: list[str]) -> bool:
    """Exact names, or `*.example.com` for any subdomain (not the apex). Never IP literals."""
    host = host.strip().lower().rstrip(".")
    if not host or host.startswith("[") or ":" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    for rule in allow:
        rule = rule.strip().lower().rstrip(".")
        if rule.startswith("*."):
            if host.endswith(rule[1:]) and host != rule[2:]:
                return True
        elif host == rule:
            return True
    return False


def parse_connect(head: bytes) -> tuple[str, int] | None:
    """`CONNECT host:port HTTP/1.1` -> (host, port), anything else -> None."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT" or not parts[2].startswith("HTTP/"):
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not port.isdigit():
        return None
    return host, int(port)


class Proxy:
    def __init__(self, allow: list[str], upstream_port: int = 443, out=print) -> None:
        self.allow = allow
        self.upstream_port = upstream_port
        self.out = out
        self.seen: set[tuple[str, bool]] = set()

    def _note(self, host: str, ok: bool) -> None:
        if (host, ok) not in self.seen:
            self.seen.add((host, ok))
            self.out(f"[egress] {'allowed' if ok else 'blocked'} {host}")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
            writer.close()
            return
        target = parse_connect(head)
        if target is None:
            await self._reply(writer, b"405 Method Not Allowed")
            return
        host, port = target
        ok = port == 443 and host_allowed(host, self.allow)
        self._note(host if port == 443 else f"{host}:{port}", ok)
        if not ok:
            await self._reply(writer, b"403 Forbidden")
            return
        try:
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(host, self.upstream_port), 15
            )
        except (OSError, TimeoutError):
            await self._reply(writer, b"502 Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(self._pipe(reader, up_w), self._pipe(up_r, writer))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: bytes) -> None:
        try:
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except OSError:
            pass
        writer.close()

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except OSError:
            pass
        finally:
            try:
                dst.close()
            except OSError:
                pass


async def serve(allow: list[str], port: int = 3128, upstream_port: int = 443) -> None:
    proxy = Proxy(allow, upstream_port, out=lambda s: print(s, flush=True))
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", port)  # noqa: S104 (in-container)
    print(READY, flush=True)
    async with server:
        await server.serve_forever()


def main(argv: list[str]) -> None:
    port, upstream, allow = 3128, 443, []
    it = iter(argv)
    for arg in it:
        if arg == "--port":
            port = int(next(it))
        elif arg == "--upstream-port":
            upstream = int(next(it))
        else:
            allow.append(arg)
    asyncio.run(serve(allow, port, upstream))


if __name__ == "__main__":
    main(sys.argv[1:])
