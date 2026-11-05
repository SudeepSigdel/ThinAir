"""The real Claude Code CLI inside Thinair's sandbox (needs Docker + npm registry; no account).

Uses a copy of the built-in config with a different id, so the login volume it creates and
deletes is `thinair-auth-claude-test`, never a real `thinair-auth-claude` login.
"""

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


@pytest.fixture
def claude_test_cfg():
    cfg = load_agents()[0]["claude"]
    return cfg.model_copy(update={"id": "claude-test"})


@pytest.mark.parametrize("fixture", ["node_app", "py_fastapi_app"])
async def test_real_claude_code_runs_as_the_sandbox_user_and_reports_logged_out(
    mgr, claude_test_cfg, fixture
) -> None:
    mgr.remove_volume(claude_test_cfg.auth_volume)
    prepared = await prepare_workspace(
        mgr, DirSource(FIXTURES / fixture), f"cc{fixture[:3]}", lambda _: None
    )
    try:
        adapter = CLIAdapter(claude_test_cfg, mgr, f"cc{fixture[:3]}", prepared, host_env={})
        with pytest.raises(AgentAuthError) as e:
            await adapter.prepare(lambda _: None)
        assert "thinair auth claude" in e.value.message  # the friendly message, not a stack trace

        res, out = await mgr.run_capture(adapter._spec(["claude", "--version"], timeout_s=120))
        assert res.exit_code == 0 and "2.1.311" in " ".join(out), out
        res, out = await mgr.run_capture(
            adapter._spec(["sh", "-c", "id -u; node --version"], timeout_s=60)
        )
        assert out[0] == "10001" and out[1].startswith("v22"), out
        # the login volume is writable by the agent (so `/login` can save credentials)
        res, out = await mgr.run_capture(
            adapter._spec(["sh", "-c", "touch /thinair-auth/probe && echo ok"])
        )
        assert out == ["ok"], out
    finally:
        mgr.remove_volume(prepared.volume)
        mgr.remove_volume(claude_test_cfg.auth_volume)


async def test_real_claude_code_goes_through_the_egress_allowlist(mgr, claude_test_cfg) -> None:
    """With an obviously fake API key (no account): Claude Code must reach api.anthropic.com
    through the per-run proxy, and the rejected key must map to the login hint."""
    mgr.remove_volume(claude_test_cfg.auth_volume)
    prepared = await prepare_workspace(
        mgr, DirSource(FIXTURES / "py_fastapi_app"), "ccegress", lambda _: None
    )
    fake = "sk-ant-api03-thinair-test-not-a-real-key"
    try:
        adapter = CLIAdapter(
            claude_test_cfg, mgr, "ccegress", prepared, host_env={"ANTHROPIC_API_KEY": fake}
        )
        await adapter.prepare(lambda _: None)  # a passed-through key skips the login check
        events = [ev async for ev in adapter.run("say hi")]
    finally:
        mgr.remove_volume(prepared.volume)
        mgr.remove_volume(claude_test_cfg.auth_volume)
    r = adapter.result()
    logs = " ".join(r.logs_tail) + " ".join(e.text for e in events)
    assert "outbound traffic limited to: api.anthropic.com" in logs
    assert "[egress] allowed api.anthropic.com" in logs, r.logs_tail
    assert not r.success and fake not in logs
    assert r.error_code == "agent_not_authenticated", (r.error_code, r.error_message, r.logs_tail)
