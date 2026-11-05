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
let current = { events: null, listTimer: null, jobId: null };
let mode = "profile"; // or "agent"
let agentsLoaded = false;
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
  for (const id of ["loading", "view-login", "view-home", "view-job"]) show(id, false);
  if (current.events) current.events.close();
  current.events = null;
  clearTimeout(current.listTimer);
  current.jobId = null;
}

function showLogin() {
  hideAll();
  show("logout", false);
  show("view-login");
  $("token").focus();
}

async function route() {
  const m = location.hash.match(/^#\/job\/([A-Za-z0-9]+)$/);
  try {
    await api("/api/me");
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
  else await showHome();
}

// ---- home ----------------------------------------------------------------------------------
async function showHome() {
  hideAll();
  show("view-home");
  if (!$("repo-url").value) $("repo-url").value = recall("thinair.lastRepo");
  await Promise.all([loadJobs(), loadAgents()]);
}

async function loadAgents() {
  if (agentsLoaded) return;
  try {
    const agents = await api("/api/agents");
    $("agent").replaceChildren(...agents.map((a) => el("option", { value: a.id, text: a.name })));
    $("mode-agent").disabled = agents.length === 0;
    agentsLoaded = true;
  } catch {
    /* the profile form still works */
  }
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
    ? "Runs your chosen coding agent on the repository, using your own login, and shows the changes."
    : "Finds the smallest CPU and memory your project's tests pass with.";
  showError("job-error", "");
}

async function loadJobs() {
  clearTimeout(current.listTimer);
  let jobs = [];
  try {
    jobs = await api("/api/jobs?limit=20");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("job-error", e.message);
    return;
  }
  const list = $("jobs");
  list.replaceChildren(
    ...jobs.map((j) => {
      const chip = el("span");
      setStatus(chip, j.status);
      const a = el("a", { href: `#/job/${j.id}` }, el("strong", { text: j.repo_name }), chip);
      return el("li", {}, a, el("small", { class: "muted", text: ago(j.created_at) }));
    }),
  );
  show("jobs-empty", jobs.length === 0);
  // Refresh only while something is in progress and the page is visible: idle costs nothing.
  if (jobs.some((j) => !j.finished) && !document.hidden && !$("view-home").hidden) {
    current.listTimer = setTimeout(loadJobs, LIST_REFRESH_MS);
  }
}

async function submitJob(ev) {
  ev.preventDefault();
  showError("job-error", "");
  const repoUrl = $("repo-url").value.trim();
  const ref = $("repo-ref").value.trim();
  const button = ev.submitter || $("job-form").querySelector("button");
  button.disabled = true;
  try {
    const body = { repo_url: repoUrl, kind: mode, ref: ref || null };
    if (mode === "agent") {
      body.agent_id = $("agent").value;
      body.task = $("task").value.trim();
    }
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    remember("thinair.lastRepo", repoUrl);
    location.hash = `#/job/${job.id}`;
  } catch (e) {
    if (!(e instanceof AuthError)) showError("job-error", e.message);
  } finally {
    button.disabled = false;
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
  let job;
  try {
    job = await api(`/api/jobs/${id}`);
  } catch (e) {
    if (!(e instanceof AuthError)) {
      $("job-title").textContent = "Job not found";
      showError("job-error-msg", e.message);
    }
    return;
  }
  renderJob(job);
  follow(id);
}

function renderJob(job) {
  $("job-title").textContent = job.repo_name;
  setStatus($("job-status"), job.status);
  const agent = job.kind === "agent";
  const pr = job.kind === "pr";
  show("agent-box", agent);
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
  const kind = ["text", "tool", "result", "auth_error"].includes(msg.kind) ? msg.kind : "text";
  if (kind === "result" && !msg.text) return; // the summary card shows the outcome
  $("agent-msgs").append(el("li", { class: kind, text: msg.text }));
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

async function logout() {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  showLogin();
}

// ---- start ---------------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", login);
  $("job-form").addEventListener("submit", submitJob);
  $("refresh").addEventListener("click", loadJobs);
  $("mode-profile").addEventListener("click", () => setMode("profile"));
  $("mode-agent").addEventListener("click", () => setMode("agent"));
  $("cancel").addEventListener("click", cancelJob);
  $("retry").addEventListener("click", retryJob);
  $("logout").addEventListener("click", logout);
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("view-home").hidden) loadJobs();
  });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  route();
});
