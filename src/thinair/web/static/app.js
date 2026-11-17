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

// One icon from the sprite (/static/icons.svg), drawn with createElementNS (UI code never builds markup from strings).
function icon(name, label) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  const use = document.createElementNS(NS, "use");
  use.setAttribute("href", `/static/icons.svg#${name}`);
  svg.append(use);
  return svg;
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
const permissionTimers = new Set();
let appMeta = {};

function hideAll() {
  for (const id of ["loading", "view-login", "view-home", "view-repo", "view-job", "view-agents", "view-signin", "view-github", "view-pick", "view-setup", "view-setup-agent", "view-first", "view-health", "setup-bar", "push-banner", "view-activity", "view-settings", "view-devices", "view-people", "view-changes", "new-msgs", "job-state", "job-conn"]) {
    show(id, false);
  }
  if (current.events) current.events.close();
  current.events = null;
  clearTimeout(current.listTimer);
  current.jobId = null;
  github.shown = false;
  for (const t of permissionTimers) clearInterval(t);
  permissionTimers.clear();
}

function showLogin() {
  hideAll();
  updateTabbar(false);
  show("who", false);
  show("view-login");
  $("token").focus();
}

async function route() {
  const m = location.hash.match(/^#\/job\/([A-Za-z0-9]+)$/);
  const r = location.hash.match(/^#\/repo\/([A-Za-z0-9]+)$/);
  const changes = location.hash.match(/^#\/job\/([A-Za-z0-9]+)\/changes$/);
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
  updateTabbar(true);
  if (changes) await showChanges(changes[1]);
  else if (location.hash === "#/activity") await showActivity();
  else if (location.hash === "#/settings") await showSettings();
  else if (location.hash === "#/settings/devices") await showDevices();
  else if (location.hash === "#/settings/people") await showPeople();
  else if (m) await showJob(m[1]);
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
  if (sharedRepo) {
    $("repo-url").value = sharedRepo; // a GitHub link shared into the app: ready to add
    sharedRepo = null;
  }
  offerPicker();
  offerNotifications();
  offerSetup();
  show("health-link-box", false); // Setup health lives in Settings now
  skeletonRows("repos");
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
  $("mode-toggle").addEventListener("click", () => setMode(mode === "agent" ? "profile" : "agent"));
  $("task").addEventListener("input", () => {
    if (current.repo) remember(`thinair.draft.${current.repo.id}`, $("task").value);
  });
  buildTabbar();
  $("jump-prev").append(icon("arrow-up", ""));
  $("jump-next").append(icon("arrow-down", ""));
  $("jump-prev").addEventListener("click", () => jumpChange(-1));
  $("jump-next").addEventListener("click", () => jumpChange(1));
  $("diff-wrap").addEventListener("click", () => setDiffWrap(!diffState.wrap));
  $("diff-all").addEventListener("click", toggleAllFiles);
  $("add-phone").addEventListener("click", addPhone);
  $("person-form").addEventListener("submit", addPerson);
  $("new-msgs").addEventListener("click", () => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
    show("new-msgs", false);
  });
  window.addEventListener("scroll", () => {
    if (!$("new-msgs").hidden && atBottom()) show("new-msgs", false);
  }, { passive: true });
  window.addEventListener("offline", syncOffline);
  window.addEventListener("online", syncOffline);
  syncOffline();
  fetch("/api/meta").then((r) => r.json()).then((m) => (appMeta = m)).catch(() => {});
  $("cancel").addEventListener("click", cancelJob);
  $("retry").addEventListener("click", retryJob);
  $("logout").addEventListener("click", logout);
  $("pair-form").addEventListener("submit", pairWithCode);
  $("push-on").addEventListener("click", turnOnPush);
  $("push-test").addEventListener("click", sendPushTest);
  $("push-off").addEventListener("click", turnOffPush);
  $("push-install").addEventListener("click", installApp);
  $("push-later").addEventListener("click", () => {
    storeSet("thinair.push.dismissed", "1");
    show("push-banner", false);
  });
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
  sharedRepo = handleShare();
  pairFromLink().then((paired) => {
    if (paired !== "failed") route();
  });
});
