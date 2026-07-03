// Bask service worker — makes the dashboard installable + delivers push alerts.
const CACHE = "bask-v1";
const SHELL = [
  "/", "/index.html", "/style.css", "/app.js", "/keep.js",
  "/favicon.svg", "/manifest.webmanifest", "/icon-192.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Network-first so a running dashboard is always live; fall back to cache offline.
// API requests are never cached — the dashboard must reflect real readings.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});

// A push from the Pi → a phone notification.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (_) {}
  e.waitUntil(
    self.registration.showNotification(d.title || "Bask", {
      body: d.body || "",
      tag: d.tag,
      renotify: true,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      data: { url: d.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) return w.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
