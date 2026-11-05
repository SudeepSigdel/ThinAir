"""CLIAdapter end to end with real containers and a fake agent (a shell script that speaks
Claude Code's stream-json). Everything except the real model is exercised: image build, login
volume, login check, token passthrough + masking, live events, failure mapping and the diff."""

from pathlib import Path

import docker
import pytest

from thinair.agents.cli_adapter import AgentAuthError, CLIAdapter
from thinair.agents.config import AgentConfig
from thinair.project.prepare import DirSource, prepare_workspace
from thinair.sandbox.manager import SandboxManager

FIXTURE = Path(__file__).parent / "fixtures" / "node_app"
pytestmark = pytest.mark.docker

SCRIPT = r"""
printf '%s\n' '{"type":"system","subtype":"init","model":"fake-1"}'
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"Working on: %s"},{"type":"tool_use","name":"Bash","input":{"command":"npm test"}}]}}\n' "$1"
echo "my token is ${FAKE_TOKEN:-none}"
echo "// edited by the agent" >> sum.js
echo "new file" > added.txt
mkdir -p node_modules/junk && echo x > node_modules/junk/x.js
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"Edited sum.js and added a file.","total_cost_usd":0.01,"num_turns":2}'
"""


def make_agent(script: str = SCRIPT) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "id": "fakeagent",
            "name": "Fake Agent",
            "install": "true",
            "output": "stream-json",
            "command": ["sh", "-c", script, "fake", "{task}"],
            "auth": {
                "login_command": ["sh"],
                "check_command": ["sh", "-c", "test -f /thinair-auth/logged-in"],
                "env_passthrough": ["FAKE_TOKEN"],
                "hint": "Fake Agent is not logged in. Run `thinair auth fakeagent`.",
            },
            "auth_error_patterns": ["not logged in"],
            "limits": {"cpus": 1, "memory": "512m", "pids": 256, "timeout_s": 60},
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
    mgr.remove_volume("thinair-auth-fakeagent")  # start logged out
    prepared = await prepare_workspace(mgr, DirSource(FIXTURE), "agtest", lambda _: None)
    yield prepared
    mgr.remove_volume(prepared.volume)
    mgr.remove_volume("thinair-auth-fakeagent")
    label = {"label": "thinair.managed=true"}
    assert not mgr.client.containers.list(all=True, filters=label), "container leaked"


async def log_in(mgr, adapter) -> None:
    """Simulates `thinair auth`: something the agent writes into its own login volume."""
    res, out = await mgr.run_capture(adapter._spec(["sh", "-c", "touch /thinair-auth/logged-in"]))
    assert res.exit_code == 0, out  # the sandbox user can write to the login volume


async def test_logged_out_agent_gets_a_clear_message(mgr, workspace) -> None:
    adapter = CLIAdapter(make_agent(), mgr, "agtest", workspace, host_env={})
    with pytest.raises(AgentAuthError) as e:
        await adapter.prepare(lambda _: None)
    assert "thinair auth fakeagent" in e.value.message


async def test_full_run_streams_events_and_produces_a_clean_diff(mgr, workspace) -> None:
    adapter = CLIAdapter(make_agent(), mgr, "agtest", workspace, host_env={})
    with pytest.raises(AgentAuthError):
        await adapter.prepare(lambda _: None)
    await log_in(mgr, adapter)
    await adapter.prepare(lambda _: None)  # now passes the login check

    events = [ev async for ev in adapter.run("add a greeting")]
    kinds = [(e.kind, e.text) for e in events]
    assert ("text", "Working on: add a greeting") in kinds
    assert ("tool", "Bash: npm test") in kinds
    assert any(k == "result" for k, _ in kinds)

    r = adapter.result()
    assert r.success and r.exit_status == 0 and r.error_code is None
    assert r.summary == "Edited sum.js and added a file." and r.cost_usd == 0.01
    assert "+// edited by the agent" in r.diff and "added.txt" in r.diff
    assert "node_modules" not in r.diff  # install/dependency folders are never part of the diff
    assert r.diff_stat == {"files": 2, "insertions": 2, "deletions": 0}


async def test_passed_through_token_skips_the_check_and_is_masked(mgr, workspace) -> None:
    secret = "s3cret-token-value-123"
    adapter = CLIAdapter(make_agent(), mgr, "agtest", workspace, host_env={"FAKE_TOKEN": secret})
    await adapter.prepare(lambda _: None)  # no login file, but a token was provided
    events = [ev async for ev in adapter.run("x")]
    text = " ".join(e.text for e in events) + " ".join(adapter.result().logs_tail)
    assert "my token is ***" in text and secret not in text


@pytest.mark.parametrize(
    ("script", "code", "fragment"),
    [
        (
            "echo 'Error: not logged in'; exit 1",
            "agent_not_authenticated",
            "thinair auth fakeagent",
        ),
        ("echo 'something broke'; exit 3", "agent_failed", "exit code 3"),
        (
            """printf '%s\\n' '{"type":"result","is_error":true,"result":"Could not fix it."}'""",
            "agent_failed",
            "Could not fix it",
        ),
    ],
)
async def test_failures_are_mapped_to_clear_errors(mgr, workspace, script, code, fragment) -> None:
    cfg = make_agent(script)
    adapter = CLIAdapter(cfg, mgr, "agtest", workspace, host_env={"FAKE_TOKEN": "t" * 10})
    await adapter.prepare(lambda _: None)
    _ = [ev async for ev in adapter.run("x")]
    r = adapter.result()
    assert not r.success and r.error_code == code and fragment in r.error_message
