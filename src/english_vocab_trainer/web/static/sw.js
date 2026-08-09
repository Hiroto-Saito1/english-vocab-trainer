const SHELL = "m1-shell-v2";
const AUDIO = "m1-audio-v2";
self.addEventListener("install", (event) => event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(["/", "/static/app.js", "/static/manifest.json"]))));
self.addEventListener("activate", (event) => event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith("m1-") && key !== SHELL && key !== AUDIO).map((key) => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (event) => {
  if (event.request.url.includes("/audio/")) {
    event.respondWith(fetch(event.request).then((response) => { if (event.request.headers.has("Range") || response.status !== 200) return response; caches.open(AUDIO).then((cache) => cache.put(event.request, response.clone())); return response; }).catch(() => caches.match(event.request)));
    return;
  }
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
