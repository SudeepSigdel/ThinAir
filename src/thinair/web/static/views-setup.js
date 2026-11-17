// Connect GitHub and the picker, the guided first run, setup health, notifications and sharing.
// Plain script (no build step): shares the globals of app.js (el, show, api, hideAll, ...).
"use strict";

// ---- Connect GitHub (the app) and the repository picker --------------------------------------
const GITHUB_RESULTS = {
  failed: "GitHub didn't finish. Nothing was changed. Try again.",
  denied: "You didn't allow Thinair on GitHub. Nothing was changed.",
  mine_connected: "Your GitHub is connected. Pick a repository.",
  mine_taken: "That installation already belongs to someone else on this server. Remove the app on GitHub and install it again from your account.",
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
  show("github-members-note", false);
  show("github-mine", false);
  if (!st.can_manage) {
    $("github-intro").textContent = "";
    state.textContent = connected
      ? "GitHub is connected by an admin."
      : "GitHub isn't connected yet. Ask an admin to connect it.";
    state.className = connected ? "ok" : "";
    renderMine(st);
    if (result && GITHUB_RESULTS[result]) showError("github-error", GITHUB_RESULTS[result]);
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
    showMembersNote(st, apps);
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

// For admins, once connected: how people on this server can connect their own GitHub accounts.
function showMembersNote(st, app) {
  const note = $("github-members-note");
  note.replaceChildren();
  if (!st.callback_ready) {
    note.append("Want people on this server to connect their own GitHub accounts? Reconnect GitHub once (Disconnect, then Connect) to enable it.");
  } else if (app && app.slug) {
    note.append("People on this server can connect their own GitHub accounts once the app is public: ", el("a", { href: `https://github.com/settings/apps/${encodeURIComponent(app.slug)}/advanced`, target: "_blank", rel: "noopener noreferrer", text: "turn on Make public in its settings ↗" }));
  }
  show("github-members-note", note.childNodes.length > 0);
}

// A member's own GitHub account (their installation of the server's app).
function renderMine(st) {
  const m = st.mine;
  if (!m) return;
  const has = m.installations.length > 0;
  show("github-mine");
  $("github-mine-list").replaceChildren(
    ...m.installations.map((i) =>
      el("li", {}, el("strong", { text: i.account }), el("small", { class: "muted", text: ` · ${i.selection === "all" ? "all repositories" : "selected repositories"}` })),
    ),
  );
  $("github-mine-text").textContent = has
    ? "Your tasks use your own GitHub access for these repositories."
    : m.can_connect
      ? "Connect your own GitHub account to work on your own repositories. You choose which ones."
      : "An admin needs to reconnect GitHub once so people can connect their own account.";
  show("github-mine-connect", m.can_connect);
  $("github-mine-connect").textContent = has ? "Change or reconnect" : "Connect my GitHub";
  show("github-mine-forget", has);
  show("github-pick", has);
}

async function connectMine() {
  const b = $("github-mine-connect");
  b.disabled = true;
  showError("github-error", "");
  try {
    const r = await api("/api/github/mine/start", { method: "POST" });
    location.href = r.action; // GitHub asks them to allow Thinair, then sends them back
  } catch (e) {
    b.disabled = false;
    if (!(e instanceof AuthError)) showError("github-error", e.message);
  }
}

async function forgetMine() {
  if (!confirm("Forget your GitHub connection here? Remove the app on GitHub to revoke it there too.")) return;
  try {
    await api("/api/github/mine", { method: "DELETE" });
    github.status = await api("/api/github");
    renderGithub(github.status);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("github-error", e.message);
  }
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

// ---- notifications (Web Push) and installing the app ----------------------------------------------
let sharedRepo = null;
let pushState = { enabled: false, subscribed: false, key: null, deferredInstall: null };

window.addEventListener("beforeinstallprompt", (ev) => {
  ev.preventDefault(); // keep it for our own button
  pushState.deferredInstall = ev;
  if (!$("push-banner").hidden) show("push-install");
});

function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}
function isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}
function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function urlBase64ToUint8Array(text) {
  const pad = "=".repeat((4 - (text.length % 4)) % 4);
  const raw = atob((text + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

// Shown on Projects and on a running job, once the person has something to be told about.
async function offerNotifications(opts = {}) {
  show("push-banner", false);
  if (storeGet("thinair.push.dismissed") && !opts.force) return;
  let st;
  try {
    st = await api("/api/push");
  } catch {
    return;
  }
  pushState.enabled = st.enabled;
  pushState.subscribed = st.subscribed;
  pushState.key = st.public_key;
  if (!st.enabled) return;
  if (st.subscribed) {
    if (!opts.force) return;
    for (const id of ["push-on", "push-later", "push-ios", "push-install"]) show(id, false);
    show("push-test");
    show("push-off");
    showError("push-error", "");
    $("push-text").textContent = "On. You'll hear from Thinair when a job ends.";
    show("push-banner");
    return;
  }
  // iOS only delivers pushes to the Home Screen app; elsewhere the browser must support them.
  if (!pushSupported() && !(isIos() && !isStandalone())) return;
  for (const id of ["push-ios", "push-test", "push-off"]) show(id, false);
  show("push-on");
  show("push-later");
  show("push-install", Boolean(pushState.deferredInstall));
  showError("push-error", "");
  $("push-text").textContent = "One notification when a job finishes or needs your answer.";
  show("push-banner");
}

async function turnOnPush() {
  showError("push-error", "");
  if (isIos() && !isStandalone()) {
    show("push-ios"); // push needs the Home Screen app
    $("push-text").textContent = "Add Thinair to your Home Screen first.";
    return;
  }
  const button = $("push-on");
  button.disabled = true;
  try {
    const permission = await Notification.requestPermission(); // from this tap, as browsers require
    if (permission !== "granted") {
      showError("push-error", "Notifications are blocked for this site. Allow them in your browser's site settings.");
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub =
      (await reg.pushManager.getSubscription()) ||
      (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(pushState.key) }));
    const j = sub.toJSON();
    await api("/api/push/subscriptions", { method: "POST", body: JSON.stringify({ endpoint: j.endpoint, keys: j.keys }) });
    pushState.subscribed = true;
    $("push-text").textContent = "On. You'll hear from Thinair when a job ends.";
    for (const id of ["push-on", "push-later", "push-ios", "push-install"]) show(id, false);
    show("push-test");
    show("push-off");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("push-error", `Couldn't turn notifications on. ${e.message}`);
  } finally {
    button.disabled = false;
  }
}

async function sendPushTest() {
  showError("push-error", "");
  try {
    const r = await api("/api/push/test", { method: "POST" });
    if (!r.sent) showError("push-error", "No phone received it. Turn notifications off and on again.");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("push-error", e.message);
  }
}

async function turnOffPush() {
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api("/api/push/subscriptions", { method: "DELETE", body: JSON.stringify({ endpoint: sub.endpoint }) });
      await sub.unsubscribe();
    }
    show("push-banner", false);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("push-error", e.message);
  }
}

async function installApp() {
  const ev = pushState.deferredInstall;
  if (!ev) return;
  ev.prompt();
  await ev.userChoice.catch(() => {});
  pushState.deferredInstall = null;
  show("push-install", false);
}

// "Share" a GitHub link to Thinair (Android): /?url=...&text=... opens the add-project form.
function handleShare() {
  const q = new URLSearchParams(location.search);
  if (![...q.keys()].some((k) => ["url", "text", "title"].includes(k))) return null;
  const hay = ["url", "text", "title"].map((k) => q.get(k) || "").join(" ");
  const m = hay.match(/https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+/);
  history.replaceState(null, "", location.pathname + "#/"); // drop the query: it is only a hand-off
  return m ? m[0] : null;
}
