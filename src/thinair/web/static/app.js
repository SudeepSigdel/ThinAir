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
    const err = new Error(msg);
    err.status = res.status;
    err.detail = d && !Array.isArray(d) ? d : null; // {code, message, ...extra}
    throw err;
  }
  return body;
}

// ---- views ---------------------------------------------------------------------------------
function hideAll() {
  for (const id of ["loading", "view-login", "view-home", "view-repo", "view-job", "view-agents", "view-signin", "view-github", "view-pick", "view-setup", "view-setup-agent", "view-first", "view-health", "setup-bar"]) {
    show(id, false);
  }
  if (current.events) current.events.close();
  current.events = null;
  clearTimeout(current.listTimer);
  current.jobId = null;
  github.shown = false;
}

function showLogin() {
  hideAll();
  show("logout", false);
  show("nav-agents", false);
  show("nav-github", false);
  show("who", false);
  show("view-login");
  $("token").focus();
}

async function route() {
  const m = location.hash.match(/^#\/job\/([A-Za-z0-9]+)$/);
  const r = location.hash.match(/^#\/repo\/([A-Za-z0-9]+)$/);
  const signin = location.hash.match(/^#\/agent\/([a-z][a-z0-9-]{0,30})\/login(?:\?.*)?$/);
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
      // Offline, the service worker still opens this page; say what usually fixes it.
      $("loading").textContent = navigator.onLine
        ? `Can't reach your Thinair server (${e.message}). If its IP address changed, run the ` +
          "installer again on the server, then scan the new QR code."
        : "You're offline. Thinair will load when you're connected again.";
    }
    return;
  }
  show("logout");
  show("nav-agents");
  show("nav-github");
  if (m) await showJob(m[1]);
  else if (r) await showRepo(r[1]);
  else if (signin) await showSignin(signin[1]);
  else if (location.hash === "#/agents") await showAgents();
  else if (location.hash.split("?")[0] === "#/setup/github") await showGithub();
  else if (location.hash === "#/setup/project") await showPick();
  else if (location.hash === "#/setup") await showSetup();
  else if (location.hash === "#/setup/agent") await showSetupAgent();
  else if (location.hash === "#/setup/first-task") await showFirstTask();
  else if (location.hash === "#/settings/health") await showHealth();
  else await showHome();
}

// ---- home: projects + recent jobs ----------------------------------------------------------
async function showHome() {
  hideAll();
  show("view-home");
  offerPicker();
  offerSetup();
  show("health-link-box", isAdmin);
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
  if (job.error_code === "agent_not_authenticated" && job.agent_id) {
    // Fix it from here: the sign-in screen, then "Run again".
    $("job-error-msg").append(
      " ",
      el("a", { href: `#/agent/${job.agent_id}/login`, text: "Sign in to the agent" })
    );
  }
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

// ---- agents: sign-in state and signing in from the phone ------------------------------------
const LOGIN_STATE = { ok: "Signed in", missing: "Not signed in", unknown: "Not checked yet" };
const SIGNIN_FINAL = ["done", "failed", "expired", "cancelled"];
let signin = { agent: null, session: null, takeover: null };

async function showAgents() {
  hideAll();
  show("view-agents");
  showError("agents-error", "");
  agents = null; // sign-in states change: always fresh here
  let list;
  try {
    list = await api("/api/agents");
    agents = list;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("agents-error", e.message);
    return;
  }
  $("agent-list").replaceChildren(...list.map(agentRow));
}

function agentRow(a) {
  const state = el("span", { class: `agent-state ${a.login.state}`, text: LOGIN_STATE[a.login.state] || "" });
  const name = el("span", { class: "agent-name" }, el("strong", { text: a.name }), state);
  const actions = el("span", { class: "actions" });
  if (a.login.in_app) {
    const signedIn = a.login.state === "ok";
    const go = el("button", { type: "button", text: signedIn ? "Sign in again" : "Sign in" });
    if (signedIn) go.className = "secondary";
    go.addEventListener("click", () => (location.hash = `#/agent/${a.id}/login`));
    actions.append(go);
  }
  const check = el("button", { type: "button", class: "secondary", text: "Check" });
  check.addEventListener("click", async () => {
    check.disabled = true;
    state.textContent = "Checking…";
    try {
      const r = await api(`/api/agents/${a.id}/login/check`, { method: "POST" });
      const st = r.logged_in === true ? "ok" : r.logged_in === false ? "missing" : "unknown";
      state.className = `agent-state ${st}`;
      state.textContent = r.logged_in === null ? "Can't be checked automatically" : LOGIN_STATE[st];
    } catch (e) {
      if (!(e instanceof AuthError)) state.textContent = e.message;
    } finally {
      check.disabled = false;
    }
  });
  actions.append(check);
  const row = el("li", { class: "agent-row" }, name, actions);
  if (!a.login.in_app) row.append(el("small", { class: "muted agent-hint", text: a.login_hint }));
  return row;
}

function resetSignin() {
  for (const id of ["signin-preparing", "signin-steps", "signin-link", "signin-url-box", "signin-paste",
    "signin-device", "signin-help", "signin-status", "signin-command-box", "signin-continue",
    "signin-again", "signin-takeover", "signin-cancel"]) show(id, false);
  showError("signin-error", "");
  $("signin-code").value = "";
  $("signin-status").className = "";
}

async function showSignin(agentId) {
  hideAll();
  show("view-signin");
  resetSignin();
  signin = { agent: agentId, session: null, takeover: null };
  const list = await loadAgents();
  const a = list.find((x) => x.id === agentId);
  const name = a ? a.name : agentId;
  $("signin-title").textContent = `Sign in to ${name}`;
  $("signin-intro").textContent =
    `Your login stays inside ${name}'s own storage on your server. Thinair never sees it.`;
  await startSignin();
}

async function startSignin() {
  resetSignin();
  show("signin-preparing");
  $("signin-preparing").textContent = "Starting…";
  let s;
  try {
    s = await api(`/api/agents/${signin.agent}/login`, { method: "POST" });
  } catch (e) {
    if (e instanceof AuthError) return;
    show("signin-preparing", false);
    const d = e.detail || {};
    if (d.code === "login_running" && d.agent === signin.agent) {
      s = { session: d.session }; // this agent's sign-in is already running: continue it
    } else {
      showError("signin-error", e.message);
      if (d.code === "no_in_app_login" && d.command) {
        $("signin-command").textContent = d.command;
        show("signin-command-box");
      } else if (d.code === "login_running") {
        signin.takeover = d.session;
        show("signin-takeover");
      } else {
        show("signin-again");
      }
      return;
    }
  }
  signin.session = s.session;
  show("signin-cancel");
  watchSignin(s.session);
}

function watchSignin(sid) {
  if (current.events) current.events.close();
  const es = new EventSource(`/api/agent-logins/${sid}/events`);
  current.events = es;
  es.addEventListener("state", (ev) => {
    const st = JSON.parse(ev.data);
    renderSignin(st);
    if (SIGNIN_FINAL.includes(st.state)) es.close();
  });
  es.onerror = () => {
    // The browser retries by itself; a 404 (ended long ago) closes the stream for good.
    if (es.readyState === EventSource.CLOSED) {
      showError("signin-error", "Lost the sign-in. Start again.");
      show("signin-again");
    }
  };
}

function renderSignin(st) {
  const final = SIGNIN_FINAL.includes(st.state);
  show("signin-preparing", st.state === "preparing");
  if (st.state === "preparing") {
    $("signin-preparing").textContent = `Getting ${st.name} ready. About a minute the first time.`;
  }
  const steps = Boolean(st.url) && !final;
  show("signin-steps", steps);
  if (steps) {
    $("signin-open-title").textContent = st.user_code
      ? "Open the device page and sign in"
      : "Open the sign-in page and sign in";
    show("signin-link", st.link);
    show("signin-url-box", !st.link);
    if (st.link) {
      $("signin-link").setAttribute("href", st.url);
    } else {
      $("signin-link").removeAttribute("href");
      $("signin-url").textContent = st.url;
    }
    const pasteFlow = !st.user_code;
    show("signin-paste", pasteFlow);
    $("signin-submit").disabled = !st.needs_code;
    $("signin-code").disabled = !st.needs_code;
    show("signin-device", Boolean(st.user_code));
    $("signin-user-code").textContent = st.user_code || "";
  }
  $("signin-help").textContent = st.help || "";
  show("signin-help", Boolean(st.help) && !final);
  const status = { verifying: "Checking the login…", done: `${st.name} is signed in.` }[st.state];
  $("signin-status").textContent = status || "";
  $("signin-status").className = st.state === "done" ? "ok" : "";
  show("signin-status", Boolean(status));
  showError("signin-error", st.state === "done" ? "" : st.message || "");
  show("signin-cancel", !final);
  show("signin-continue", st.state === "done");
  show("signin-again", final && st.state !== "done");
}

async function submitCode(ev) {
  ev.preventDefault();
  const code = $("signin-code").value.trim();
  if (!code || !signin.session) return;
  $("signin-submit").disabled = true;
  try {
    await api(`/api/agent-logins/${signin.session}/input`, { method: "POST", body: JSON.stringify({ code }) });
    $("signin-code").value = "";
  } catch (e) {
    if (!(e instanceof AuthError)) showError("signin-error", e.message);
    $("signin-submit").disabled = false;
  }
}

async function cancelSignin(sid = signin.session) {
  if (!sid) return;
  try {
    await api(`/api/agent-logins/${sid}`, { method: "DELETE" });
  } catch (e) {
    if (!(e instanceof AuthError)) showError("signin-error", e.message);
  }
}

async function copyUserCode() {
  try {
    await navigator.clipboard.writeText($("signin-user-code").textContent);
    $("signin-copy").textContent = "Copied";
    setTimeout(() => ($("signin-copy").textContent = "Copy"), 1500);
  } catch {
    /* clipboard not allowed: the code is on screen */
  }
}

// ---- Connect GitHub (the app) and the repository picker --------------------------------------
const GITHUB_RESULTS = {
  failed: "GitHub didn't finish. Nothing was changed. Try again.",
  bad_state: "That sign-in link has expired or was already used. Tap Connect GitHub again.",
  no_app: "Thinair has no GitHub App yet. Tap Connect GitHub.",
  sync_failed: "Couldn't read what GitHub installed. Wait a moment and reload.",
};
let github = { status: null, shown: false };

function hashQuery() {
  const q = location.hash.split("?")[1] || "";
  return new URLSearchParams(q);
}

async function showGithub() {
  hideAll();
  show("view-github");
  showSetupBar("github");
  showError("github-error", "");
  github.shown = true;
  show("github-body", false);
  show("github-loading");
  try {
    // Coming back from GitHub, the server has already re-read the installations.
    github.status = await api("/api/github");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("github-error", e.message);
    show("github-loading", false);
    return;
  }
  renderGithub(github.status);
}

function renderGithub(st) {
  show("github-loading", false);
  show("github-body");
  const result = hashQuery().get("result");
  const apps = st.app;
  const installs = st.installations || [];
  const connected = st.mode === "app" && installs.length > 0;
  const state = $("github-state");
  state.className = "";
  for (const id of ["github-connect", "github-choose", "github-pick", "github-manage", "github-disconnect",
    "github-installs", "github-next", "github-org", "github-delete", "github-points"]) {
    show(id, false);
  }
  $("github-intro").textContent =
    "Thinair creates its own private GitHub App on your account. You choose which repositories it can see.";
  if (!st.can_manage) {
    $("github-intro").textContent = "";
    state.textContent = connected
      ? "GitHub is connected by an admin."
      : "GitHub isn't connected yet. Ask an admin to connect it.";
    state.className = connected ? "ok" : "";
    return;
  }
  if (connected) {
    state.textContent = `Connected as ${installs.map((i) => i.account).join(", ")}`;
    state.className = "ok";
    $("github-installs").replaceChildren(
      ...installs.map((i) =>
        el("li", {}, el("strong", { text: i.account }), el("small", { class: "muted", text: ` · ${i.selection === "all" ? "all repositories" : "selected repositories"}` })),
      ),
    );
    show("github-installs");
    show("github-pick");
    $("github-manage").href = installs[0].manage_url;
    show("github-manage");
    show("github-disconnect");
    $("github-intro").textContent = "";
  } else if (st.mode === "app" && apps && apps.slug) {
    state.textContent = "Almost there. Thinair's app exists but isn't installed on any repository yet.";
    state.className = "warn";
    $("github-choose").href = `https://github.com/apps/${encodeURIComponent(apps.slug)}/installations/new`;
    show("github-choose");
    show("github-disconnect");
    $("github-intro").textContent = "";
  } else if (st.mode === "app") {
    state.textContent = st.problem || "Thinair's GitHub App can't be read.";
    state.className = "warn";
    show("github-disconnect");
  } else {
    show("github-points");
    show("github-next");
    show("github-org");
    show("github-connect");
    state.textContent = st.mode === "token"
      ? "You're using a token from .env. That works. Connect the app for short-lived keys."
      : "";
    if (!st.public_url_set) {
      state.textContent = "Set THINAIR_PUBLIC_URL first: GitHub sends your phone back to that address.";
      state.className = "warn";
      $("github-connect").disabled = true;
    } else {
      $("github-connect").disabled = false;
    }
  }
  if (result && GITHUB_RESULTS[result]) showError("github-error", GITHUB_RESULTS[result]);
  else showError("github-error", "");
}

async function connectGithub() {
  const button = $("github-connect");
  button.disabled = true;
  showError("github-error", "");
  try {
    const org = $("github-org-name").value.trim();
    const start = await api("/api/github/app", { method: "POST", body: JSON.stringify({ org: org || null }) });
    // A real form POST to github.com (a top-level navigation; the page's CSP allows exactly this).
    const form = el("form", { method: "post", action: start.action, hidden: true });
    form.append(el("input", { type: "hidden", name: "manifest", value: start.manifest }));
    document.body.append(form);
    form.submit();
  } catch (e) {
    if (!(e instanceof AuthError)) showError("github-error", e.message);
    button.disabled = false;
  }
}

async function disconnectGithub() {
  if (!confirm("Thinair will lose access to your repositories. Open pull requests stay.")) return;
  try {
    const out = await api("/api/github/app", { method: "DELETE" });
    github.status = await api("/api/github");
    renderGithub(github.status);
    if (out.delete_url) {
      const note = $("github-delete");
      note.replaceChildren(
        "To remove the app from your GitHub account too: ",
        el("a", { href: out.delete_url, target: "_blank", rel: "noopener noreferrer", text: "open its settings ↗" }),
      );
      show("github-delete");
    }
  } catch (e) {
    if (!(e instanceof AuthError)) showError("github-error", e.message);
  }
}

let pick = { repos: [] };

async function showPick() {
  hideAll();
  show("view-pick");
  showSetupBar("project");
  showError("pick-error", "");
  show("pick-loading");
  show("pick-empty", false);
  show("pick-more", false);
  show("pick-url-form", false);
  $("pick-list").replaceChildren();
  $("pick-search").value = "";
  try {
    const data = await api("/api/github/repos");
    pick.repos = data.repos;
    $("pick-manage").href = data.manage_url;
  } catch (e) {
    show("pick-loading", false);
    if (e instanceof AuthError) return;
    if (e.detail && e.detail.code === "no_app") {
      show("pick-url-form"); // GitHub isn't connected: public repositories still work by address
      return;
    }
    showError("pick-error", `Couldn't reach GitHub. ${e.message}`);
    return;
  }
  show("pick-loading", false);
  show("pick-more");
  renderPick();
}

function renderPick() {
  const q = $("pick-search").value.trim().toLowerCase();
  const rows = pick.repos.filter((r) => !q || r.full_name.toLowerCase().includes(q));
  show("pick-empty", pick.repos.length === 0);
  $("pick-list").replaceChildren(
    ...rows.map((r) => {
      const bits = [r.language, r.pushed_at ? `pushed ${ago(r.pushed_at)}` : null].filter(Boolean);
      const name = el("span", { class: "name", text: r.full_name });
      if (r.private) name.append(el("span", { class: "tag", text: "Private" }));
      if (r.added) name.append(el("span", { class: "tag", text: "Added" }));
      const row = el("button", { type: "button", class: "pick-row" }, name, el("small", { class: "muted", text: bits.join(" · ") }));
      row.addEventListener("click", () => addFromPick(r, row));
      return el("li", {}, row);
    }),
  );
}

async function addFromPick(r, row) {
  row.disabled = true;
  try {
    const repo = await api("/api/repos", { method: "POST", body: JSON.stringify({ url: r.url, private: r.private }) });
    await afterProjectAdded(repo);
  } catch (e) {
    row.disabled = false;
    if (!(e instanceof AuthError)) showError("pick-error", e.message);
  }
}

// In the guided setup the next step follows; otherwise open the project.
async function afterProjectAdded(repo) {
  try {
    const st = await loadSetup();
    if (!st.complete && !st.dismissed) {
      await setupNext();
      return;
    }
  } catch {
    /* fall through to the project page */
  }
  location.hash = `#/repo/${repo.id}`;
}

async function addFromUrl(ev) {
  ev.preventDefault();
  showError("pick-error", "");
  try {
    const repo = await api("/api/repos", { method: "POST", body: JSON.stringify({ url: $("pick-url").value.trim() }) });
    $("pick-url").value = "";
    await afterProjectAdded(repo);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("pick-error", e.message);
  }
}

// Home: offer the picker only when the app is connected and can see something.
async function offerPicker() {
  show("pick-github-box", false);
  try {
    const st = await api("/api/github");
    show("pick-github-box", st.mode === "app" && st.installations.length > 0 && st.allowed);
  } catch {
    /* the add-by-URL form is always there */
  }
}

// ---- first-run setup: Welcome, steps, first task, health -------------------------------------
const SETUP_PATHS = {
  github: "#/setup/github",
  agent: "#/setup/agent",
  project: "#/setup/project",
  first_task: "#/setup/first-task",
};
const STARTER_README =
  'Add a short "Project structure" section to the README that explains what each top-level folder is for. Don\'t change any code.';
const STARTER_TYPOS = "Fix typos in the documentation and comments. Don't change any code behaviour.";
let setup = { state: null, step: null };

async function loadSetup() {
  setup.state = await api("/api/setup");
  return setup.state;
}

function storeGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function storeSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode: the redirect just happens again */
  }
}

