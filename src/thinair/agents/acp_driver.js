// Thinair's Agent Client Protocol (ACP) client. Runs INSIDE the agent's sandbox as
//   node -e <this file> <task> <agent command...>
// launches the agent over stdio (newline-delimited JSON-RPC 2.0, protocol version 1, see
// https://agentclientprotocol.com/protocol/overview), sends one prompt, and prints compact
// events as JSON lines (`{"acp": <kind>, ...}`) for Thinair's `acp` stream parser.
//
// Self-contained (Node.js standard library only), because it is passed with `node -e`.
// Policy: the sandbox is the security boundary, so permission requests are allowed (like the
// CLI agents' bypass/yolo modes). No file-system or terminal capabilities are advertised (the
// agent works in /workspace itself); any other client method gets "method not found".
"use strict";
const { spawn } = require("node:child_process");
const readline = require("node:readline");

const PROTOCOL_VERSION = 1;
const [task, ...command] = process.argv.slice(1);
const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
const clip = (s, n = 300) => {
  s = typeof s === "string" ? s : JSON.stringify(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};

if (!task || command.length === 0) {
  emit({ acp: "error", message: "usage: <task> <agent command...>" });
  process.exit(2);
}

const child = spawn(command[0], command.slice(1), { stdio: ["pipe", "pipe", "inherit"] });
let nextId = 1;
let sessionId = null;
let done = false;
const pending = new Map();

function send(msg) {
  if (!child.stdin.destroyed) child.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...msg }) + "\n");
}

function request(method, params) {
  const id = nextId++;
  send({ id, method, params });
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

function finish(code) {
  if (done) return;
  done = true;
  child.stdin.end();
  const killer = setTimeout(() => child.kill("SIGKILL"), 3000);
  child.once("exit", () => {
    clearTimeout(killer);
    process.exit(code);
  });
  if (child.exitCode !== null || child.signalCode !== null) process.exit(code);
  child.kill("SIGTERM");
}

function onPermission(params) {
  const options = Array.isArray(params?.options) ? params.options : [];
  const pick =
    options.find((o) => o?.kind === "allow_once") || options.find((o) => o?.kind === "allow_always");
  const title = params?.toolCall?.title || params?.toolCall?.toolCallId || "tool";
  if (!pick) {
    emit({ acp: "permission", title: clip(title), allowed: false });
    return { outcome: { outcome: "cancelled" } };
  }
  emit({ acp: "permission", title: clip(title), allowed: true });
  return { outcome: { outcome: "selected", optionId: pick.optionId } };
}

function onUpdate(u) {
  if (!u || typeof u !== "object") return;
  switch (u.sessionUpdate) {
    case "agent_message_chunk":
      if (u.content?.type === "text" && typeof u.content.text === "string")
        emit({ acp: "text", text: u.content.text, id: u.messageId ?? null });
      break;
    case "tool_call":
      emit({
        acp: "tool",
        id: u.toolCallId,
        title: clip(u.title || u.name || "tool"),
        kind: u.kind || "other",
        status: u.status || "pending",
      });
      break;
    case "tool_call_update":
      if (u.status === "failed" || u.status === "completed")
        emit({ acp: "tool_update", id: u.toolCallId, status: u.status, title: clip(u.title || "") });
      break;
    case "plan":
      if (Array.isArray(u.entries))
        emit({
          acp: "plan",
          total: u.entries.length,
          done: u.entries.filter((e) => e?.status === "completed").length,
        });
      break;
    case "usage_update":
      emit({ acp: "usage", used: u.used, size: u.size, cost: u.cost ?? null });
      break;
    default:
      break; // thoughts, user echo, commands, modes: not useful on a phone
  }
}

function onMessage(msg) {
  if (msg.method === undefined && msg.id !== undefined) {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.error) p.reject(msg.error);
    else p.resolve(msg.result ?? {});
    return;
  }
  if (msg.method === "session/update") return onUpdate(msg.params?.update);
  if (msg.id === undefined) return; // other notifications
  if (msg.method === "session/request_permission")
    return send({ id: msg.id, result: onPermission(msg.params) });
  send({ id: msg.id, error: { code: -32601, message: `Method not found: ${msg.method}` } });
}

readline.createInterface({ input: child.stdout }).on("line", (line) => {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    process.stderr.write(`[acp] non-protocol output: ${clip(line, 200)}\n`);
    return;
  }
  if (msg && typeof msg === "object") onMessage(msg);
});

child.on("error", (e) => {
  emit({ acp: "error", message: `could not start the agent: ${e.message}` });
  done = true;
  process.exit(127);
});
child.on("exit", (code, signal) => {
  if (done) return;
  emit({ acp: "error", message: `the agent exited unexpectedly (${signal || "code " + code})` });
  done = true;
  process.exit(2);
});
process.on("SIGTERM", () => {
  if (sessionId) send({ method: "session/cancel", params: { sessionId } });
  finish(143);
});

// -32000 is ACP's `auth_required`; some agents report login problems as generic errors.
const AUTH_TEXT = /authenticat|unauthori[sz]ed|not logged in|log ?in required|api key/i;
const isAuthError = (e) =>
  e?.code === -32000 || AUTH_TEXT.test(`${e?.message || ""} ${JSON.stringify(e?.data ?? "")}`);

(async () => {
  let stage = "initialize";
  try {
    const init = await request("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      clientInfo: { name: "thinair", version: "1" },
    });
    if (init.protocolVersion !== PROTOCOL_VERSION) {
      emit({ acp: "error", message: `unsupported ACP protocol version ${init.protocolVersion}` });
      return finish(2);
    }
    const info = init.agentInfo || {};
    emit({ acp: "started", name: info.name || null, version: info.version || null });

    stage = "session/new";
    const session = await request("session/new", { cwd: process.cwd(), mcpServers: [] });
    sessionId = session.sessionId;

    stage = "session/prompt";
    const res = await request("session/prompt", {
      sessionId,
      prompt: [{ type: "text", text: task }],
    });
    const stopReason = res.stopReason || "end_turn";
    emit({ acp: "result", stopReason });
    finish(stopReason === "end_turn" ? 0 : 1);
  } catch (e) {
    const message = clip(e?.message || String(e), 500);
    emit({ acp: isAuthError(e) ? "auth_required" : "error", stage, message, code: e?.code ?? null });
    finish(isAuthError(e) ? 3 : 2);
  }
})();
