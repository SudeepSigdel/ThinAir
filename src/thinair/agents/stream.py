"""Turn an agent CLI's output lines into short, phone-friendly events.

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


def parse_line(line: str, fmt: str = "stream-json") -> list[AgentEvent]:
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
