"""Codex and Gemini output parsing plus their built-in configs. Event shapes follow the CLIs' own
schemas (Codex: codex-rs/exec/src/exec_events.rs @ rust-v0.166.0; Gemini:
packages/core/src/output/types.ts @ v0.67.0)."""

import json

from thinair.agents.cli_adapter import agent_dockerfile
from thinair.agents.config import load_agents
from thinair.agents.stream import StreamParser


def j(obj) -> str:
    return json.dumps(obj)


def feed(parser: StreamParser, *lines: str):
    out = []
    for ln in lines:
        out += parser.feed(ln)
    return out + parser.finish()


# ---- Codex -----------------------------------------------------------------------------------
def test_codex_run_becomes_phone_events_and_a_summary() -> None:
    evs = feed(
        StreamParser("codex-json"),
        j({"type": "thread.started", "thread_id": "t1"}),
        j({"type": "turn.started"}),
        j(
            {
                "type": "item.started",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "bash -lc 'npm test'",
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            }
        ),
        j(
            {
                "type": "item.completed",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "bash -lc 'npm test'",
                    "aggregated_output": "1 failing",
                    "exit_code": 1,
                    "status": "failed",
                },
            }
        ),
        j(
            {
                "type": "item.completed",
                "item": {"id": "i2", "type": "reasoning", "text": "thinking..."},
            }
        ),
        j(
            {
                "type": "item.completed",
                "item": {
                    "id": "i3",
                    "type": "file_change",
                    "changes": [{"path": "sum.js", "kind": "update"}],
                    "status": "completed",
                },
            }
        ),
        j(
            {
                "type": "item.completed",
                "item": {"id": "i4", "type": "agent_message", "text": "Fixed add(); tests pass."},
            }
        ),
        j(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 0,
                },
            }
        ),
    )
    kinds = [(e.kind, e.text) for e in evs]
    assert kinds == [
        ("log", "[agent] session started"),
        ("tool", "Shell: bash -lc 'npm test'"),
        ("log", "[agent] command exited with 1"),
        ("tool", "Edit: sum.js"),
        ("text", "Fixed add(); tests pass."),
        ("result", "Fixed add(); tests pass."),
    ]
    assert evs[-1].data["is_error"] is False and evs[-1].data["usage"]["output_tokens"] == 5


def test_codex_failure_and_errors() -> None:
    evs = feed(
        StreamParser("codex-json"),
        j({"type": "error", "message": "Reconnecting... 2/5"}),
        j(
            {
                "type": "item.completed",
                "item": {"id": "e", "type": "error", "message": "falling back"},
            }
        ),
        j({"type": "turn.failed", "error": {"message": "usage limit reached"}}),
        "2026-11-05T18:58:12Z ERROR codex_api: not json",
    )
    assert [e.kind for e in evs] == ["log", "log", "result", "log"]
    assert evs[2].text == "usage limit reached" and evs[2].data["is_error"] is True


def test_codex_ignores_garbage_shapes() -> None:
    p = StreamParser("codex-json")
    for raw in ['{"type":"item.completed","item":"x"}', "[1,2]", '{"type":"todo"}', ""]:
        assert all(e.kind == "log" for e in p.feed(raw))


# ---- Gemini ----------------------------------------------------------------------------------
def test_gemini_joins_delta_chunks_and_summarises() -> None:
    ts = "2026-11-06T00:00:00Z"
    evs = feed(
        StreamParser("gemini-stream-json"),
        j({"type": "init", "timestamp": ts, "session_id": "s", "model": "gemini-3-pro"}),
        j({"type": "message", "timestamp": ts, "role": "user", "content": "fix it"}),
        j(
            {
                "type": "message",
                "timestamp": ts,
                "role": "assistant",
                "content": "Looking ",
                "delta": True,
            }
        ),
        j(
            {
                "type": "message",
                "timestamp": ts,
                "role": "assistant",
                "content": "at the test.",
                "delta": True,
            }
        ),
        j(
            {
                "type": "tool_use",
                "timestamp": ts,
                "tool_name": "run_shell_command",
                "tool_id": "1",
                "parameters": {"command": "npm test"},
            }
        ),
        j(
            {
                "type": "tool_result",
                "timestamp": ts,
                "tool_id": "1",
                "status": "error",
                "error": {"type": "x", "message": "exit 1"},
            }
        ),
        j(
            {
                "type": "message",
                "timestamp": ts,
                "role": "assistant",
                "content": "Fixed it.",
                "delta": True,
            }
        ),
        j({"type": "result", "timestamp": ts, "status": "success", "stats": {"total_tokens": 9}}),
    )
    assert [(e.kind, e.text) for e in evs] == [
        ("log", "[agent] session started (model gemini-3-pro)"),
        ("text", "Looking at the test."),
        ("tool", "run_shell_command: npm test"),
        ("log", "[agent] tool failed: exit 1"),
        ("text", "Fixed it."),
        ("result", "Fixed it."),
    ]
    assert evs[-1].data == {"is_error": False, "stats": {"total_tokens": 9}}


def test_gemini_error_result_and_abrupt_end() -> None:
    p = StreamParser("gemini-stream-json")
    evs = feed(
        p,
        j({"type": "error", "severity": "warning", "message": "slow"}),
        j({"type": "result", "status": "error", "error": {"type": "Fatal", "message": "quota"}}),
    )
    assert evs[0].text == "[agent] warning: slow"
    assert evs[1].kind == "result" and evs[1].text == "quota" and evs[1].data["is_error"]
    # Output that ends mid-message still shows the text (flushed by finish()).
    p2 = StreamParser("gemini-stream-json")
    p2.feed(j({"type": "message", "role": "assistant", "content": "partial", "delta": True}))
    assert [(e.kind, e.text) for e in p2.finish()] == [("text", "partial")]
    # Plain lines (e.g. "Please set an Auth method ...") are logs, after any buffered text.
    assert p2.feed("Please set an Auth method")[0].kind == "log"


# ---- built-in configs ------------------------------------------------------------------------
def test_builtin_codex_and_gemini_configs() -> None:
    agents, errors = load_agents()
    assert errors == []
    codex, gemini = agents["codex"], agents["gemini"]

    assert codex.argv("-x") == [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "--",
        " -x",
    ]
    assert codex.env["CODEX_HOME"] == codex.auth.config_dir == "/thinair-auth"
    assert codex.auth.check_command == ["codex", "login", "status"]
    assert codex.output == "codex-json" and codex.needs_ca_certs and codex.needs_node

    assert gemini.argv("fix")[-2:] == ["--prompt", "fix"]
    assert {"--skip-trust", "yolo", "stream-json"} <= set(gemini.command)
    assert "--sandbox" not in gemini.command and "-s" not in gemini.command
    assert gemini.env["GEMINI_CLI_HOME"] == "/thinair-auth"
    assert gemini.auth.check_command is None and gemini.output == "gemini-stream-json"
    for cfg in (codex, gemini):
        assert f"@{cfg.version}" in cfg.install  # pinned


def test_ca_bundle_is_copied_only_when_asked() -> None:
    agents = load_agents()[0]
    codex_df = agent_dockerfile(agents["codex"], "thinair-sandbox-node:1", "node")
    assert "COPY --from=python:3.12-slim /etc/ssl/certs/ca-certificates.crt" in codex_df
    assert "SSL_CERT_FILE" in codex_df and "apt" not in codex_df
    assert "ca-certificates" not in agent_dockerfile(agents["claude"], "img", "node")
