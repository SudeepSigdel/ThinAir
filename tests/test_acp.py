"""ACP support without Docker: the in-sandbox driver (run with the host's Node.js against a
scripted fake ACP agent), the `acp` stream parser, and the agent config rules."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from thinair.agents.config import (
    ACP_DRIVER,
    AgentConfig,
    AgentConfigError,
    load_agents,
    parse_agent,
)
from thinair.agents.stream import StreamParser
from thinair.users import auth_volume_name

FAKE = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def drive(tmp_path: Path, mode: str, task: str = "fix the -x test") -> tuple[int, list, list]:
    """Run the driver against the fake agent; returns (exit code, driver events, agent log)."""
    log = tmp_path / f"{mode}.jsonl"
    proc = subprocess.run(  # noqa: S603
        [NODE, "-e", ACP_DRIVER, "--", task, sys.executable, str(FAKE), mode, str(log)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    events = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    received = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    return proc.returncode, events, received


@needs_node
def test_driver_speaks_acp_v1_and_reports_a_successful_turn(tmp_path) -> None:
    code, events, received = drive(tmp_path, "ok")
    assert code == 0, events
    init, new, prompt = received[0], received[1], received[2]
    assert init["method"] == "initialize" and init["params"]["protocolVersion"] == 1
    # Nothing beyond the baseline is offered: the agent edits /workspace itself.
    caps = init["params"]["clientCapabilities"]
    assert caps == {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
    assert new["method"] == "session/new" and new["params"]["mcpServers"] == []
    assert Path(new["params"]["cwd"]).is_absolute()
    assert prompt["params"]["prompt"] == [{"type": "text", "text": "fix the -x test"}]
    assert (tmp_path / "acp_was_here.txt").read_text().strip() == "fix the -x test"

    replies = next(r for r in received if "perm_reply" in r)
    assert replies["perm_reply"]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "yes"}
    }
    assert replies["fs_reply"]["error"]["code"] == -32601  # not advertised -> method not found

    kinds = [e["acp"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "result"
    assert events[-1]["stopReason"] == "end_turn"
    assert "agent_thought_chunk" not in json.dumps(events)  # thoughts are dropped


@needs_node
@pytest.mark.parametrize(
    ("mode", "exit_code", "last"),
    [
        ("auth", 3, "auth_required"),
        ("refusal", 1, "result"),
        ("crash", 2, "error"),
        ("version2", 2, "error"),
    ],
)
def test_driver_failures(tmp_path, mode, exit_code, last) -> None:
    code, events, _ = drive(tmp_path, mode)
    assert code == exit_code and events[-1]["acp"] == last, events


@needs_node
def test_driver_never_approves_without_an_allow_option(tmp_path) -> None:
    code, events, received = drive(tmp_path, "reject_only")
    assert code == 0
    assert next(r for r in received if "perm_reply" in r)["perm_reply"]["result"] == {
        "outcome": {"outcome": "cancelled"}
    }
    perm = next(e for e in events if e.get("acp") == "permission")
    assert perm == {
        "acp": "permission",
        "title": "Run npm test",
        "allowed": False,
        "option": None,
        "by": "auto",
    }


@needs_node
def test_driver_output_parses_into_phone_events(tmp_path) -> None:
    log = tmp_path / "x.jsonl"
    proc = subprocess.run(  # noqa: S603
        [NODE, "-e", ACP_DRIVER, "--", "t", sys.executable, str(FAKE), "ok", str(log)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    p = StreamParser("acp")
    events = [e for ln in proc.stdout.splitlines() for e in p.feed(ln)] + p.finish()
    assert [(e.kind, e.text) for e in events if e.kind in ("text", "tool")] == [
        ("text", "Looking at it."),
        ("tool", "execute: Run npm test"),
        ("text", "Fixed the test."),
    ]
    result = events[-1]
    assert result.kind == "result" and result.text == "Fixed the test."
    assert result.data == {"is_error": False, "stop_reason": "end_turn", "total_cost_usd": 0.25}


def test_parser_edge_cases() -> None:
    p = StreamParser("acp")
    assert p.feed("plain stderr line")[0].kind == "log"
    assert p.feed('{"type": "not ours"}')[0].kind == "log"
    assert p.feed('{"acp": "auth_required", "message": "Authentication required"}')[0].kind == (
        "auth_error"
    )
    p.feed('{"acp": "text", "text": "partial", "id": null}')
    assert [e.text for e in p.finish()] == ["partial"]  # buffered text survives an abrupt end
    stopped = p.feed('{"acp": "result", "stopReason": "max_tokens"}')[-1]
    assert stopped.data["is_error"] and "max_tokens" in stopped.text
    err = p.feed('{"acp": "error", "message": "the agent exited unexpectedly (code 5)"}')
    assert err[-1].kind == "result" and err[-1].data["is_error"]


# ---- config ----------------------------------------------------------------------------------
BASE = """
id: myacp
name: My ACP agent
install: "true"
needs_node: true
protocol: acp
command: [my-agent, --acp]
auth: {login_command: [my-agent]}
"""


def test_acp_config_rules() -> None:
    cfg = parse_agent(BASE)
    assert cfg.output == "acp"
    argv = cfg.argv("-rf everything")
    assert argv[:2] == ["node", "-e"] and argv[2] == ACP_DRIVER
    assert argv[3:] == ["--", "-rf everything", "my-agent", "--acp"]  # task stays one argument
    bad = {
        "command: [my-agent, '{task}']": "no",
        "needs_node: true": "needs_node: false",
        "output: acp": "output: stream-json",
    }
    for good, wrong in bad.items():
        text = BASE.replace(good, wrong) if good in BASE else BASE + wrong + "\n"
        with pytest.raises(AgentConfigError):
            parse_agent(text)
    cli = BASE.replace("protocol: acp", "protocol: cli").replace("--acp]", "'{task}']")
    assert parse_agent(cli).output == "text"
    with pytest.raises(AgentConfigError):
        parse_agent(cli + "output: acp\n")
    with pytest.raises(AgentConfigError):
        parse_agent(
            BASE.replace(
                "{login_command: [my-agent]}", "{login_command: [a], shares_login_with: Bad!}"
            )
        )


def test_builtin_acp_agents_share_their_cli_logins() -> None:
    agents, errors = load_agents(None)
    assert errors == []
    for acp_id, base in [("claude-acp", "claude"), ("gemini-acp", "gemini")]:
        cfg = agents[acp_id]
        assert cfg.protocol == "acp" and cfg.output == "acp"
        assert cfg.login_id == base and cfg.auth_volume == f"thinair-auth-{base}"
        assert cfg.auth.config_dir == agents[base].auth.config_dir
    assert agents["claude-acp"].egress == agents["claude"].egress
    assert auth_volume_name(agents["claude-acp"].login_id, None) == "thinair-auth-claude"
    assert isinstance(agents["claude"], AgentConfig) and agents["claude"].login_id == "claude"


# ---- conversations: replies and approvals over the driver's stdin ---------------------------
def converse(tmp_path: Path, answer, *, approval: str = "ask", idle: str = "20"):
    """Run the driver in conversation mode; `answer(event)` returns a command (or None) for
    each event the driver prints. Returns (exit code, events, agent log)."""
    import os

    log = tmp_path / "chat.jsonl"
    env = {
        **os.environ,
        "THINAIR_ACP_INTERACTIVE": "1",
        "THINAIR_ACP_APPROVAL": approval,
        "THINAIR_ACP_IDLE_S": idle,
    }
    proc = subprocess.Popen(  # noqa: S603
        [NODE, "-e", ACP_DRIVER, "--", "first", sys.executable, str(FAKE), "chat", str(log)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    events = []
    assert proc.stdout is not None and proc.stdin is not None
    for line in proc.stdout:
        ev = json.loads(line)
        events.append(ev)
        cmd = answer(ev)
        if cmd is not None:
            proc.stdin.write(json.dumps(cmd) + "\n")
            proc.stdin.flush()
    code = proc.wait(timeout=60)
    received = [json.loads(ln) for ln in log.read_text().splitlines()]
    return code, events, received


@needs_node
def test_conversation_asks_for_approval_and_takes_more_messages(tmp_path) -> None:
    waits = []

    def answer(ev):
        if ev["acp"] == "permission_request":
            # approve the first request, deny the second
            option = "yes" if ev["request"] == 1 else None
            return {"type": "permission", "request": ev["request"], "option": option}
        if ev["acp"] == "waiting":
            waits.append(ev)
            return (
                {"type": "message", "text": "also README"} if len(waits) == 1 else {"type": "end"}
            )
        return None

    code, events, received = converse(tmp_path, answer)
    assert code == 0, events
    reqs = [e for e in events if e["acp"] == "permission_request"]
    assert [r["request"] for r in reqs] == [1, 2]
    assert reqs[0]["options"] == [
        {"id": "no", "name": "Reject", "kind": "reject_once"},
        {"id": "yes", "name": "Allow once", "kind": "allow_once"},
    ]
    first = next(r for r in received if "perm_reply" in r and "turn" not in r)
    assert first["perm_reply"]["result"] == {"outcome": {"outcome": "selected", "optionId": "yes"}}
    second = next(r for r in received if r.get("turn") == 2)
    assert second["text"] == "also README"
    assert second["perm_reply"]["result"] == {"outcome": {"outcome": "cancelled"}}
    assert {"acp": "user", "text": "also README"} in events
    decided = [e for e in events if e["acp"] == "permission"]
    assert [(d["allowed"], d["by"]) for d in decided] == [(True, "you"), (False, "you")]
    assert events[-1] == {"acp": "ended", "reason": "you ended the conversation"}

    p = StreamParser("acp")
    parsed = [e for ev in events for e in p.feed(json.dumps(ev))] + p.finish()
    kinds = [e.kind for e in parsed]
    assert kinds.count("permission_request") == 2 and kinds.count("waiting") == 2
    assert [e.text for e in parsed if e.kind == "permission"] == [
        "Run npm test: Allow once",
        "Edit also README: denied",
    ]
    assert parsed[[e.kind for e in parsed].index("permission_request")].data["request"] == 1
    assert any(e.kind == "user" and e.text == "also README" for e in parsed)


@needs_node
def test_conversation_ends_by_itself_when_nobody_answers(tmp_path) -> None:
    code, events, _ = converse(tmp_path, lambda ev: None, idle="1")
    assert code == 0, events
    decided = next(e for e in events if e["acp"] == "permission")
    assert decided["by"] == "timeout" and decided["allowed"] is False  # unanswered = not allowed
    assert events[-1] == {"acp": "ended", "reason": "no reply in time"}


@needs_node
def test_conversation_with_auto_approval_and_a_queued_message(tmp_path) -> None:
    sent = []

    def answer(ev):
        if ev["acp"] == "started" and not sent:  # sent while the first turn is still running
            sent.append(1)
            return {"type": "message", "text": "queued"}
        if ev["acp"] == "waiting" and len(sent) == 1:
            sent.append(2)
            return None  # the queued message is picked up without waiting
        if ev["acp"] == "waiting":
            return {"type": "end"}
        return None

    code, events, received = converse(tmp_path, answer, approval="auto")
    assert code == 0, events
    assert not [e for e in events if e["acp"] == "permission_request"]  # never asked
    assert next(r for r in received if r.get("turn") == 2)["text"] == "queued"
