"""Turn an agent CLI's output lines into short, phone-friendly events.

Formats (`output:` in the agent YAML):
- `stream-json`: Claude Code `--output-format stream-json --verbose` (stateless, `parse_line`).
- `codex-json`: Codex `exec --json` (JSONL `thread.*` / `turn.*` / `item.*` events).
- `gemini-stream-json`: Gemini CLI `--output-format stream-json` (assistant text arrives in
  `delta` chunks, so this parser keeps state and joins them).
- `text`: every line is a log line.

Use `StreamParser(fmt)`: `feed(line)` per output line, then `finish()` once at the end.
Every parser is defensive: anything unrecognised becomes a plain log line, never an error.

For `stream-json` (Claude Code), each line is a JSON object. The docs specify the final
`result` message and `system` events; the shape of assistant messages is not documented, so
parsing is defensive: anything unrecognised becomes a plain log line, never an error.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["text", "tool", "result", "auth_error", "log"]

# `system/api_retry` error codes that mean "this login does not work" (from the headless docs).
_AUTH_ERRORS = {
    "authentication_failed",
    "oauth_org_not_allowed",
    "account_on_hold",
    "billing_error",
}
_MAX = 500


@dataclass
class AgentEvent:
    kind: Kind
    text: str
    data: dict[str, Any] = field(default_factory=dict)


def _short(value: Any, limit: int = 160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _tool_summary(name: str, args: Any) -> str:
    if isinstance(args, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description", "query"):
            if isinstance(args.get(key), str):
                return f"{name}: {_short(args[key])}"
    return name


def _json(line: str) -> dict[str, Any] | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def parse_line(line: str, fmt: str = "stream-json") -> list[AgentEvent]:
    """Stateless parsing for Claude Code's stream-json (and `text`)."""
    if fmt != "stream-json":
        return [AgentEvent("log", line)]
    try:
        msg = json.loads(line)
    except ValueError:
        return [AgentEvent("log", line)]
    if not isinstance(msg, dict):
        return [AgentEvent("log", _short(line, _MAX))]

    kind = msg.get("type")
    if kind == "assistant":
        content = (msg.get("message") or {}).get("content")
        events: list[AgentEvent] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.append(AgentEvent("text", block["text"].strip()))
                elif block.get("type") == "tool_use":
                    name = str(block.get("name", "tool"))
                    events.append(AgentEvent("tool", _tool_summary(name, block.get("input"))))
        return events
    if kind == "user":
        return []  # tool results: verbose and already reflected in the next assistant message
    if kind == "system":
        sub = msg.get("subtype")
        if sub == "init":
            model = msg.get("model")
            return [
                AgentEvent("log", f"[agent] session started{f' (model {model})' if model else ''}")
            ]
        if sub == "api_retry" and msg.get("error") in _AUTH_ERRORS:
            return [
                AgentEvent("auth_error", f"[agent] {msg.get('error')}", {"error": msg.get("error")})
            ]
        return [AgentEvent("log", f"[agent] {sub or 'system'}: {_short(msg, 200)}")]
    if kind == "result":
        data = {
            k: msg.get(k)
            for k in (
                "subtype",
                "is_error",
                "total_cost_usd",
                "num_turns",
                "duration_ms",
                "session_id",
            )
            if k in msg
        }
        text = msg.get("result") if isinstance(msg.get("result"), str) else ""
        return [AgentEvent("result", text, data)]
    return [AgentEvent("log", _short(line, _MAX))]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class StreamParser:
    """Per-run parser for any supported format."""

    def __init__(self, fmt: str) -> None:
        self.fmt = fmt
        self._buf: list[str] = []  # gemini: assistant text not yet emitted
        self._last_text = ""  # codex/gemini: the agent's latest message = the run's summary

    def feed(self, line: str) -> list[AgentEvent]:
        if self.fmt == "codex-json":
            return self._codex(line)
        if self.fmt == "gemini-stream-json":
            return self._gemini(line)
        return parse_line(line, self.fmt)

    def finish(self) -> list[AgentEvent]:
        return self._flush()

    # ---- Codex `exec --json` (schema: codex-rs/exec/src/exec_events.rs @ rust-v0.166.0) ----
    def _codex(self, line: str) -> list[AgentEvent]:
        msg = _json(line)
        if msg is None:
            return [AgentEvent("log", _short(line, _MAX))]
        kind = msg.get("type")
        if kind == "thread.started":
            return [AgentEvent("log", "[agent] session started")]
        if kind == "turn.started":
            return []
        if kind == "turn.completed":
            usage = _dict(msg.get("usage"))
            return [AgentEvent("result", self._last_text, {"is_error": False, "usage": usage})]
        if kind == "turn.failed":
            text = str(_dict(msg.get("error")).get("message") or "The agent reported a failure.")
            return [AgentEvent("result", text, {"is_error": True})]
        if kind == "error":
            return [AgentEvent("log", f"[agent] {_short(msg.get('message', ''), 300)}")]
        if kind in ("item.started", "item.updated", "item.completed") and isinstance(
            msg.get("item"), dict
        ):
            return self._codex_item(kind, msg["item"])
        return [AgentEvent("log", _short(line, _MAX))]

    def _codex_item(self, kind: str, item: dict[str, Any]) -> list[AgentEvent]:
        itype = item.get("type")
        started, completed = kind == "item.started", kind == "item.completed"
        if itype == "agent_message" and completed:
            text = str(item.get("text") or "").strip()
            if text:
                self._last_text = text
                return [AgentEvent("text", text)]
        elif itype == "command_execution":
            if started:
                return [AgentEvent("tool", f"Shell: {_short(item.get('command', ''))}")]
            code = item.get("exit_code")
            if completed and isinstance(code, int) and code != 0:
                return [AgentEvent("log", f"[agent] command exited with {code}")]
        elif itype == "file_change" and completed:
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            paths = [str(c["path"]) for c in changes if isinstance(c, dict) and c.get("path")]
            if paths:
                return [AgentEvent("tool", f"Edit: {_short(', '.join(paths))}")]
        elif itype == "mcp_tool_call" and started:
            return [AgentEvent("tool", f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}")]
        elif itype == "web_search" and started:
            return [AgentEvent("tool", f"WebSearch: {_short(item.get('query', ''))}")]
        elif itype == "error" and completed:
            return [AgentEvent("log", f"[agent] {_short(item.get('message', ''), 300)}")]
        return []  # reasoning, to-do lists, progress updates: not useful on a phone

    # ---- Gemini CLI stream-json (schema: packages/core/src/output/types.ts @ v0.67.0) ----
    def _flush(self) -> list[AgentEvent]:
        text = "".join(self._buf).strip()
        self._buf.clear()
        if not text:
            return []
        self._last_text = text
        return [AgentEvent("text", text)]

    def _gemini(self, line: str) -> list[AgentEvent]:
        msg = _json(line)
        if msg is None:
            return [*self._flush(), AgentEvent("log", _short(line, _MAX))]
        kind = msg.get("type")
        if kind == "message":
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), str):
                return []
            self._buf.append(msg["content"])
            return [] if msg.get("delta") else self._flush()
        events = self._flush()
        if kind == "init":
            model = msg.get("model")
            suffix = f" (model {model})" if model else ""
            events.append(AgentEvent("log", f"[agent] session started{suffix}"))
        elif kind == "tool_use":
            name = str(msg.get("tool_name") or "tool")
            events.append(AgentEvent("tool", _tool_summary(name, msg.get("parameters"))))
        elif kind == "tool_result":
            if msg.get("status") == "error":
                why = _short(_dict(msg.get("error")).get("message", ""))
                events.append(AgentEvent("log", f"[agent] tool failed: {why}"))
        elif kind == "error":
            sev = msg.get("severity", "error")
            events.append(
                AgentEvent("log", f"[agent] {sev}: {_short(msg.get('message', ''), 300)}")
            )
        elif kind == "result":
            is_error = msg.get("status") != "success"
            err_text = str(_dict(msg.get("error")).get("message") or "")
            data: dict[str, Any] = {"is_error": is_error}
            if isinstance(msg.get("stats"), dict):
                data["stats"] = msg["stats"]
            events.append(AgentEvent("result", err_text if is_error else self._last_text, data))
        else:
            events.append(AgentEvent("log", _short(line, _MAX)))
        return events
