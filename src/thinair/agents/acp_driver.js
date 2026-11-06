// Thinair's Agent Client Protocol (ACP) client. Runs INSIDE the agent's sandbox as
//   node -e <this file> <task> <agent command...>
// launches the agent over stdio (newline-delimited JSON-RPC 2.0, protocol version 1, see
// https://agentclientprotocol.com/protocol/overview), sends one prompt, and prints compact
// events as JSON lines (`{"acp": <kind>, ...}`) for Thinair's `acp` stream parser.
//
// Self-contained (Node.js standard library only), because it is passed with `node -e`.
// Policy: the sandbox is the security boundary, so by default permission requests are allowed
// (like the CLI agents' bypass/yolo modes). No file-system or terminal capabilities are
// advertised (the agent works in /workspace itself); any other client method gets "method not
// found".
//
// Conversation mode (THINAIR_ACP_INTERACTIVE=1): after each turn the driver prints
// {"acp":"waiting"} and reads Thinair's commands from its own stdin, one JSON object per line:
//   {"type":"message","text":"..."}   another prompt in the same session (queued while busy)
//   {"type":"permission","request":N,"option":"<optionId>"|null}   answer a permission request
//   {"type":"stop"}   cancel the current turn (session/cancel), keep the session
//   {"type":"end"}    finish (also: stdin closed, or THINAIR_ACP_IDLE_S without a command)
// With THINAIR_ACP_APPROVAL=ask, permission requests are printed as {"acp":"permission_request"}
// and wait for an answer (none within the idle time = cancelled).
"use strict";
const { spawn } = require("node:child_process");
const readline = require("node:readline");

const PROTOCOL_VERSION = 1;
const [task, ...command] = process.argv.slice(1);
const INTERACTIVE = process.env.THINAIR_ACP_INTERACTIVE === "1";
const ASK = INTERACTIVE && process.env.THINAIR_ACP_APPROVAL === "ask";
const IDLE_MS = Math.max(1, Number(process.env.THINAIR_ACP_IDLE_S) || 600) * 1000;
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

// ---- commands from Thinair (conversation mode) ----------------------------------------------
const inbox = []; // queued messages
let wakeInbox = null; // resolves the wait for the next command
const asking = new Map(); // our request number -> {resolve, options, title}
let nextAsk = 1;

function onCommand(cmd) {
  if (!cmd || typeof cmd !== "object") return;
  if (cmd.type === "permission") {
    const q = asking.get(cmd.request);
    if (!q) return;
    asking.delete(cmd.request);
    clearTimeout(q.timer);
    const opt = q.options.find((o) => o?.optionId === cmd.option);
    q.resolve(opt ? opt : null, "you");
    return;
  }
  if (cmd.type === "stop") {
    if (sessionId) send({ method: "session/cancel", params: { sessionId } });
    return;
  }
  if (cmd.type === "message" && typeof cmd.text === "string" && cmd.text.trim()) inbox.push(cmd);
  if (cmd.type === "end") inbox.push(cmd);
  if (wakeInbox) wakeInbox();
}

if (INTERACTIVE) {
  readline.createInterface({ input: process.stdin }).on("line", (line) => {
    try {
      onCommand(JSON.parse(line));
    } catch {
      /* not a command */
    }
  });
  process.stdin.on("end", () => onCommand({ type: "end" }));
}

// The next message or "end" (null after the idle time).
function nextCommand() {
  if (inbox.length) return Promise.resolve(inbox.shift());
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      wakeInbox = null;
      resolve(null);
    }, IDLE_MS);
    wakeInbox = () => {
      clearTimeout(timer);
      wakeInbox = null;
      resolve(inbox.shift());
    };
  });
}

function decided(title, option, by) {
  emit({ acp: "permission", title: clip(title), allowed: Boolean(option && /^allow/.test(option.kind || "")), option: option?.name || null, by });
  return option
    ? { outcome: { outcome: "selected", optionId: option.optionId } }
    : { outcome: { outcome: "cancelled" } };
}

function onPermission(params) {
  const options = (Array.isArray(params?.options) ? params.options : []).filter((o) => o?.optionId);
  const title = params?.toolCall?.title || params?.toolCall?.toolCallId || "tool";
  if (ASK) {
    const request = nextAsk++;
    return new Promise((resolve) => {
      const done = (option, by) => resolve(decided(title, option, by));
      const timer = setTimeout(() => {
        asking.delete(request);
        done(null, "timeout");
      }, IDLE_MS);
      asking.set(request, { resolve: done, options, title, timer });
      emit({
        acp: "permission_request",
        request,
        title: clip(title),
        kind: params?.toolCall?.kind || null,
        options: options.map((o) => ({ id: o.optionId, name: clip(o.name || o.optionId, 80), kind: o.kind || null })),
      });
    });
  }
  const pick =
    options.find((o) => o.kind === "allow_once") || options.find((o) => o.kind === "allow_always");
  return decided(title, pick || null, "auto");
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
  if (msg.method === "session/request_permission") {
    Promise.resolve(onPermission(msg.params)).then((result) => send({ id: msg.id, result }));
    return;
  }
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
    let text = task;
    let stopReason = "end_turn";
    for (;;) {
      const res = await request("session/prompt", {
        sessionId,
        prompt: [{ type: "text", text }],
      });
      stopReason = res.stopReason || "end_turn";
      emit({ acp: "result", stopReason });
      if (!INTERACTIVE) break;
      emit({ acp: "waiting", idle_s: Math.round(IDLE_MS / 1000) });
      const next = await nextCommand();
      if (!next || next.type === "end") {
        emit({ acp: "ended", reason: next ? "you ended the conversation" : "no reply in time" });
        break;
      }
      text = next.text;
      emit({ acp: "user", text: clip(text, 2000) });
    }
    finish(stopReason === "end_turn" || stopReason === "cancelled" ? 0 : 1);
  } catch (e) {
    const message = clip(e?.message || String(e), 500);
    emit({ acp: isAuthError(e) ? "auth_required" : "error", stage, message, code: e?.code ?? null });
    finish(isAuthError(e) ? 3 : 2);
  }
})();
