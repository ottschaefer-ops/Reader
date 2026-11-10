// Keeps the app shell available offline. Feeds are always fetched live.
const CACHE = "reader-v20";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icon-180.png", "icon-192.png", "icon-512.png", "favicon-32.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL.map(u => new Request(u, { cache: "reload" }))))); self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE && k !== "reader-flags").map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin || e.request.method !== "GET") return;
  e.respondWith(fetch(e.request, { cache: "no-cache" }).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request)));
});

// The feed job sends a push when new articles arrive: set the icon count, and
// show the notification iOS requires with every push.
self.addEventListener("push", e => {
  let d = {}; try { d = e.data ? e.data.json() : {}; } catch {}
  const jobs = [self.registration.showNotification(d.title || "Reader", { body: d.body || "New articles", tag: "reader-new", icon: "icon-192.png" })];
  const nav = self.navigator;
  // Tell Reader new articles came, so it refreshes when opened even if it did
  // a few minutes ago.
  jobs.push(caches.open("reader-flags").then(c => c.put("push-at", new Response(String(Date.now())))));
  if (typeof d.badge === "number" && nav && nav.setAppBadge) jobs.push(d.badge > 0 ? nav.setAppBadge(d.badge) : nav.clearAppBadge());
  e.waitUntil(Promise.all(jobs.map(p => Promise.resolve(p).catch(() => {}))));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true })
    .then(ws => { if (!ws.length) return self.clients.openWindow("./"); ws[0].postMessage({ type: "refresh" }); return ws[0].focus(); }));
});
