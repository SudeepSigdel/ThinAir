// The change review screen (#/job/<id>/changes), the result card on a finished agent job and the
// "pull request opened" card. Uses diff.js (parseDiff, changeTargets). Builds no markup from strings.
"use strict";

const LARGE_CHANGE_LINES = 400;
const CHUNK_LINES = 300; // lines added to the page per animation frame, so a huge file never freezes it
const diffState = { files: [], targets: [], cursor: -1, jobId: null, wrap: true };

function statusLabel(f) {
  if (f.status === "added") return "new file";
  if (f.status === "deleted") return "deleted";
  if (f.status === "renamed") return `${f.oldPath} → ${f.path}`;
  return "";
}

function diffLine(ln) {
  const cls = ln.t === "+" ? "dl add" : ln.t === "-" ? "dl del" : "dl";
  const no = ln.t === "-" ? ln.o : ln.n; // one number column: the new side, the old for removed lines
  const tx = el("span", { class: "tx" }, ln.s || " ");
  if (ln.cr) tx.append(el("span", { class: "cr", text: "␍" }));
  if (ln.noEol) tx.append(el("span", { class: "eol", text: "  (no newline at end of file)" }));
  return el("div", { class: cls }, el("span", { class: "no", text: no == null ? "" : String(no) }), el("span", { class: "sg", text: ln.t === " " ? "" : ln.t }), tx);
}

// Render one file's body the first time it opens. Lines are appended in chunks.
function renderFileBody(details, file, fileIndex) {
  if (details.dataset.rendered) return;
  details.dataset.rendered = "1";
  const body = el("div", { class: "diff-body" });
  details.append(body);
  if (file.binary) {
    body.append(el("p", { class: "diff-note", text: "Binary file changed." }));
    return;
  }
  if (!file.hunks.length) {
    const why = file.status === "renamed" ? "Renamed, no content changes." : file.notes.length ? file.notes.join("; ") : "No line changes.";
    body.append(el("p", { class: "diff-note", text: why }));
    return;
  }
  const queue = [];
  file.hunks.forEach((h, hi) => {
    const wrap = el("div", { class: "diff-hunk" });
    wrap.dataset.file = String(fileIndex);
    wrap.dataset.hunk = String(hi);
    wrap.append(el("div", { class: "hunk-h", text: h.header }));
    body.append(wrap);
    for (const ln of h.lines) queue.push([wrap, ln]);
  });
  let i = 0;
  const pump = () => {
    const end = Math.min(i + CHUNK_LINES, queue.length);
    for (; i < end; i++) queue[i][0].append(diffLine(queue[i][1]));
    if (i < queue.length) requestAnimationFrame(pump);
  };
  pump();
}

function fileSummary(file, note) {
  const label = statusLabel(file);
  const path = el("span", { class: "path" }, el("span", { class: "caret" }), file.status === "renamed" ? label : file.path);
  if (label && file.status !== "renamed") path.append(el("span", { class: "diff-note", text: `  ${label}` }));
  const stat = el("span", { class: "diff-stat" });
  if (file.binary) stat.append("binary");
  else stat.append(el("span", { class: "add", text: `+${file.additions}` }), " ", el("span", { class: "del", text: `−${file.deletions}` }));
  const summary = el("summary", {}, path, stat);
  if (note) summary.append(el("span", { class: "diff-note", text: note })); // visible while the file is closed
  return summary;
}

async function showChanges(id) {
  hideAll();
  show("view-changes");
  diffState.jobId = id;
  for (const x of ["changes-empty", "changes-truncated", "diff-tools", "diff-jump", "changes-bar", "changes-error"]) show(x, false);
  $("diff-list").replaceChildren();
  $("changes-stat").textContent = "";
  $("changes-summary").textContent = "";
  $("changes-back").setAttribute("href", `#/job/${id}`);
  let job;
  let r;
  try {
    [job, r] = await Promise.all([api(`/api/jobs/${id}`), api(`/api/jobs/${id}/result`)]);
  } catch (e) {
    if (!(e instanceof AuthError)) showError("changes-error", e.status === 404 ? "There are no changes to show for this job." : e.message);
    return;
  }
  $("changes-back").textContent = `‹ ${job.repo_name}`;
  const stat = r.diff_stat || {};
  const files = typeof parseDiff === "function" ? parseDiff(r.diff || "") : [];
  diffState.files = files;
  diffState.targets = changeTargets(files);
  diffState.cursor = -1;
  const adds = stat.insertions ?? files.reduce((n, f) => n + f.additions, 0);
  const dels = stat.deletions ?? files.reduce((n, f) => n + f.deletions, 0);
  $("changes-stat").textContent = `${stat.files ?? files.length} file${(stat.files ?? files.length) === 1 ? "" : "s"}  +${adds}  −${dels}`;
  if (r.summary) $("changes-summary").textContent = r.summary.length > 280 ? `${r.summary.slice(0, 279)}…` : r.summary;

  if (!files.length) {
    show("changes-empty");
  } else {
    show("diff-tools");
    const first = files.findIndex((f) => !f.binary && f.additions + f.deletions <= LARGE_CHANGE_LINES);
    $("diff-list").replaceChildren(
      ...files.map((f, fi) => {
        const d = el("details", { class: "diff-file" });
        d.dataset.file = String(fi);
        const lines = f.additions + f.deletions;
        const large = lines > LARGE_CHANGE_LINES;
        d.append(fileSummary(f, large ? `Large change: ${lines.toLocaleString()} lines. Tap to show.` : ""));
        d.addEventListener("toggle", () => {
          if (d.open) renderFileBody(d, f, fi);
        });
        if (fi === (first < 0 ? 0 : first) && !f.binary && !large) d.open = true; // the first readable file
        return d;
      }),
    );
    for (const d of $("diff-list").querySelectorAll("details[open]")) renderFileBody(d, files[Number(d.dataset.file)], Number(d.dataset.file));
    show("diff-jump", diffState.targets.length > 0);
  }
  if (r.diff_stat && r.diff_stat.truncated) show("changes-truncated");
  const wrap = recall("thinair.diff.wrap") !== "0";
  setDiffWrap(wrap);
  renderChangesBar(id, job, r);
}

