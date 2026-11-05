"""Per-job outbound allowlist for agent runs.

    agent container --(internal network, no route out)--> egress proxy --(bridge)--> allowed hosts

`EgressGateway.start()` creates an internal Docker network `thinair-net-<job>` and starts the
allowlisting proxy (`egress_proxy.py`, standard library only) on it, also connected to the
default bridge. The agent container then joins only the internal network and gets
`HTTPS_PROXY`, so the proxy is its single way out: it tunnels `CONNECT host:443` for allowlisted
names and refuses everything else (other hosts, IP literals, other ports, plain HTTP). Nothing is
decrypted. Everything exists only during the run; `stop()` removes it and returns which hosts
were allowed or blocked, and the janitor sweeps leftovers after a crash.

Limits, stated plainly: an allowlisted host that accepts uploads can still receive data, and
tools that ignore proxy settings simply get no network.
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from docker.models.containers import Container

from thinair.project.detect import PYTHON_IMAGE
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxError, SandboxSpec

PROXY_PORT = 3128
PROXY_SOURCE = (Path(__file__).with_name("egress_proxy.py")).read_text(encoding="utf-8")
READY = "[egress] ready"


@dataclass
class EgressGateway:
    mgr: SandboxManager
    job_id: str
    allow: list[str]
    _container: Container | None = field(default=None, init=False)
    _network_made: bool = field(default=False, init=False)

    @property
    def network(self) -> str:
        return f"thinair-net-{self.job_id}"

    @property
    def proxy_host(self) -> str:
        return f"thinair-egress-{self.job_id}"

    @property
    def env(self) -> dict[str, str]:
        url = f"http://{self.proxy_host}:{PROXY_PORT}"
        return {
            "HTTPS_PROXY": url,
            "https_proxy": url,
            "HTTP_PROXY": url,
            "http_proxy": url,
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }

    def _start_blocking(self, ready_timeout_s: float) -> None:
        self.mgr.create_internal_network(self.network, self.job_id)
        self._network_made = True
        spec = SandboxSpec(
            image=PYTHON_IMAGE,
            command=["python", "-u", "-c", PROXY_SOURCE, "--port", str(PROXY_PORT), *self.allow],
            job_id=self.job_id,
            network_name=self.network,
            cpus=0.25,
            memory_bytes=64 * 1024**2,
            pids_limit=64,
            timeout_s=24 * 3600,  # lifetime is managed by stop(); unused for services
        )
        self._container = self.mgr.start_service(spec, self.proxy_host, also_on=["bridge"])
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            self._container.reload()
            if self._container.status not in ("created", "running"):
                break
            if READY in self._container.logs().decode("utf-8", "replace"):
                return
            time.sleep(0.2)
        raise SandboxError("The egress proxy for this agent run did not start.")

    async def start(self, ready_timeout_s: float = 20) -> None:
        try:
            await asyncio.to_thread(self._start_blocking, ready_timeout_s)
        except BaseException:
            await self.stop()
            raise

    def _stop_blocking(self) -> list[str]:
        lines: list[str] = []
        if self._container is not None:
            lines = self.mgr.stop_service(self._container)
            self._container = None
        if self._network_made:
            with contextlib.suppress(Exception):
                self.mgr.remove_network(self.network)
            self._network_made = False
        return [ln for ln in lines if ln.startswith("[egress] ") and ln != READY]

    async def stop(self) -> list[str]:
        """Tear everything down. Returns `[egress] allowed/blocked <host>` lines."""
        return await asyncio.shield(asyncio.to_thread(self._stop_blocking))
