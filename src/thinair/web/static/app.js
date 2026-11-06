// Thinair mobile UI. Plain JS, no build step. Talks only to the authenticated JSON API.
// Security rule: never build HTML from strings. All text from the server (log lines, repo names, errors)
// goes into the page via textContent, so it can never be interpreted as HTML.
"use strict";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "timeout"]);
const MAX_LOG_LINES = 2000;
const LIST_REFRESH_MS = 4000;
const RESULT_LABELS = {
  passed: "✅ passed",
  test_failed: "❌ tests failed",
  oom_killed: "💥 out of memory",
  timeout: "⏱ timeout",
  startup_failed: "🚫 didn't start",
};
const STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  succeeded: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  timeout: "Timed out",
};

const $ = (id) => document.getElementById(id);
let current = { events: null, listTimer: null, jobId: null, repo: null };
let mode = "agent"; // or "profile"
let agents = null; // [{id, name}] once loaded
let isAdmin = false;
const MAX_DIFF_LINES = 5000;
// Agent jobs that ended before the agent ran have no result to fetch.
const RESULT_CODES = new Set([null, "agent_failed", "agent_timeout"]);

// ---- small helpers -------------------------------------------------------------------------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function show(id, visible = true) {
  $(id).hidden = !visible;
}

function fmtMem(bytes) {
  if (bytes == null) return "–";
  if (bytes >= 1024 ** 3) return `${+(bytes / 1024 ** 3).toFixed(2)} GiB`;
  return `${Math.round(bytes / 1024 ** 2)} MiB`;
}

function fmtCfg(cfg) {
  // Non-breaking spaces keep "256 MiB" together when a narrow card wraps the line.
  return cfg ? `${cfg.cpus} CPU / ${fmtMem(cfg.mem_bytes).replace(" ", " ")}` : "–";
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function setStatus(chip, status) {
  chip.textContent = STATUS_LABELS[status] || status;
  chip.className = `chip s-${status}`;
}

function showError(id, message) {
  const box = $(id);
  box.textContent = message || "";
  box.hidden = !message;
}

function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode: not important */
  }
}

function recall(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

// ---- API -----------------------------------------------------------------------------------
class AuthError extends Error {}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    showLogin();
    throw new AuthError("Not logged in");
  }
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const d = body && body.detail;
    const msg = (d && d.message) || (Array.isArray(d) && d[0] && d[0].msg) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return body;
}

// ---- views ---------------------------------------------------------------------------------
function hideAll() {
  for (const id of ["loading", "view-login", "view-home", "view-repo", "view-job"]) show(id, false);
  if (current.events) current.events.close();
  current.events = null;
  clearTimeout(current.listTimer);
  current.jobId = null;
}

function showLogin() {
  hideAll();
  show("logout", false);
  show("who", false);
  show("view-login");
  $("token").focus();
}