// The progress bar above every setup step. `stepId` is one of the four step ids.
async function showSetupBar(stepId) {
  setup.step = stepId;
  try {
    const st = await loadSetup();
    const idx = st.steps.findIndex((x) => x.id === stepId);
    if (idx < 0 || setup.step !== stepId) return;
    $("setup-count").textContent = `${idx + 1} of ${st.total}`;
    $("setup-progress").max = st.total;
    $("setup-progress").value = idx + 1;
    show("setup-bar");
  } catch {
    /* the step itself still works without the bar */
  }
}

async function setupNext() {
  const st = await loadSetup();
  location.hash = st.next ? SETUP_PATHS[st.next] : "#/";
}

async function skipStep(ev) {
  ev.preventDefault();
  if (!setup.step) return;
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({ skip: setup.step }) });
    await setupNext();
  } catch (e) {
    if (!(e instanceof AuthError)) location.hash = "#/";
  }
}

async function showSetup() {
  hideAll();
  show("view-setup");
  storeSet("thinair.setup.seen", "1");
  showError("setup-error", "");
  let st;
  try {
    st = await loadSetup();
  } catch (e) {
    if (!(e instanceof AuthError)) showError("setup-error", e.message);
    return;
  }
  const chosen = st.preferred_agent || "claude";
  for (const r of document.querySelectorAll('input[name="agent-choice"]')) r.checked = r.value === chosen;
  show("setup-other-note", chosen === "other");
  $("setup-steps").replaceChildren(
    ...st.steps.map((x, i) => {
      const status = x.done ? "Done" : x.skipped ? "Skipped" : x.detail || "";
      return el(
        "li",
        {},
        el("a", { href: SETUP_PATHS[x.id], text: `${i + 1}  ${x.label}` }),
        el("span", { class: `st${x.done ? " ok" : ""}`, text: status }),
      );
    }),
  );
  $("setup-continue").textContent = st.complete ? "Back to projects" : st.done_count ? "Continue" : "Start";
}

