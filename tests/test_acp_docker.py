"""An ACP agent end to end with real containers: the scripted fake ACP agent (Python) driven by
Thinair's Node driver inside the agent sandbox, through CLIAdapter. Covers image build (Node
copied into a Python project image), live events, the result, the diff and auth failures.
Real ACP CLIs (claude-agent-acp, gemini --acp) were checked by hand, see docs/agents.md."""

from pathlib import Path

import docker
import pytest

from thinair.agents.cli_adapter import CLIAdapter
from thinair.agents.config import AgentConfig
from thinair.project.prepare import DirSource, prepare_workspace
from thinair.sandbox.manager import SandboxManager

FIXTURE = Path(__file__).parent / "fixtures" / "py_fastapi_app"
FAKE = (Path(__file__).parent / "fixtures" / "fake_acp_agent.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.docker


def make_agent(mode: str) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "id": "fakeacp",
            "name": "Fake ACP",
            "install": "true",
            "needs_node": True,
            "protocol": "acp",
            "command": ["python3", "-c", FAKE, mode],
            "auth": {"login_command": ["sh"]},
            "limits": {"cpus": 1, "memory": "512m", "pids": 256, "timeout_s": 90},
        }
    )


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


@pytest.fixture
async def workspace(mgr):
    prepared = await prepare_workspace(mgr, DirSource(FIXTURE), "acptest", lambda _: None)
    yield prepared
    mgr.remove_volume(prepared.volume)
    mgr.remove_volume("thinair-auth-fakeacp")
    label = {"label": "thinair.managed=true"}
    assert not mgr.client.containers.list(all=True, filters=label), "container leaked"


async def test_acp_agent_runs_end_to_end(mgr, workspace) -> None:
    adapter = CLIAdapter(make_agent("ok"), mgr, "acptest", workspace, host_env={})
    await adapter.prepare(lambda _: None)
    events = [ev async for ev in adapter.run("--add a greeting file")]
    shown = [(e.kind, e.text) for e in events if e.kind in ("text", "tool")]
    assert shown == [
        ("text", "Looking at it."),
        ("tool", "execute: Run npm test"),
        ("text", "Fixed the test."),
    ]
    r = adapter.result()
    assert r.success and r.exit_status == 0, (r.error_message, r.logs_tail)
    assert r.summary == "Fixed the test." and r.cost_usd == 0.25
    assert "+--add a greeting file" in r.diff and "acp_was_here.txt" in r.diff


@pytest.mark.parametrize(
    ("mode", "code"),
    [("auth", "agent_not_authenticated"), ("refusal", "agent_failed"), ("crash", "agent_failed")],
)
async def test_acp_failures_are_mapped(mgr, workspace, mode, code) -> None:
    adapter = CLIAdapter(make_agent(mode), mgr, "acptest", workspace, host_env={})
    await adapter.prepare(lambda _: None)
    _ = [ev async for ev in adapter.run("t")]
    r = adapter.result()
    assert not r.success and r.error_code == code, (r.error_message, r.logs_tail)


async def test_conversation_through_the_sandbox_stdin(mgr, workspace) -> None:
    """Replies and approvals reach the agent inside the container (Docker attach, stdin only),
    and the run ends with a normal result + diff when the conversation is finished."""
    adapter = CLIAdapter(make_agent("chat"), mgr, "acptest", workspace, host_env={})
    await adapter.prepare(lambda _: None)
    assert await adapter.send({"type": "end"}) is False  # not running yet
    seen = []
    async for ev in adapter.run("first", interactive=True, ask=True):
        seen.append((ev.kind, ev.text))
        if ev.kind == "permission_request":
            option = "yes" if ev.data["request"] == 1 else None
            assert await adapter.send(
                {"type": "permission", "request": ev.data["request"], "option": option}
            )
        elif ev.kind == "waiting":
            waits = sum(1 for k, _ in seen if k == "waiting")
            msg = {"type": "message", "text": "and more"} if waits == 1 else {"type": "end"}
            assert await adapter.send(msg)
    assert ("permission", "Run npm test: Allow once") in seen
    assert ("user", "and more") in seen
    assert ("permission", "Edit and more: denied") in seen
    assert ("text", "You said: and more") in seen
    r = adapter.result()
    assert r.success and r.exit_status == 0, (r.error_message, r.logs_tail)
    assert r.summary == "You said: and more"
    assert "acp_was_here.txt" in r.diff
    assert await adapter.send({"type": "end"}) is False  # finished: nothing to send to
