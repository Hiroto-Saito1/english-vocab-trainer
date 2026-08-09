const SHELL = "m1-shell-v1";
self.addEventListener("install", (event) => event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(["/", "/static/app.js", "/static/manifest.json"]))));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => { if (event.request.url.includes("/audio/")) caches.open("m1-audio").then((cache) => cache.put(event.request, response.clone())); return response; }))));
