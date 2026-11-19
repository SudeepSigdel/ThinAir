// Service worker: makes Thinair installable and lets the app shell open without a connection.
// API calls (/api/...) are never cached or intercepted: job data always comes from the server.
"use strict";

const CACHE = "thinair-shell-v4";
const SHELL = [
  "/",
  "/static/app.js",
  "/static/diff.js",
  "/static/views-jobs.js",
  "/static/views-account.js",
  "/static/views-setup.js",
  "/static/views-settings.js",
  "/static/views-diff.js",
  "/static/icons.svg",
  "/static/style.css",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz") return;
  // Network first, so a server upgrade shows up immediately; the cache is only the offline fallback.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match("/"))),
  );
});

// ---- Web Push ----------------------------------------------------------------------------------
// The server sends {"web_push": 8030, "notification": {title, body, navigate, tag}}. iOS 18.4+ shows
// that natively (Declarative Web Push); everywhere else this handler shows the same notification.
// Only same-origin addresses are ever opened.
function sameOriginUrl(target) {
  try {
    const u = new URL(target || "/", self.location.origin);
    return u.origin === self.location.origin ? u.href : self.location.origin + "/";
  } catch {
    return self.location.origin + "/";
  }
}

self.addEventListener("push", (event) => {
  let data = null;
  try {
    data = event.data ? event.data.json() : null;
  } catch {
    data = null;
  }
  const n = data && data.notification;
  if (!n || typeof n.title !== "string") return;
  event.waitUntil(
    self.registration.showNotification(n.title.slice(0, 120), {
      body: typeof n.body === "string" ? n.body.slice(0, 240) : "",
      tag: typeof n.tag === "string" ? n.tag.slice(0, 80) : undefined,
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      data: { navigate: sameOriginUrl(n.navigate) },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = sameOriginUrl(event.notification.data && event.notification.data.navigate);
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.startsWith(self.location.origin) && "focus" in w) {
          return w.focus().then((c) => {
            if (c && "navigate" in c) return c.navigate(target);
            return undefined;
          });
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
