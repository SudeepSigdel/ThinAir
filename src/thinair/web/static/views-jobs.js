// A project, a job, the live view, conversations and approvals, results (core of the app).
// Plain script (no build step): shares the globals of app.js (el, show, api, hideAll, ...).
"use strict";

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
  show("mode-toggle", agents.length > 0);
  setMode(agents.length === 0 ? "profile" : mode);
  $("opt-chat").checked = recall("thinair.chat") !== "0";
  $("opt-ask").checked = recall("thinair.ask") === "1";
  updateChatOptions();
  $("task").value = recall(`thinair.draft.${repo.id}`) || ""; // kept until the task is sent
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
  if (mode === "agent") $("submit-job").textContent = a ? `Start with ${a.name}` : "Start agent";
  updateSigninRow();
  $("opt-chat-help").textContent = can
    ? "Reply to the agent while it works."
    : `${a ? a.name : "This agent"} runs one task at a time. Pick an "(ACP)" agent to reply and approve.`;
}

// Under the task box: the agent isn't signed in, with the way to fix it (the draft stays).
function updateSigninRow() {
  const a = (agents || []).find((x) => x.id === $("agent").value);
  const missing = mode === "agent" && a && a.login && a.login.in_app && a.login.state === "missing";
  show("agent-signin-row", Boolean(missing));
  if (missing) {
    $("agent-signin-text").textContent = `${a.name} isn't signed in.`;
    $("agent-signin-link").setAttribute("href", `#/agent/${a.id}/login`);
  }
}