async function route() {
  const m = location.hash.match(/^#\/job\/([A-Za-z0-9]+)$/);
  const r = location.hash.match(/^#\/repo\/([A-Za-z0-9]+)$/);
  try {
    const me = await api("/api/me");
    isAdmin = Boolean(me.is_admin);
    // Only worth showing once there are several people; the owner alone sees nothing new.
    $("who").textContent = me.user && me.user !== "admin" ? me.user : "";
    show("who", Boolean($("who").textContent));
  } catch (e) {
    if (!(e instanceof AuthError)) {
      hideAll();
      show("loading");
      $("loading").textContent = `Cannot reach Thinair: ${e.message}`;
    }
    return;
  }
  show("logout");
  if (m) await showJob(m[1]);
  else if (r) await showRepo(r[1]);
  else await showHome();
}

// ---- home: projects + recent jobs ----------------------------------------------------------
async function showHome() {
  hideAll();
  show("view-home");
  await Promise.all([loadRepos(), loadJobs()]);
}

function firstLine(text, max = 70) {
  const line = (text || "").split("\n")[0].trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function jobLabel(j) {
  if (j.kind === "agent") return firstLine(j.task) || "Agent task";
  return j.kind === "pr" ? "Pull request" : "Resource profile";
}

async function loadRepos() {
  let repos = [];
  try {
    repos = await api("/api/repos");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("repo-error", e.message);
    return;
  }
  $("repos").replaceChildren(
    ...repos.map((r) => {
      const head = [el("strong", { text: r.name })];
      if (r.last_job) {
        const chip = el("span");
        setStatus(chip, r.last_job.status);
        head.push(chip);
      }
      const bits = [];
      if (r.active_jobs) bits.push(`${r.active_jobs} in progress`);
      if (r.last_job) bits.push(`${jobLabel(r.last_job)} · ${ago(r.last_job.created_at)}`);
      else bits.push("No jobs yet");
      return el(
        "li",
        {},
        el("a", { href: `#/repo/${r.id}` }, ...head),
        el("small", { class: "muted", text: bits.join(" · ") }),
      );
    }),
  );
  show("repos-empty", repos.length === 0);
}

async function addRepo(ev) {
  ev.preventDefault();
  showError("repo-error", "");
  const button = $("add-repo");
  button.disabled = true;
  try {
    const repo = await api("/api/repos", {
      method: "POST",
      body: JSON.stringify({ url: $("repo-url").value.trim() }),
    });
    $("repo-url").value = "";
    location.hash = `#/repo/${repo.id}`;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("repo-error", e.message);
  } finally {
    button.disabled = false;
  }
}

function renderJobList(listId, emptyId, jobs, withRepo) {
  $(listId).replaceChildren(
    ...jobs.map((j) => {
      const chip = el("span");
      setStatus(chip, j.status);
      const title = withRepo ? j.repo_name : jobLabel(j);
      const a = el("a", { href: `#/job/${j.id}` }, el("strong", { text: title }), chip);
      const sub = withRepo ? `${jobLabel(j)} · ${ago(j.created_at)}` : ago(j.created_at);
      return el("li", {}, a, el("small", { class: "muted", text: sub }));
    }),
  );
  show(emptyId, jobs.length === 0);
}

// Refresh only while something is in progress and the page is visible: idle costs nothing.
function refreshWhileBusy(jobs, viewId, again) {
  clearTimeout(current.listTimer);
  if (jobs.some((j) => !j.finished) && !document.hidden && !$(viewId).hidden) {
    current.listTimer = setTimeout(again, LIST_REFRESH_MS);
  }
}

async function loadJobs() {
  let jobs = [];
  try {
    jobs = await api("/api/jobs?limit=20");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("repo-error", e.message);
    return;
  }
  renderJobList("jobs", "jobs-empty", jobs, true);
  refreshWhileBusy(jobs, "view-home", () => Promise.all([loadRepos(), loadJobs()]));
}

// ---- one project: start a job, its jobs, its defaults --------------------------------------------
async function loadAgents() {
  if (agents) return agents;
  try {
    agents = await api("/api/agents");
  } catch {
    agents = []; // the profile form still works
  }
  return agents;
}

async function showRepo(id) {
  hideAll();
  show("loading");
  showError("job-error", "");
  let repo;
  try {
    [repo] = await Promise.all([api(`/api/repos/${id}`), loadAgents()]);
  } catch (e) {
    if (!(e instanceof AuthError)) {
      show("loading", false);
      show("view-repo");
      $("repo-title").textContent = "Project not found";
      showError("job-error", e.message);
    }
    return;
  }
  // Shown only once filled in, so a previous project's name or form never flashes.
  show("loading", false);
  show("view-repo");
  current.repo = repo;
  $("repo-title").textContent = repo.name;
  const link = $("repo-link");
  link.textContent = repo.url.replace(/^https:\/\//, "");
  if (repo.url.startsWith("https://github.com/")) link.setAttribute("href", repo.url);

  // The agent list: the project's default first-picked, else the last one used on this phone.
  const options = agents.map((a) => el("option", { value: a.id, text: a.name }));
  $("agent").replaceChildren(...options);
  const preferred = repo.default_agent_id || recall("thinair.agent");
  if (agents.some((a) => a.id === preferred)) $("agent").value = preferred;
  $("mode-agent").disabled = agents.length === 0;
  setMode(agents.length === 0 ? "profile" : mode);
  $("opt-chat").checked = recall("thinair.chat") !== "0";
  $("opt-ask").checked = recall("thinair.ask") === "1";
  updateChatOptions();
  $("repo-ref").value = "";
  $("repo-ref").setAttribute("placeholder", repo.default_ref || "default branch");

  show("repo-settings", isAdmin);
  if (isAdmin) {
    const none = el("option", { value: "", text: "Ask each time (no default)" });
    $("default-agent").replaceChildren(
      none,
      ...agents.map((a) => el("option", { value: a.id, text: a.name })),
    );
    $("default-agent").value = repo.default_agent_id || "";
    $("default-ref").value = repo.default_ref || "";
    show("settings-saved", false);
    showError("settings-error", "");
  }
  await loadRepoJobs();
}

async function loadRepoJobs() {
  const repo = current.repo;
  if (!repo) return;
  let jobs = [];
  try {
    jobs = await api(`/api/jobs?limit=30&repo_id=${encodeURIComponent(repo.id)}`);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("job-error", e.message);
    return;
  }
  renderJobList("repo-jobs", "repo-jobs-empty", jobs, false);
  refreshWhileBusy(jobs, "view-repo", loadRepoJobs);
}

// Conversations need an ACP agent; the options follow the chosen agent.
function updateChatOptions() {
  const a = (agents || []).find((x) => x.id === $("agent").value);
  const can = Boolean(a && a.conversational);
  $("opt-chat").disabled = !can;
  $("opt-ask").disabled = !can || !$("opt-chat").checked;
  $("opt-chat-help").textContent = can
    ? "Reply to the agent while it works."
    : `${a ? a.name : "This agent"} runs one task at a time. Pick an "(ACP)" agent to reply and approve.`;
}

function setMode(next) {
  mode = next;
  const agent = mode === "agent";
  $("mode-profile").setAttribute("aria-selected", String(!agent));
  $("mode-agent").setAttribute("aria-selected", String(agent));
  show("agent-fields", agent);
  $("task").required = agent;
  $("submit-job").textContent = agent ? "Start agent" : "Start profile";
  $("mode-help").textContent = agent
    ? "Your coding agent works on a fresh copy of the project, using your own login, and shows the changes."
    : "Finds the smallest CPU and memory this project's tests pass with.";
  showError("job-error", "");
}

async function submitJob(ev) {
  ev.preventDefault();
  showError("job-error", "");
  const repo = current.repo;
  if (!repo) return;
  const ref = $("repo-ref").value.trim();
  const button = ev.submitter || $("submit-job");
  button.disabled = true;
  try {
    const body = { repo_id: repo.id, kind: mode, ref: ref || null };
    if (mode === "agent") {
      body.agent_id = $("agent").value;
      body.task = $("task").value.trim();
      body.interactive = $("opt-chat").checked && !$("opt-chat").disabled;
      body.ask_permissions = body.interactive && $("opt-ask").checked;
      if (!$("opt-chat").disabled) {
        // Only a real choice is remembered (a greyed-out box says nothing about preference).
        remember("thinair.chat", body.interactive ? "1" : "0");
        remember("thinair.ask", $("opt-ask").checked ? "1" : "0");
      }
    }
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    if (mode === "agent") {
      remember("thinair.agent", body.agent_id);
      $("task").value = "";
    }
    location.hash = `#/job/${job.id}`;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("job-error", e.message);
  } finally {
    button.disabled = false;
  }
}

async function saveRepoSettings(ev) {
  ev.preventDefault();
  show("settings-saved", false);
  showError("settings-error", "");
  const repo = current.repo;
  if (!repo) return;
  try {
    current.repo = await api(`/api/repos/${repo.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        default_agent_id: $("default-agent").value || null,
        default_ref: $("default-ref").value.trim() || null,
      }),
    });
    $("repo-ref").setAttribute("placeholder", current.repo.default_ref || "default branch");
    if (current.repo.default_agent_id) $("agent").value = current.repo.default_agent_id;
    show("settings-saved");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("settings-error", e.message);
  }
}

// ---- one job -------------------------------------------------------------------------------
async function showJob(id) {
  hideAll();
  current.jobId = id;
  show("view-job");
  $("log").replaceChildren();
  $("runs").tBodies[0].replaceChildren();
  show("runs-empty");
  show("report", false);
  $("report").replaceChildren();
  show("agent-result", false);
  $("agent-result").replaceChildren();
  $("agent-msgs").replaceChildren();
  show("agent-empty");
  setAgentState("working");
  showError("reply-error", "");
  show("log-box");
  show("job-status");
  let job;
  try {
    job = await api(`/api/jobs/${id}`);
  } catch (e) {
    if (!(e instanceof AuthError)) {
      $("job-title").textContent = "Job not found";
      showError("job-error-msg", e.message);
      for (const part of ["runs-box", "log-box", "job-status"]) show(part, false);
    }
    return;
  }
  renderJob(job);
  if (job.logs_removed_at) {
    $("log").append(
      el("div", { class: "sys", text: "[thinair] The log lines were removed (older than the retention period). The result is kept." })
    );
  }
  follow(id);
}

function renderJob(job) {
  $("job-title").textContent = job.repo_name;
  $("job-back").textContent = `‹ ${job.repo_name}`;
  $("job-back").setAttribute("href", `#/repo/${job.repo_id}`);
  setStatus($("job-status"), job.status);
  const agent = job.kind === "agent";
  const pr = job.kind === "pr";
  show("agent-box", agent);
  show("reply-form", agent && job.interactive && !job.finished);
  show("runs-box", job.kind === "profile");
  $("job-task").textContent = job.task || "";
  show("job-task", (agent || pr) && !!job.task);
  const bits = [agent ? `Agent: ${job.agent_id}` : pr ? "Pull request" : "Resource profile"];
  if (job.ref) bits.push(`ref ${job.ref}`);
  if (job.commit_sha) bits.push(job.commit_sha.slice(0, 10));
  bits.push(`started ${ago(job.started_at || job.created_at)}`);
  $("job-meta").replaceChildren(bits.join(" · "));
  if (pr && job.parent_job_id) {
    $("job-meta").append(" · ", el("a", { href: `#/job/${job.parent_job_id}`, text: "agent task" }));
  }
  showError("job-error-msg", job.error_message);
  show("cancel", !job.finished);
  show("retry", job.finished);
}

function follow(id) {
  // The browser reconnects by itself after a network drop and sends Last-Event-ID, so the
  // server resends only what we missed. We close it ourselves once the job has finished.
  const es = new EventSource(`/api/jobs/${id}/events`);
  current.events = es;
  es.addEventListener("log", (e) => appendLog(JSON.parse(e.data)));
  es.addEventListener("profile_run", (e) => appendRun(JSON.parse(e.data).data));
  es.addEventListener("agent_msg", (e) => appendAgentMsg(JSON.parse(e.data).data));
  es.addEventListener("status", async (e) => {
    const { status } = JSON.parse(e.data).data;
    setStatus($("job-status"), status);
    if (TERMINAL.has(status)) {
      es.close();
      if (current.jobId !== id) return;
      try {
        const job = await api(`/api/jobs/${id}`);
        renderJob(job);
        // Only ask for results that can exist (no needless 404s).
        if (job.kind === "agent") {
          if (RESULT_CODES.has(job.error_code ?? null) && job.status !== "cancelled") await loadResult(id, job);
        } else if (job.kind === "pr") {
          if (job.status === "succeeded") await loadPrResult(id);
        } else if (job.status === "succeeded" || job.error_code === "baseline_failed") {
          await loadReport(id);
        }
      } catch {
        /* shown elsewhere */
      }
    }
  });
  es.addEventListener("error", async () => {
    if (es.readyState === EventSource.CLOSED) {
      try {
        await api("/api/me"); // 401 here switches to the login screen
      } catch {
        /* handled */
      }
    }
  });
}

function appendLog(event) {
  const log = $("log");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const line = el("div", { text: event.data.line });
  if (event.stream === "system" || event.data.line.startsWith("[thinair]")) line.className = "sys";
  log.append(line);
  while (log.childElementCount > MAX_LOG_LINES) log.firstElementChild.remove();
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

function appendAgentMsg(msg) {
  show("agent-empty", false);
  if (msg.kind === "waiting") return setAgentState("waiting");
  if (msg.kind === "permission_request") return appendPermissionCard(msg);
  if (msg.kind === "permission") {
    // An answer to the oldest open card (requests are answered in order by the driver's ids,
    // but we only need to close one: the card with this title that is still open).
    const open = [...$("agent-msgs").querySelectorAll("li.ask:not(.answered)")];
    const card = open.find((c) => msg.text.startsWith(`${c.dataset.title}:`)) || open[0];
    if (card) closePermissionCard(card, msg.text);
    else $("agent-msgs").append(el("li", { class: "tool", text: msg.text }));
    return;
  }
  const kinds = ["text", "tool", "result", "auth_error", "user"];
  const kind = kinds.includes(msg.kind) ? msg.kind : "text";
  if (kind === "result" && !msg.text) return; // the summary card shows the outcome
  if (kind === "user" || kind === "tool") setAgentState("working");
  $("agent-msgs").append(el("li", { class: kind, text: msg.text }));
}

// ---- conversations: replies and approvals -------------------------------------------------------
function setAgentState(state) {
  const text = {
    working: "The agent is working…",
    waiting: "Your turn: reply, or tap Finish to collect the result.",
    asking: "The agent is waiting for your approval.",
  }[state];
  if (text) $("agent-state").textContent = text;
  $("agent-state").className = state === "working" ? "muted" : "attention";
}

function appendPermissionCard(msg) {
  const data = msg.data || {};
  const card = el("li", { class: "ask" });
  card.dataset.request = String(data.request);
  card.dataset.title = msg.text;
  card.append(el("strong", { text: "Allow this?" }), el("span", { class: "ask-title", text: msg.text }));
  const buttons = el("div", { class: "ask-actions" });
  const options = Array.isArray(data.options) ? data.options : [];
  for (const o of options) {
    const deny = /^reject/.test(o.kind || "");
    const b = el("button", { type: "button", class: deny ? "secondary" : "", text: o.name || o.id });
    b.addEventListener("click", () => answerPermission(card, deny ? null : o.id));
    buttons.append(b);
  }
  if (!options.some((o) => /^reject/.test(o.kind || ""))) {
    const b = el("button", { type: "button", class: "secondary", text: "Deny" });
    b.addEventListener("click", () => answerPermission(card, null));
    buttons.append(b);
  }
  card.append(buttons);
  $("agent-msgs").append(card);
  setAgentState("asking");
}

function closePermissionCard(card, text) {
  card.classList.add("answered");
  card.replaceChildren(el("span", { text }));
}

async function answerPermission(card, option) {
  for (const b of card.querySelectorAll("button")) b.disabled = true;
  showError("reply-error", "");
  try {
    await sendInput({ type: "permission", request: Number(card.dataset.request), option });
    setAgentState("working");
  } catch (e) {
    for (const b of card.querySelectorAll("button")) b.disabled = false;
    if (!(e instanceof AuthError)) showError("reply-error", e.message);
  }
}

function sendInput(body) {
  return api(`/api/jobs/${current.jobId}/input`, { method: "POST", body: JSON.stringify(body) });
}

async function sendReply(ev) {
  ev.preventDefault();
  const text = $("reply").value.trim();
  if (!text) return;
  showError("reply-error", "");
  $("send-reply").disabled = true;
  try {
    await sendInput({ type: "message", text });
    $("reply").value = "";
  } catch (e) {
    if (!(e instanceof AuthError)) showError("reply-error", e.message);
  } finally {
    $("send-reply").disabled = false;
  }
}

async function simpleInput(type) {
  showError("reply-error", "");
  try {
    await sendInput({ type });
  } catch (e) {
    if (!(e instanceof AuthError)) showError("reply-error", e.message);
  }
}

async function loadResult(id, job) {
  let r;
  try {
    r = await api(`/api/jobs/${id}/result`);
  } catch {
    return;
  }
  const stat = r.diff_stat || {};
  const parts = [el("h2", { text: "Result" })];
  if (r.summary) parts.push(el("p", { class: "summary", text: r.summary }));
  const facts = [`${stat.files || 0} file(s) changed`, `+${stat.insertions || 0}`, `−${stat.deletions || 0}`];
  if (stat.cost_usd != null) facts.push(`≈ $${Number(stat.cost_usd).toFixed(2)} reported by the agent`);
  parts.push(el("p", { class: "muted", text: facts.join(" · ") }));
  if (r.diff) {
    const lines = r.diff.split("\n");
    const pre = el("pre", { class: "diff" });
    for (const line of lines.slice(0, MAX_DIFF_LINES)) {
      let cls = "";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff --git")) cls = "meta";
      else if (line.startsWith("@@")) cls = "hunk";
      else if (line.startsWith("+")) cls = "add";
      else if (line.startsWith("-")) cls = "del";
      pre.append(el("div", { class: cls, text: line || " " }));
    }
    parts.push(el("h2", { text: "Changes" }), pre);
    if (lines.length > MAX_DIFF_LINES || stat.truncated) {
      parts.push(el("p", { class: "warn", text: "The diff is long; only the beginning is shown." }));
    }
  } else {
    parts.push(el("p", { class: "muted", text: "The agent made no file changes." }));
  }
  if (job && job.status === "succeeded" && r.has_patch) parts.push(prSection(id, r));
  $("agent-result").replaceChildren(...parts);
  show("agent-result");
}

// Only GitHub links are rendered as links (never javascript: or other schemes from the server).
function prLink(url) {
  if (typeof url !== "string" || !url.startsWith("https://github.com/")) return null;
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "View pull request ↗" });
}

function prSection(id, r) {
  const box = el("div", { class: "pr" }, el("h2", { text: "Pull request" }));
  const link = prLink(r.pr_url);
  const prJob = r.pr_job;
  if (link) {
    box.append(el("p", {}, link));
    return box;
  }
  if (prJob && !prJob.finished) {
    box.append(el("p", {}, "Opening a pull request… ", el("a", { href: `#/job/${prJob.id}`, text: "follow it" })));
    return box;
  }
  if (prJob && prJob.status === "failed" && prJob.error_message) {
    box.append(el("p", { class: "error", text: prJob.error_message }));
  }
  const button = el("button", { type: "button", text: prJob ? "Try again" : "Create pull request" });
  const err = el("p", { class: "error", hidden: "" });
  button.addEventListener("click", async () => {
    button.disabled = true;
    err.hidden = true;
    try {
      const job = await api(`/api/jobs/${id}/pr`, { method: "POST" });
      location.hash = `#/job/${job.id}`;
    } catch (e) {
      if (!(e instanceof AuthError)) {
        err.textContent = e.message;
        err.hidden = false;
      }
      button.disabled = false;
    }
  });
  box.append(
    el("p", { class: "muted", text: "Pushes the change to a new branch and opens a pull request." }),
    el("div", { class: "actions" }, button, el("a", { href: `/api/jobs/${id}/patch`, text: "Download patch" })),
    err,
  );
  return box;
}

async function loadPrResult(id) {
  let r;
  try {
    r = await api(`/api/jobs/${id}/result`);
  } catch {
    return;
  }
  const link = prLink(r.pr_url);
  if (!link) return;
  $("agent-result").replaceChildren(el("h2", { text: "Pull request opened" }), el("p", {}, link));
  show("agent-result");
}

function appendRun(run) {
  show("runs-empty", false);
  let label = RESULT_LABELS[run.outcome] || run.outcome;
  if (run.attempt > 1) label += " (re-run)";
  const time = run.wall_s != null ? `${run.wall_s.toFixed(1)}s` : "–";
  $("runs").tBodies[0].append(
    el(
      "tr",
      { class: `r-${run.outcome}` },
      el("td", { text: run.cpus }),
      el("td", { text: fmtMem(run.mem_bytes) }),
      el("td", { text: label }),
      el("td", { text: fmtMem(run.peak_mem_bytes) }),
      el("td", { text: time }),
    ),
  );
}

async function loadReport(id) {
  let data;
  try {
    data = await api(`/api/jobs/${id}/profile`);
  } catch {
    return; // e.g. the job failed before profiling started; its error is already shown
  }
  const r = data.report;
  const box = $("report");
  const parts = [];
  if (r.min_config) {
    parts.push(
      el(
        "div",
        { class: "cards" },
        card("Minimum that passes", fmtCfg(r.min_config)),
        card("Recommended", fmtCfg(r.recommended), `with ${r.headroom}× headroom`),
        card("Peak memory", fmtMem(r.min_config_peak_mem_bytes), "at the minimum"),
      ),
    );
  }
  if (r.status === "incomplete") parts.push(el("p", { class: "warn", text: "The run limit was reached; this result is partial." }));
  for (const note of r.notes || []) parts.push(el("p", { class: r.status === "baseline_failed" ? "error" : "note", text: note }));
  const tail = r.status === "baseline_failed" && r.baseline && r.baseline.output_tail;
  if (tail && tail.length) parts.push(el("pre", { class: "tail", text: tail.join("\n") }));
  if (r.levels && r.levels.length) {
    const rows = r.levels.map((lv) =>
      el(
        "tr",
        {},
        el("td", { text: lv.cpus }),
        el("td", { text: fmtMem(lv.min_mem_bytes) }),
        el("td", { text: { found: "passes", infeasible: "fails", skipped: "not run" }[lv.status] + (lv.flaky ? " ⚠ flaky" : "") }),
      ),
    );
    parts.push(
      el("h2", { text: "Minimum memory by CPU" }),
      el(
        "div",
        { class: "table-wrap" },
        el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: "CPU" }), el("th", { text: "Memory" }), el("th", { text: "Status" }))), el("tbody", {}, ...rows)),
      ),
    );
  }
  parts.push(
    el(
      "p",
      { class: "downloads" },
      el("a", { href: `/api/jobs/${id}/profile.md`, download: `thinair-profile-${id}.md`, text: "Download Markdown" }),
      " · ",
      el("a", { href: `/api/jobs/${id}/profile`, download: `thinair-profile-${id}.json`, text: "JSON" }),
    ),
  );
  box.replaceChildren(...parts);
  show("report");
}

