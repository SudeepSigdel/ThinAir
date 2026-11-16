// Tab bar, Activity, Settings, Devices and People. Plain script (no build step): shares the
// globals of app.js (el, show, api, hideAll, ...). Builds no markup from strings.
"use strict";

// ---- tab bar -----------------------------------------------------------------------------------
const TAB_ROUTES = {
  "": "projects",
  "#/": "projects",
  "#/activity": "activity",
  "#/settings": "settings",
  "#/settings/devices": "settings",
  "#/settings/people": "settings",
  "#/settings/health": "settings",
};

function buildTabbar() {
  const tabs = [
    ["tab-projects", "projects", "Projects"],
    ["tab-activity", "activity", "Activity"],
    ["tab-settings", "settings", "Settings"],
  ];
  for (const [id, name, label] of tabs) $(id).replaceChildren(icon(name), el("span", { text: label }));
}

// Visible on the three top-level screens only; job and project pages have their own back link
// and action bar, so they don't show it.
function updateTabbar(loggedIn = true) {
  const key = (location.hash.split("?")[0] || "").replace(/\/$/, "") || "#/";
  const active = TAB_ROUTES[key === "#" ? "#/" : key];
  const visible = loggedIn && Boolean(active);
  show("tabbar", visible);
  document.body.classList.toggle("has-tabbar", visible);
  for (const [id, name] of [["tab-projects", "projects"], ["tab-activity", "activity"], ["tab-settings", "settings"]]) {
    if (name === active) $(id).setAttribute("aria-current", "page");
    else $(id).removeAttribute("aria-current");
  }
}

function skeletonRows(listId, n = 3) {
  $(listId).replaceChildren(...Array.from({ length: n }, () => el("li", { class: "skel", "aria-hidden": "true" })));
}

// ---- offline banner ----------------------------------------------------------------------------
function syncOffline() {
  const off = !navigator.onLine;
  show("offline-banner", off);
  document.body.classList.toggle("offline", off);
}

// ---- Activity ---------------------------------------------------------------------------------
async function showActivity() {
  hideAll();
  show("view-activity");
  showError("activity-error", "");
  show("activity-empty", false);
  skeletonRows("activity-jobs");
  await loadActivity();
}

async function loadActivity() {
  let jobs;
  try {
    jobs = await api("/api/jobs?limit=50");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("activity-error", e.message);
    $("activity-jobs").replaceChildren();
    return;
  }
  renderJobList("activity-jobs", "activity-empty", jobs, true);
  refreshWhileBusy(jobs, "view-activity", loadActivity);
}

// ---- Settings ---------------------------------------------------------------------------------
function settingsRow(href, label, sub, value) {
  const left = el("span", {}, label);
  if (sub) left.append(el("span", { class: "sub", text: sub }));
  const right = el("span", { class: "val" }, value || "", icon("chevron-right", ""));
  right.lastChild.classList.add("sm");
  return el("li", {}, el("a", { href }, left, right));
}

async function showSettings() {
  hideAll();
  show("view-settings");
  $("settings-url").textContent = location.origin;
  const rows = $("settings-rows");
  rows.replaceChildren(...Array.from({ length: 4 }, () => el("li", { class: "skel", "aria-hidden": "true" })));
  const [gh, devices, meta, users, push] = await Promise.all([
    api("/api/github").catch(() => null),
    api("/api/devices").catch(() => []),
    fetch("/api/meta").then((r) => r.json()).catch(() => ({})),
    isAdmin ? api("/api/users").catch(() => []) : Promise.resolve([]),
    api("/api/push").catch(() => null),
  ]);
  $("settings-version").textContent = meta.version || "";
  let ghValue = "Not connected";
  if (gh && gh.mode === "app" && gh.installations.length) ghValue = `Connected · ${gh.installations.map((i) => i.account).join(", ")}`;
  else if (gh && gh.mode === "token") ghValue = "Token in .env";
  const items = [
    settingsRow("#/setup/github", "GitHub", null, ghValue),
    settingsRow("#/agents", "Agents", null, ""),
  ];
  const notif = settingsRow("#", "Notifications", null, push && push.enabled ? (push.subscribed ? "This phone · On" : "Off") : "Unavailable");
  notif.firstChild.addEventListener("click", (ev) => {
    ev.preventDefault();
    if (push && push.enabled) offerNotifications({ force: true }).then(() => window.scrollTo(0, 0));
  });
  items.push(notif);
  if (isAdmin) items.push(settingsRow("#/settings/health", "Setup health", null, ""));
  items.push(settingsRow("#/settings/devices", "Devices", null, `${devices.length}`));
  if (isAdmin) items.push(settingsRow("#/settings/people", "People", null, `${users.length}`));
  rows.replaceChildren(...items);
}

