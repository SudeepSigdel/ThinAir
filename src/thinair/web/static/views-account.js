// Cancel/retry, signing in to Thinair (token, pairing), agents and signing them in.
// Plain script (no build step): shares the globals of app.js (el, show, api, hideAll, ...).
"use strict";

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
