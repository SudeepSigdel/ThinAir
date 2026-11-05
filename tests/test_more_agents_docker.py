"""The real Codex and Gemini CLIs inside Thinair's sandbox (needs Docker + npm registry; no
account is used). Copies of the built-in configs get `-test` ids, so the login volumes created
and deleted here are never a real login."""

import json
from pathlib import Path

import docker
import pytest

from thinair.agents.cli_adapter import AgentAuthError, CLIAdapter
from thinair.agents.config import load_agents
from thinair.project.prepare import DirSource, prepare_workspace
from thinair.sandbox.manager import SandboxManager

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


def copy_cfg(agent_id: str):
    cfg = load_agents()[0][agent_id]
    return cfg.model_copy(update={"id": f"{agent_id}-test"})


@pytest.fixture
async def workspace(mgr, request):
    agent_id, fixture = request.param
    cfg = copy_cfg(agent_id)
    mgr.remove_volume(cfg.auth_volume)
    job = f"ma{agent_id[:2]}"
    prepared = await prepare_workspace(mgr, DirSource(FIXTURES / fixture), job, lambda _: None)
    yield cfg, job, prepared
    mgr.remove_volume(prepared.volume)
    mgr.remove_volume(cfg.auth_volume)


async def common_checks(mgr, adapter, version_cmd: list[str], version: str) -> None:
    res, out = await mgr.run_capture(adapter._spec(version_cmd, timeout_s=120))
    assert res.exit_code == 0 and version in " ".join(out), out
    res, out = await mgr.run_capture(
        adapter._spec(["sh", "-c", "id -u; touch /thinair-auth/probe && echo writable"])
    )
    assert out == ["10001", "writable"], out


@pytest.mark.parametrize("workspace", [("codex", "node_app")], indirect=True)
async def test_real_codex_logged_out_and_flags_accepted(mgr, workspace) -> None:
    cfg, job, prepared = workspace
    adapter = CLIAdapter(cfg, mgr, job, prepared, host_env={})
    with pytest.raises(AgentAuthError) as e:
        await adapter.prepare(lambda _: None)  # `codex login status` exits 1
    assert "thinair auth codex" in e.value.message
    await common_checks(mgr, adapter, ["codex", "--version"], "0.166.0")

    # node:22-slim has no CA bundle of its own; the agent image must bring one.
    res, out = await mgr.run_capture(
        adapter._spec(["sh", "-c", 'grep -c "BEGIN CERTIFICATE" "$SSL_CERT_FILE"'])
    )
    assert res.exit_code == 0 and int(out[0]) > 50, out

    # Our exact command line is accepted: Codex starts a thread and prints JSONL (then fails to
    # reach the network, which is off here: no request leaves the machine).
    res, out = await mgr.run_capture(adapter._spec(cfg.argv("say hi"), network=False, timeout_s=45))
    events = [json.loads(ln) for ln in out if ln.startswith("{")]
    assert events and events[0]["type"] == "thread.started", out[:10]
    assert not [ln for ln in out if "unexpected argument" in ln or "Usage:" in ln], out[:10]


@pytest.mark.parametrize("workspace", [("gemini", "py_fastapi_app")], indirect=True)
async def test_real_gemini_logged_out_maps_to_the_login_hint(mgr, workspace) -> None:
    cfg, job, prepared = workspace
    adapter = CLIAdapter(cfg, mgr, job, prepared, host_env={})
    await adapter.prepare(lambda _: None)  # no check command: the run itself reveals it
    await common_checks(mgr, adapter, ["gemini", "--version"], "0.67.0")
    res, out = await mgr.run_capture(adapter._spec(["node", "--version"]))
    assert out[0].startswith("v22"), out  # Node copied into the Python image

    _ = [ev async for ev in adapter.run("say hi")]
    r = adapter.result()
    assert not r.success
    assert r.error_code == "agent_not_authenticated", (r.error_code, r.logs_tail)
    assert "thinair auth gemini" in r.error_message
    assert r.exit_status == 41, r.logs_tail  # FatalAuthenticationError, not an argument error
