"""Outbound allowlist for agent runs: the proxy (in-process, no Docker), the spec/config plumbing
and when the adapter uses it."""

import asyncio

import pytest
from pydantic import ValidationError

from thinair.agents.config import AgentConfig
from thinair.config import get_settings
from thinair.sandbox.egress_proxy import Proxy, host_allowed, parse_connect
from thinair.sandbox.manager import build_container_args
from thinair.sandbox.spec import SandboxSpec


# ---- allowlist rules ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("api.anthropic.com", True),
        ("API.Anthropic.com.", True),
        ("evil-api.anthropic.com", False),
        ("api.anthropic.com.evil.net", False),
        ("x.claude.ai", True),  # *.claude.ai
        ("claude.ai", False),  # the wildcard does not match the apex
        ("1.2.3.4", False),
        ("[::1]", False),
        ("::1", False),
        ("", False),
    ],
)
def test_host_rules(host: str, ok: bool) -> None:
    assert host_allowed(host, ["api.anthropic.com", "*.claude.ai"]) is ok


def test_parse_connect() -> None:
    assert parse_connect(b"CONNECT api.x.com:443 HTTP/1.1\r\nHost: a\r\n\r\n") == ("api.x.com", 443)
    assert parse_connect(b"GET http://x.com/ HTTP/1.1\r\n\r\n") is None
    assert parse_connect(b"CONNECT x.com HTTP/1.1\r\n\r\n") is None
    assert parse_connect(b"CONNECT x.com:abc HTTP/1.1\r\n\r\n") is None


# ---- the proxy, in-process ---------------------------------------------------------------------
async def echo_server():
    async def handle(r, w):
        while data := await r.read(1024):
            w.write(data.upper())
            await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def proxy_server(allow, upstream_port):
    seen: list[str] = []
    proxy = Proxy(allow, upstream_port, out=seen.append)
    srv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


async def ask(
    port: int, request: bytes
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(request)
    await w.drain()
    status = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    return status, r, w


async def test_allowed_host_is_tunnelled_untouched_and_others_are_refused() -> None:
    upstream, up_port = await echo_server()
    srv, port, seen = await proxy_server(["localhost"], up_port)
    async with upstream, srv:
        status, r, w = await ask(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        assert status.startswith(b"HTTP/1.1 200")
        w.write(b"hello")
        await w.drain()
        assert await asyncio.wait_for(r.readexactly(5), 5) == b"HELLO"  # bytes pass through
        w.close()

        for req, code in [
            (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"403"),
            (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", b"403"),  # IP literal
            (b"CONNECT localhost:22 HTTP/1.1\r\n\r\n", b"403"),  # other port
            (b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n", b"405"),
        ]:
            status, _, w2 = await ask(port, req)
            assert status.split()[1] == code, (req, status)
            w2.close()
    assert seen == [
        "[egress] allowed localhost",
        "[egress] blocked example.com",
        "[egress] blocked 127.0.0.1",
        "[egress] blocked localhost:22",
    ]


async def test_unreachable_allowed_host_is_a_502() -> None:
    srv, port, _ = await proxy_server(["localhost"], 1)  # nothing listens on port 1
    async with srv:
        status, _, w = await ask(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        assert status.startswith(b"HTTP/1.1 502")
        w.close()


# ---- plumbing ----------------------------------------------------------------------------------
def test_spec_joins_only_thinair_networks() -> None:
    spec = SandboxSpec(image="i", command=["true"], job_id="j", network_name="thinair-net-j")
    assert build_container_args(spec)["network_mode"] == "thinair-net-j"
    for bad in ["host", "bridge", "container:abc", "thinair-net-"]:
        with pytest.raises(ValidationError):
            SandboxSpec(image="i", command=["true"], job_id="j", network_name=bad)


def agent(**kw) -> AgentConfig:
    base = {
        "id": "a",
        "name": "A",
        "install": "true",
        "command": ["a", "{task}"],
        "auth": {"login_command": ["a"]},
    }
    return AgentConfig.model_validate({**base, **kw})


def test_egress_config_validation() -> None:
    assert agent(egress=["API.example.com", "*.example.org"]).egress == [
        "api.example.com",
        "*.example.org",
    ]
    assert agent().egress is None
    for bad in [["http://x.com"], ["x"], ["1.2.3.4:443"], ["*"], ["a b.com"]]:
        with pytest.raises(ValidationError):
            agent(egress=bad)


def test_adapter_uses_the_gateway_only_when_declared_and_enabled(monkeypatch) -> None:
    from thinair.agents.cli_adapter import CLIAdapter

    class P:
        volume, commit_sha = "v", None

    def gateway(cfg):
        return CLIAdapter(cfg, object(), "job1", P(), host_env={})._gateway()  # type: ignore[arg-type]

    monkeypatch.setenv("THINAIR_AGENT_EGRESS_EXTRA_HOSTS", " pypi.org, ,registry.npmjs.org")
    get_settings.cache_clear()
    gw = gateway(agent(egress=["api.example.com"]))
    assert gw.allow == ["api.example.com", "pypi.org", "registry.npmjs.org"]
    assert gw.network == "thinair-net-job1"
    assert gw.env["HTTPS_PROXY"] == "http://thinair-egress-job1:3128"
    assert gateway(agent()) is None  # not declared: unrestricted
    assert gateway(agent(egress=["a.com"], network=False)) is None  # no network at all
    monkeypatch.setenv("THINAIR_AGENT_EGRESS", "open")
    get_settings.cache_clear()
    assert gateway(agent(egress=["a.com"])) is None


def test_builtin_claude_is_allowlisted() -> None:
    from thinair.agents.config import load_agents

    agents = load_agents()[0]
    assert agents["claude"].egress == ["api.anthropic.com", "claude.ai", "platform.claude.com"]
    # Not verified to honour HTTPS_PROXY, so they stay unrestricted (documented).
    assert agents["codex"].egress is None and agents["gemini"].egress is None