function setDiffWrap(wrap) {
  diffState.wrap = wrap;
  $("diff-list").classList.toggle("scroll", !wrap);
  $("diff-wrap").setAttribute("aria-pressed", String(wrap));
  $("diff-wrap").textContent = wrap ? "Wrap lines" : "Scroll sideways";
  remember("thinair.diff.wrap", wrap ? "1" : "0");
}

function jumpChange(delta) {
  const t = diffState.targets;
  if (!t.length) return;
  diffState.cursor = (diffState.cursor + delta + t.length) % t.length;
  const { file, hunk } = t[diffState.cursor];
  const details = $("diff-list").querySelector(`details[data-file="${file}"]`);
  if (!details) return;
  if (!details.open) details.open = true; // its toggle handler renders the body
  renderFileBody(details, diffState.files[file], file);
  const target = hunk < 0 ? details : details.querySelector(`.diff-hunk[data-hunk="${hunk}"]`) || details;
  target.scrollIntoView({ block: "start", behavior: "smooth" });
}

function toggleAllFiles() {
  const all = [...$("diff-list").querySelectorAll("details")];
  const open = !all.every((d) => d.open);
  for (const d of all) d.open = open;
  $("diff-all").textContent = open ? "Collapse all" : "Expand all";
}

// The primary action of the screen sits in a sticky bar at the bottom.
async function renderChangesBar(id, job, r) {
  const bar = $("changes-bar");
  bar.replaceChildren();
  const patch = el("a", { class: "link", href: `/api/jobs/${id}/patch` }, icon("download", ""), " Patch");
  const link = typeof prLink === "function" ? prLink(r.pr_url) : null;
  if (link) {
    link.className = "button";
    bar.append(link, patch);
  } else if (job.status === "succeeded" && r.has_patch) {
    const prJob = r.pr_job;
    if (prJob && !prJob.finished) {
      bar.append(el("a", { class: "button", href: `#/job/${prJob.id}`, text: "Opening the pull request…" }), patch);
    } else {
      let gh = null;
      try {
        gh = await api("/api/github");
      } catch {
        /* fall through: the server decides when the button is tapped */
      }
      if (gh && gh.mode === "none") {
        bar.append(el("a", { class: "button", href: "#/setup/github", text: "Connect GitHub to open a pull request" }), patch);
      } else {
        const b = el("button", { type: "button", text: prJob ? "Try again" : "Create pull request" });
        b.addEventListener("click", async () => {
          b.disabled = true;
          showError("changes-error", "");
          try {
            await openPrEditor(id);
          } catch (e) {
            if (!(e instanceof AuthError)) showError("changes-error", e.message);
          }
          b.disabled = false;
        });
        bar.append(b, patch);
      }
    }
  } else if (r.has_patch) {
    bar.append(patch);
  }
  show("changes-bar", bar.childElementCount > 0);
}

// ---- result card on a finished agent job -------------------------------------------------------
function resultCard(id, job, r) {
  const stat = r.diff_stat || {};
  const files = stat.files || 0;
  const card = el("div", { class: "result-card" }, el("h2", { text: "Result" }));
  if (r.summary) card.append(el("p", { class: "summary", text: r.summary }));
  const facts = [`${files} file${files === 1 ? "" : "s"} changed`, `+${stat.insertions || 0}`, `−${stat.deletions || 0}`];
  if (stat.cost_usd != null) facts.push(`≈ $${Number(stat.cost_usd).toFixed(2)} reported by the agent`);
  card.append(el("p", { class: "stat", text: facts.join("  ·  ") }));
  if (r.diff) {
    card.append(el("div", { class: "actions" }, el("a", { class: "button", href: `#/job/${id}/changes`, text: "Review changes" })));
  } else {
    card.append(el("p", { class: "muted", text: "The agent didn't change any files." }));
  }
  if (job && job.status === "succeeded" && r.has_patch) card.append(prSection(id, r));
  return card;
}

// ---- pull request opened (C6) -------------------------------------------------------------------
function buzz() {
  if (navigator.vibrate) navigator.vibrate(10); // Android only; absent on iOS
}

function prCard(r, job) {
  const link = prLink(r.pr_url);
  if (!link) return null;
  const m = String(r.pr_url).match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/);
  const first = !recall("thinair.celebrated");
  if (first) {
    remember("thinair.celebrated", "1");
    buzz();
  }
  link.className = "button";
  link.textContent = "View on GitHub ↗";
  const card = el("div", { class: `result-card pr-card${first ? " celebrate" : ""}` });
  if (first) card.append(el("div", { class: "lines", "aria-hidden": "true" }, el("span"), el("span"), el("span")));
  card.append(el("h2", { class: first ? "card-rise" : "", text: "Pull request opened" }));
  card.append(el("p", { class: "stat", text: m ? `${m[1]} #${m[2]}` : "" }));
  const actions = el("div", { class: "actions" }, link);
  if (job && job.repo_id) actions.append(el("a", { href: `#/repo/${job.repo_id}`, text: "Start another task" }));
  card.append(actions);
  return card;
}