async function chooseAgent(value) {
  show("setup-other-note", value === "other");
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({ preferred_agent: value }) });
  } catch (e) {
    if (!(e instanceof AuthError)) showError("setup-error", e.message);
  }
}

async function setupContinue() {
  const picked = document.querySelector('input[name="agent-choice"]:checked');
  try {
    if (picked && (!setup.state || setup.state.preferred_agent !== picked.value)) await chooseAgent(picked.value);
    await setupNext();
  } catch (e) {
    if (!(e instanceof AuthError)) showError("setup-error", e.message);
  }
}

async function dismissSetup() {
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({ dismiss: true }) });
  } catch (e) {
    if (e instanceof AuthError) return;
  }
  location.hash = "#/";
}

async function showSetupAgent() {
  hideAll();
  show("view-setup-agent");
  showError("sa-error", "");
  for (const id of ["sa-signin", "sa-continue", "sa-hint"]) show(id, false);
  showSetupBar("agent");
  let st, list;
  try {
    [st, list] = await Promise.all([loadSetup(), api("/api/agents")]);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("sa-error", e.message);
    return;
  }
  const pref = st.preferred_agent;
  if (!pref) {
    location.hash = "#/setup";
    return;
  }
  if (pref === "other") {
    $("sa-title").textContent = "Add an agent later";
    $("sa-status").textContent = "Pick Claude Code or Codex on the previous screen to sign one in. You can add more agents later from the Agents screen.";
    show("sa-continue");
    return;
  }
  const a = list.find((x) => x.id === pref);
  if (!a) {
    location.hash = "#/setup";
    return;
  }
  $("sa-title").textContent = `Sign in to ${a.name}`;
  const step = st.steps.find((x) => x.id === "agent");
  if (step.done) {
    $("sa-status").textContent = `${a.name} is signed in.`;
    show("sa-continue");
  } else if (a.login.in_app) {
    $("sa-status").textContent = `${a.name} isn't signed in yet. It takes about a minute.`;
    $("sa-signin").href = `#/agent/${a.id}/login?from=setup`;
    show("sa-signin");
  } else {
    $("sa-status").textContent = `${a.name} can't sign in from the app yet.`;
    $("sa-hint").textContent = a.login_hint;
    show("sa-hint");
    show("sa-continue");
  }
}

