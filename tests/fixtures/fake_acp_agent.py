"""A scripted ACP agent (protocol v1, newline-delimited JSON-RPC over stdio) for testing
Thinair's ACP driver. Behaviour is chosen by argv[1]; everything the client sent is appended
to the JSON-lines file in argv[2] so tests can check it."""

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
LOG = sys.argv[2] if len(sys.argv) > 2 else None
_next = 1000


def log(msg: dict) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def read() -> dict:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    msg = json.loads(line)
    log(msg)
    return msg


def update(sid: str, u: dict) -> None:
    send({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def call(method: str, params: dict) -> dict:
    """Agent -> client request; returns the client's response."""
    global _next
    _next += 1
    send({"id": _next, "method": method, "params": params})
    while True:
        msg = read()
        if msg.get("id") == _next and "method" not in msg:
            return msg


def main() -> None:
    if MODE == "crash":
        sys.exit(5)
    init = read()
    version = 2 if MODE == "version2" else 1
    send(
        {
            "id": init["id"],
            "result": {
                "protocolVersion": version,
                "agentCapabilities": {"loadSession": False},
                "agentInfo": {"name": "fake-acp", "version": "0.1"},
                "authMethods": [{"id": "login", "name": "Log in"}],
            },
        }
    )
    new = read()
    if MODE == "auth":
        send({"id": new["id"], "error": {"code": -32000, "message": "Authentication required"}})
        read()  # wait for stdin to close
        return
    sid = "sess_1"
    send({"id": new["id"], "result": {"sessionId": sid}})
    prompt = read()
    print("[fake-acp] working", file=sys.stderr, flush=True)
    if MODE in ("ok", "chat"):  # a visible change in the client's working directory
        cwd = new["params"]["cwd"]
        with open(f"{cwd}/acp_was_here.txt", "w", encoding="utf-8") as f:
            f.write(prompt["params"]["prompt"][0]["text"] + "\n")
    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}})
    update(
        sid,
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "pending"},
            ],
        },
    )
    update(
        sid,
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m1",
            "content": {"type": "text", "text": "Looking "},
        },
    )
    update(
        sid,
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m1",
            "content": {"type": "text", "text": "at it."},
        },
    )
    update(
        sid,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "Run npm test",
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": "npm test"},
        },
    )
    options = [
        {"optionId": "no", "name": "Reject", "kind": "reject_once"},
        {"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
    ]
    if MODE == "reject_only":
        options = options[:1]
    perm = call(
        "session/request_permission",
        {
            "sessionId": sid,
            "toolCall": {"toolCallId": "t1", "title": "Run npm test"},
            "options": options,
        },
    )
    fs = call("fs/read_text_file", {"sessionId": sid, "path": "/etc/passwd"})
    log({"perm_reply": perm, "fs_reply": fs})
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "failed"})
    update(
        sid,
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m2",
            "content": {"type": "text", "text": "Fixed the test."},
        },
    )
    update(
        sid,
        {
            "sessionUpdate": "usage_update",
            "used": 10,
            "size": 100,
            "cost": {"amount": 0.25, "currency": "USD"},
        },
    )
    if MODE == "hang":
        time.sleep(60)
    stop = "refusal" if MODE == "refusal" else "end_turn"
    send({"id": prompt["id"], "result": {"stopReason": stop}})
    turn = 1
    while MODE == "chat":  # more prompts in the same session, each asking permission once
        msg = read()  # exits when the client closes stdin
        if msg.get("method") != "session/prompt":
            continue  # e.g. session/cancel
        turn += 1
        text = msg["params"]["prompt"][0]["text"]
        perm = call(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {"toolCallId": f"t{turn}", "title": f"Edit {text}"},
                "options": options,
            },
        )
        log({"turn": turn, "text": text, "perm_reply": perm})
        update(
            sid,
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": f"r{turn}",
                "content": {"type": "text", "text": f"You said: {text}"},
            },
        )
        send({"id": msg["id"], "result": {"stopReason": "end_turn"}})
    read()  # the client closes stdin when done


if __name__ == "__main__":
    main()
