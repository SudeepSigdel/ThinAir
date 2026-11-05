"""The outbound allowlist with real containers (needs Docker and github.com). A sandbox on the
job's internal network reaches an allowlisted host only through the proxy, is refused other hosts,
and has no direct route out. Everything is removed afterwards."""

from pathlib import Path

import docker
import pytest

from thinair.agents.cli_adapter import CLIAdapter
from thinair.agents.config import AgentConfig
from thinair.project.detect import PYTHON_IMAGE
from thinair.project.prepare import DirSource, ensure_image, prepare_workspace
from thinair.sandbox import janitor
from thinair.sandbox.egress import EgressGateway
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxSpec

pytestmark = pytest.mark.docker
FIXTURE = Path(__file__).parent / "fixtures" / "node_app"
ALLOWED = "api.github.com"

FETCH = """
import sys, urllib.request
url = sys.argv[1]
direct = len(sys.argv) > 2
opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
try:
    print("status", opener.open(url, timeout=20).status)
except Exception as e:
    print("error", type(e).__name__, str(e)[:120])
"""


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


def nothing_left(mgr, job: str) -> None:
    f = {"label": f"thinair.job={job}"}
    assert not mgr.client.containers.list(all=True, filters=f), "container leaked"
    assert not mgr.client.networks.list(filters=f), "network leaked"


async def test_internal_network_reaches_only_allowlisted_hosts(mgr) -> None:
    await ensure_image(mgr, "python", lambda _: None)
    gw = EgressGateway(mgr, "egtest", [ALLOWED])
    await gw.start()
    try:

        async def fetch(url: str, *extra: str) -> str:
            res, out = await mgr.run_capture(
                SandboxSpec(
                    image=PYTHON_IMAGE,
                    command=["python", "-c", FETCH, url, *extra],
                    job_id="egtest",
                    network_name=gw.network,
                    env=gw.env,
                    timeout_s=60,
                )
            )
            return " ".join(out)

        assert (await fetch(f"https://{ALLOWED}/zen")).startswith("status 200")
        blocked = await fetch("https://example.com/")
        assert blocked.startswith("error") and "403" in blocked, blocked
        direct = await fetch(f"https://{ALLOWED}/zen", "direct")  # bypassing the proxy
        assert direct.startswith("error"), direct
    finally:
        lines = await gw.stop()
    assert f"[egress] allowed {ALLOWED}" in lines and "[egress] blocked example.com" in lines
    nothing_left(mgr, "egtest")


async def test_agent_run_goes_through_the_proxy_and_reports_blocked_hosts(mgr) -> None:
    script = (
        "python3 -c \"import urllib.request as u; u.urlopen('https://example.com', timeout=20)\" "
        "2>/dev/null || echo blocked-as-expected; "
        'printf \'%s\\n\' \'{"type":"result","is_error":false,"result":"ok"}\''
    )
    cfg = AgentConfig.model_validate(
        {
            "id": "egressagent",
            "name": "Egress Agent",
            "install": "true",
            "output": "stream-json",
            "command": ["sh", "-c", script, "x", "{task}"],
            "auth": {"login_command": ["sh"], "env_passthrough": ["FAKE_TOKEN"]},
            "egress": [ALLOWED],
            "limits": {"cpus": 1, "memory": "512m", "pids": 256, "timeout_s": 90},
        }
    )
    prepared = await prepare_workspace(
        mgr,
        DirSource(Path(__file__).parent / "fixtures" / "py_fastapi_app"),
        "egagent",
        lambda _: None,
    )
    try:
        adapter = CLIAdapter(cfg, mgr, "egagent", prepared, host_env={"FAKE_TOKEN": "t" * 12})
        await adapter.prepare(lambda _: None)
        events = [ev async for ev in adapter.run("x")]
    finally:
        mgr.remove_volume(prepared.volume)
        mgr.remove_volume("thinair-auth-egressagent")
    texts = [e.text for e in events]
    assert any("outbound traffic limited to: api.github.com" in t for t in texts), texts
    assert any("blocked example.com (not on the allowlist)" in t for t in texts), texts
    assert "blocked-as-expected" in " ".join(adapter.result().logs_tail)
    assert adapter.result().success
    nothing_left(mgr, "egagent")


async def test_janitor_removes_leftover_networks(mgr) -> None:
    mgr.create_internal_network("thinair-net-egjan", "egjan")
    removed = janitor.sweep(mgr.client, keep_job_ids=frozenset({"other"}))
    assert removed["networks"] >= 1
    nothing_left(mgr, "egjan")
