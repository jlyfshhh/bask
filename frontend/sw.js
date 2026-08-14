// Bask service worker — makes the dashboard installable + delivers push alerts.
const CACHE = "bask-v6";
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
      // Installed PWAs can otherwise keep running the old app.js indefinitely
      // even after this worker replaces its cache. Navigate each open Bask page
      // once so the freshly activated shell takes effect immediately.
      .then(() => self.clients.matchAll({ type: "window", includeUncontrolled: true }))
      .then((clients) => Promise.all(clients.map((client) => client.navigate(client.url))))
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
