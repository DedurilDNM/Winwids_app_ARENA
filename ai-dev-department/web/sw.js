// Service worker: caches the static app shell so the PWA opens instantly
// and installs on phones/PCs. API, WebSocket and noVNC traffic is never cached.
const CACHE = "aidd-shell-v10";
const SHELL = [
  "./", "index.html", "css/app.css", "icon.svg", "manifest.webmanifest",
  "js/app.js", "js/api.js", "js/ui.js", "js/browser.js", "js/attention.js",
  "js/views/login.js", "js/views/accounts.js", "js/views/github.js",
  "js/views/projects.js", "js/views/profile.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (!url.pathname.startsWith("/app/")) return; // /api, /ws, /novnc: network only
  // Network first (fresh code after deploys), cache as offline fallback.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((r) => r || caches.match("index.html")))
  );
});
