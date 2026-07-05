// Paper Collector service worker.
// Strategy:
//  - App shell (icons, manifest): cache-first.
//  - Figures under /data/figures/: cache-first (last daily payload stays offline-viewable).
//  - Root page /: network-first, fall back to cache when offline.
//  - /api/*: never cache (auth-sensitive, dynamic).
const CACHE = "papers-v2";
const SHELL = [
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-180.png",
  "/icons/favicon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(req) {
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) {
      const clone = resp.clone();
      caches.open(CACHE).then((c) => c.put(req, clone));
    }
    return resp;
  } catch (err) {
    const hit = await caches.match(req);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const resp = await fetch(req);
  if (resp && resp.ok) {
    const clone = resp.clone();
    caches.open(CACHE).then((c) => c.put(req, clone));
  }
  return resp;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;   // never intercept API calls
  if (url.pathname === "/" || url.pathname === "/index.html") {
    e.respondWith(networkFirst(req));
    return;
  }
  if (
    url.pathname.startsWith("/data/figures/") ||
    url.pathname.startsWith("/icons/") ||
    url.pathname === "/manifest.json"
  ) {
    e.respondWith(cacheFirst(req));
  }
});