async function showFirstTask() {
  hideAll();
  show("view-first");
  showError("first-error", "");
  showSetupBar("first_task");
  let st, repos, agentList;
  try {
    [st, repos, agentList] = await Promise.all([loadSetup(), api("/api/repos"), api("/api/agents")]);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("first-error", e.message);
    return;
  }
  if (repos.length === 0) {
    location.hash = "#/setup/project";
    return;
  }
  const repo = repos[0]; // most recently used first
  const pref = st.preferred_agent && st.preferred_agent !== "other" ? agentList.find((x) => x.id === st.preferred_agent) : null;
  const signedIn = st.steps.find((x) => x.id === "agent").done;
  $("first-on").textContent = `on ${repo.name}`;
  if (!$("first-task").value) $("first-task").value = STARTER_README;
  firstTask.repo = repo;
  firstTask.agent = pref;
  firstTask.needsSignin = Boolean(pref && !signedIn);
  show("first-form", Boolean(pref));
  show("first-alt", !pref);
  $("first-start").textContent = !pref ? "Start" : firstTask.needsSignin ? `Sign in to ${pref.name} first` : `Start with ${pref.name}`;
  $("first-task").required = !firstTask.needsSignin;
}
const firstTask = { repo: null, agent: null, needsSignin: false };

async function startFirstTask(ev) {
  ev.preventDefault();
  showError("first-error", "");
  if (firstTask.needsSignin) {
    location.hash = `#/agent/${firstTask.agent.id}/login?from=setup`;
    return;
  }
  const button = $("first-start");
  button.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        repo_id: firstTask.repo.id,
        kind: "agent",
        agent_id: firstTask.agent.id,
        task: $("first-task").value.trim(),
      }),
    });
    location.hash = `#/job/${job.id}`;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("first-error", e.message);
  } finally {
    button.disabled = false;
  }
}