// ---- QR code drawn from the server's SVG path (no library, no markup strings) ----------------------------
function drawQr(svg, qr) {
  const NS = "http://www.w3.org/2000/svg";
  svg.setAttribute("viewBox", `0 0 ${qr.size} ${qr.size}`);
  svg.setAttribute("shape-rendering", "crispEdges");
  const bg = document.createElementNS(NS, "rect");
  bg.setAttribute("width", qr.size);
  bg.setAttribute("height", qr.size);
  bg.setAttribute("fill", "#fff");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", qr.path);
  path.setAttribute("fill", "#000");
  svg.replaceChildren(bg, path);
}

// ---- Devices ---------------------------------------------------------------------------------
async function showDevices() {
  hideAll();
  show("view-devices");
  show("pair-box", false);
  showError("devices-error", "");
  await loadDevices();
}

async function loadDevices() {
  let list;
  try {
    list = await api("/api/devices");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("devices-error", e.message);
    return;
  }
  $("device-list").replaceChildren(
    ...list.map((d) => {
      const left = el("span", {}, d.name + (d.current ? " (this device)" : ""));
      left.append(el("span", { class: "sub", text: d.last_used_at ? `Last used ${ago(d.last_used_at)}` : "Never used" }));
      const row = el("span", { class: "row-static" }, left);
      const b = el("button", { type: "button", class: "secondary", text: d.current ? "Sign out" : "Revoke" });
      b.addEventListener("click", () => revokeDevice(d, b));
      row.append(b);
      return el("li", {}, row);
    }),
  );
}

async function revokeDevice(d, button) {
  const msg = d.current ? "Sign out of this device?" : `Revoke "${d.name}"? It will be signed out and stop getting notifications.`;
  if (!confirm(msg)) return;
  button.disabled = true;
  try {
    await api(`/api/devices/${d.id}`, { method: "DELETE" });
    if (d.current) {
      showLogin();
      return;
    }
    await loadDevices();
  } catch (e) {
    button.disabled = false;
    if (!(e instanceof AuthError)) showError("devices-error", e.message);
  }
}

async function addPhone() {
  showError("devices-error", "");
  try {
    const r = await api("/api/devices/pair", { method: "POST", body: JSON.stringify({}) });
    drawQr($("pair-qr"), r.qr);
    $("pair-url").textContent = r.url;
    $("pair-short").textContent = r.short;
    show("pair-box");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("devices-error", e.message);
  }
}

// ---- People (admins) -------------------------------------------------------------------------
async function showPeople() {
  hideAll();
  show("view-people");
  show("person-pair", false);
  showError("people-error", "");
  if (!isAdmin) {
    showError("people-error", "Only an admin can manage people.");
    return;
  }
  await loadPeople();
}

async function loadPeople() {
  let users;
  try {
    users = await api("/api/users");
  } catch (e) {
    if (!(e instanceof AuthError)) showError("people-error", e.message);
    return;
  }
  $("people-list").replaceChildren(
    ...users.map((u) => {
      const left = el("span", {}, u.name);
      left.append(el("span", { class: "sub", text: [u.is_admin ? "Admin" : "Member", u.disabled_at ? "disabled" : null].filter(Boolean).join(" · ") }));
      const row = el("span", { class: "row-static" }, left);
      if (u.id && !u.disabled_at) {
        const b = el("button", { type: "button", class: "secondary", text: "Disable" });
        b.addEventListener("click", async () => {
          if (!confirm(`Disable ${u.name}? Their phones are signed out. Their jobs stay.`)) return;
          try {
            await api(`/api/users/${encodeURIComponent(u.name)}/disable`, { method: "POST" });
            await loadPeople();
          } catch (e) {
            if (!(e instanceof AuthError)) showError("people-error", e.message);
          }
        });
        row.append(b);
      }
      return el("li", {}, row);
    }),
  );
}

async function addPerson(ev) {
  ev.preventDefault();
  showError("people-error", "");
  show("person-pair", false);
  const name = $("person-name").value.trim();
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify({ name }) });
    const r = await api("/api/devices/pair", { method: "POST", body: JSON.stringify({ user: name }) });
    drawQr($("person-qr"), r.qr);
    $("person-short").textContent = r.short;
    show("person-pair");
    $("person-name").value = "";
    await loadPeople();
  } catch (e) {
    if (!(e instanceof AuthError)) showError("people-error", e.message);
  }
}