function setMode(next) {
  mode = next;
  const agent = mode === "agent";
  show("agent-fields", agent);
  $("task").required = agent;
  $("submit-job").textContent = agent ? "Start agent" : "Start profile";
  $("mode-toggle").textContent = agent ? "Measure resources instead" : "Back to an agent task";
  if (agent) updateChatOptions();
  else show("agent-signin-row", false);
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
      remember(`thinair.draft.${repo.id}`, "");
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
  current.job = null;
  current.sysLine = "";
  show("job-state", false);
  show("job-conn", false);
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

let pushOfferedFor = null;

function renderJob(job) {
  if (!TERMINAL.has(job.status) && pushOfferedFor !== job.id) {
    pushOfferedFor = job.id; // once per job: the banner on a running job
    offerNotifications();
  }
  $("job-title").textContent = job.repo_name;
  $("job-back").textContent = `‹ ${job.repo_name}`;
  $("job-back").setAttribute("href", `#/repo/${job.repo_id}`);
  setStatus($("job-status"), job.status);
  const agent = job.kind === "agent";
  const pr = job.kind === "pr";
  current.job = job;
  show("agent-box", agent);
  show("reply-form", agent && job.interactive && !job.finished);
  if (current.logFor !== job.id) {
    current.logFor = job.id;
    $("log-box").open = !agent; // the raw log is open for profiles, folded away for agent tasks
  }
  updateJobState(job);
  show("runs-box", job.kind === "profile");
  $("job-task").textContent = job.task || "";
  show("job-task", (agent || pr) && !!job.task);
  const bits = [agent ? `Agent: ${job.agent_id}` : pr ? "Pull request" : "Resource profile"];
  if (job.ref) bits.push(`ref ${job.ref}`);
  if (job.commit_sha) bits.push(job.commit_sha.slice(0, 10));
  bits.push(`started ${ago(job.started_at || job.created_at)}`);
  if (job.finished && job.started_at && job.finished_at) {
    // How long it ran (a job cancelled while still queued has no start time, so no duration).
    const took = fmtDuration((new Date(job.finished_at) - new Date(job.started_at)) / 1000);
    if (took) bits.push(`took ${took}`);
  }
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

// One line under the title while nothing has happened yet: where the job is.
function updateJobState(job) {
  let text = "";
  const quiet = !$("agent-empty").hidden || job.kind !== "agent";
  if (job.status === "queued") text = "Waiting for a free slot.";
  else if (job.status === "running" && job.kind === "agent" && quiet) text = current.sysLine || "Starting…";
  $("job-state").textContent = text;
  show("job-state", Boolean(text));
}

function follow(id) {
  // The browser reconnects by itself after a network drop and sends Last-Event-ID, so the
  // server resends only what we missed. We close it ourselves once the job has finished.
  const es = new EventSource(`/api/jobs/${id}/events`);
  current.events = es;
  es.addEventListener("open", () => show("job-conn", false));
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
    // A stream that ends with no final status event (jobs interrupted by a restart before the
    // server sent one) would reconnect forever: if the job is already finished, stop.
    if (current.job && current.job.id === id && current.job.finished) {
      es.close();
      show("job-conn", false);
      return;
    }
    if (es.readyState === EventSource.CONNECTING && current.jobId === id) show("job-conn"); // resumes by itself
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
  if (event.stream === "system" || event.data.line.startsWith("[thinair]")) {
    line.className = "sys";
    current.sysLine = event.data.line.replace(/^\[thinair\]\s*/, "");
    if (current.job && current.job.kind === "agent" && !$("agent-empty").hidden) updateJobState(current.job);
  }
  log.append(line);
  while (log.childElementCount > MAX_LOG_LINES) log.firstElementChild.remove();
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

// Follow new content only when the person is already at the bottom; else offer a pill.
function atBottom() {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 100;
}
function afterAppend(stick) {
  if (stick) window.scrollTo({ top: document.documentElement.scrollHeight });
  else show("new-msgs");
}

function appendAgentMsg(msg) {
  const stick = atBottom();
  show("agent-empty", false);
  show("job-state", false);
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
  const li = el("li", { class: kind, text: msg.text });
  if (kind === "tool") {
    // One mono line; tap to read the whole command.
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-expanded", "false");
    const flip = () => li.setAttribute("aria-expanded", String(li.classList.toggle("open")));
    li.addEventListener("click", flip);
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        flip();
      }
    });
  }
  $("agent-msgs").append(li);
  afterAppend(stick);
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
  const stick = atBottom();
  const data = msg.data || {};
  const card = el("li", { class: "ask" });
  card.setAttribute("role", "alertdialog");
  card.setAttribute("aria-label", "Approval needed");
  card.tabIndex = -1;
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
  // How long the agent waits for an answer before it refuses (THINAIR_AGENT_IDLE_S).
  const idle = Number(appMeta.agent_idle_s) || 0;
  if (idle > 0) {
    const cd = el("span", { class: "countdown" });
    card.append(cd);
    const started = Date.now();
    const tick = () => {
      const left = Math.max(0, idle - Math.floor((Date.now() - started) / 1000));
      cd.textContent = left > 0 ? `Waiting ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} before it's refused` : "No answer in time: it was refused.";
      if (left === 0) clearInterval(card._timer);
    };
    tick();
    card._timer = setInterval(tick, 1000);
    permissionTimers.add(card._timer);
  }
  $("agent-msgs").append(card);
  setAgentState("asking");
  afterAppend(stick);
  card.focus({ preventScroll: !stick });
}

function closePermissionCard(card, text) {
  if (card._timer) {
    clearInterval(card._timer);
    permissionTimers.delete(card._timer);
  }
  card.removeAttribute("role");
  card.classList.add("answered");
  card.replaceChildren(el("span", { text }));
}

async function answerPermission(card, option) {
  for (const b of card.querySelectorAll("button")) b.disabled = true;
  showError("reply-error", "");
  try {
    await sendInput({ type: "permission", request: Number(card.dataset.request), option });
    setAgentState("working");
    buzz();
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
  $("agent-result").replaceChildren(resultCard(id, job, r));
  show("agent-result");
  show("job-state", false);
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
  const card = prCard(r, current.job);
  if (!card) return;
  $("agent-result").replaceChildren(card);
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