async function startFirstProfile() {
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ repo_id: firstTask.repo.id, kind: "profile" }) });
    location.hash = `#/job/${job.id}`;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("first-error", e.message);
  }
}

// Home: "Finish setting up", and the first-visit hop to the Welcome screen.
async function offerSetup() {
  show("setup-card", false);
  let st;
  try {
    st = await loadSetup();
  } catch {
    return;
  }
  if (st.complete || st.dismissed) return;
  if (st.done_count === 0 && !st.preferred_agent && !storeGet("thinair.setup.seen")) {
    location.hash = "#/setup";
    return;
  }
  const next = st.steps.find((x) => x.id === st.next);
  $("setup-card-count").textContent = `${st.done_count} of ${st.total}`;
  $("setup-card-next").textContent = next ? `Next: ${next.label.charAt(0).toLowerCase()}${next.label.slice(1)}` : "";
  $("setup-card-go").href = SETUP_PATHS[st.next] || "#/setup";
  show("setup-card");
}

async function dismissCard() {
  show("setup-card", false);
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({ dismiss: true }) });
  } catch {
    /* it comes back next time */
  }
}

// ---- setup health -----------------------------------------------------------------------------
function fmtSince(seconds) {
  if (seconds < 90) return `${seconds} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

async function showHealth() {
  hideAll();
  show("view-health");
  showError("health-error", "");
  $("health-summary").textContent = "Checking…";
  $("health-list").replaceChildren();
  $("health-first").textContent = "";
  let data;
  try {
    data = await api("/api/health");
  } catch (e) {
    if (e instanceof AuthError) return;
    $("health-summary").textContent = "";
    showError("health-error", e.status === 403 ? "Only an admin can see this screen." : e.message);
    return;
  }
  const n = data.problems;
  const fails = data.checks.filter((c) => c.status === "fail").length;
  $("health-summary").textContent =
    n === 0 ? "Everything is working." : fails ? `${fails} problem${fails === 1 ? "" : "s"} to fix.` : `Working, with ${n} suggestion${n === 1 ? "" : "s"}.`;
  $("health-list").replaceChildren(
    ...data.checks.map((c) => {
      const row = el(
        "li",
        { class: "health-row" },
        el("span", { class: "name" }, el("span", {}, el("span", { class: `dot ${c.status}` }), c.name), el("span", { class: "word", text: c.status === "fail" ? "problem" : c.status === "warn" ? "suggestion" : "ok" })),
      );
      if (c.detail) row.append(el("small", { class: "muted wrap", text: c.detail }));
      if (c.status !== "ok" && c.fix) row.append(el("small", { class: "muted wrap", text: c.fix }));
      if (c.action) {
        const b = el("button", { type: "button", text: c.action.label });
        b.addEventListener("click", () => runHealthAction(c.action, b));
        row.append(b);
      }
      return row;
    }),
  );
  const t0 = data.first_run.length ? Date.parse(data.first_run[0].at) : null;
  const parts = data.first_run
    .slice(1)
    .map((e) => `${e.name.replaceAll("_", " ")} after ${fmtSince(Math.max(0, Math.round((Date.parse(e.at) - t0) / 1000)))}`);
  $("health-first").textContent = parts.length ? `First run: ${parts.join(" · ")}` : "";
}

async function runHealthAction(action, button) {
  if (action.kind === "route") {
    location.hash = action.target;
  } else if (action.kind === "post") {
    button.disabled = true;
    button.textContent = "Started…";
    try {
      await api(action.target, { method: "POST" });
      setTimeout(showHealth, 4000);
    } catch (e) {
      if (!(e instanceof AuthError)) showError("health-error", e.message);
      button.disabled = false;
      button.textContent = action.label;
    }
  } else if (action.kind === "copy") {
    try {
      await navigator.clipboard.writeText(action.target);
      button.textContent = "Copied";
    } catch {
      /* clipboard blocked */
    }
  }
}

// Sign in with the short code from `thinair qr` (no camera).
async function pairWithCode(ev) {
  ev.preventDefault();
  showError("pair-error", "");
  const res = await fetch("/api/pair", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: $("pair-code").value.trim() }),
  });
  if (res.ok) {
    $("pair-code").value = "";
    await route();
  } else if (res.status === 429) {
    showError("pair-error", "Too many attempts. Wait a minute and try again.");
  } else {
    showError("pair-error", "This code has expired or was already used. Run `thinair qr` on the server for a new one.");
  }
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
  $("pair-form").addEventListener("submit", pairWithCode);
  $("pick-url-form").addEventListener("submit", addFromUrl);
  $("setup-skip").addEventListener("click", skipStep);
  $("setup-continue").addEventListener("click", setupContinue);
  $("setup-dismiss").addEventListener("click", dismissSetup);
  $("setup-card-dismiss").addEventListener("click", dismissCard);
  $("sa-continue").addEventListener("click", setupNext);
  $("first-form").addEventListener("submit", startFirstTask);
  $("first-typos").addEventListener("click", () => ($("first-task").value = STARTER_TYPOS));
  $("first-own").addEventListener("click", () => {
    $("first-task").value = "";
    $("first-task").focus();
  });
  $("first-profile").addEventListener("click", startFirstProfile);
  for (const r of document.querySelectorAll('input[name="agent-choice"]')) {
    r.addEventListener("change", () => chooseAgent(r.value));
  }
  $("github-connect").addEventListener("click", connectGithub);
  $("github-disconnect").addEventListener("click", disconnectGithub);
  $("pick-search").addEventListener("input", renderPick);
  $("signin-form").addEventListener("submit", submitCode);
  $("signin-copy").addEventListener("click", copyUserCode);
  $("signin-cancel").addEventListener("click", () => cancelSignin());
  $("signin-again").addEventListener("click", startSignin);
  $("signin-continue").addEventListener("click", () => {
    location.hash = location.hash.includes("from=setup") ? "#/setup/agent" : "#/agents";
  });
  $("signin-takeover").addEventListener("click", async () => {
    await cancelSignin(signin.takeover);
    await startSignin();
  });
  window.addEventListener("hashchange", route);
  window.addEventListener("online", () => {
    if (!$("loading").hidden) route(); // back online after "can't reach": try again
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (!$("view-home").hidden) Promise.all([loadRepos(), loadJobs()]);
    else if (!$("view-repo").hidden) loadRepoJobs();
    else if (github.shown && github.status && github.status.can_manage && github.status.mode === "app") {
      // Back from GitHub's install page: ask for what changed (no webhooks, so we ask).
      api("/api/github/sync", { method: "POST" })
        .then((st) => {
          github.status = st;
          renderGithub(st);
        })
        .catch(() => {});
    }
  });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  pairFromLink().then((paired) => {
    if (paired !== "failed") route();
  });
});