function card(title, value, sub) {
  return el("div", { class: "card" }, el("small", { text: title }), el("strong", { text: value }), sub ? el("small", { class: "muted", text: sub }) : "");
}

// ---- actions -------------------------------------------------------------------------------
async function cancelJob() {
  if (!current.jobId || !confirm("Cancel this job?")) return;
  try {
    renderJob(await api(`/api/jobs/${current.jobId}/cancel`, { method: "POST" }));
  } catch (e) {
    if (!(e instanceof AuthError)) showError("job-error-msg", e.message);
  }
}

async function retryJob() {
  if (!current.jobId) return;
  try {
    const job = await api(`/api/jobs/${current.jobId}/retry`, { method: "POST" });
    location.hash = `#/job/${job.id}`;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("job-error-msg", e.message);
  }
}

async function login(ev) {
  ev.preventDefault();
  showError("login-error", "");
  const res = await fetch("/api/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: $("token").value.trim() }),
  });
  if (res.ok) {
    $("token").value = "";
    await route();
  } else if (res.status === 429) {
    showError("login-error", "Too many attempts. Wait a minute and try again.");
  } else {
    showError("login-error", "That token was not accepted.");
  }
}

// A QR code from `thinair qr` opens `/#pair=<one-time code>`. Swap it for a session at once and
// drop it from the address bar (and so from this history entry).
async function pairFromLink() {
  const m = location.hash.match(/^#pair=([A-Za-z0-9_-]{10,200})$/);
  if (!m) return false;
  history.replaceState(null, "", location.pathname + location.search);
  const res = await fetch("/api/pair", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: m[1] }),
  });
  if (res.ok) return true;
  showLogin();
  showError(
    "login-error",
    res.status === 429
      ? "Too many attempts. Wait a minute and try again."
      : "This QR code has expired or was already used. Create a new one with `thinair qr`.",
  );
  return "failed";
}

async function logout() {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  showLogin();
}

// ---- start ---------------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", login);
  $("job-form").addEventListener("submit", submitJob);
  $("add-repo-form").addEventListener("submit", addRepo);
  $("agent").addEventListener("change", updateChatOptions);
  $("opt-chat").addEventListener("change", updateChatOptions);
  $("reply-form").addEventListener("submit", sendReply);
  $("stop-turn").addEventListener("click", () => simpleInput("stop"));
  $("end-chat").addEventListener("click", () => simpleInput("end"));
  $("repo-settings-form").addEventListener("submit", saveRepoSettings);
  $("refresh").addEventListener("click", () => Promise.all([loadRepos(), loadJobs()]));
  $("mode-profile").addEventListener("click", () => setMode("profile"));
  $("mode-agent").addEventListener("click", () => setMode("agent"));
  $("cancel").addEventListener("click", cancelJob);
  $("retry").addEventListener("click", retryJob);
  $("logout").addEventListener("click", logout);
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (!$("view-home").hidden) Promise.all([loadRepos(), loadJobs()]);
    else if (!$("view-repo").hidden) loadRepoJobs();
  });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  pairFromLink().then((paired) => {
    if (paired !== "failed") route();
  });
});
