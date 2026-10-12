const SHELL = "pwa-shell-v7";
const MANAGED_PREFIX = "pwa-";
const SHELL_ASSETS = ["/", "/static/app.js", "/static/manifest.json", "/static/icon.svg"];

const isShell = (request) => {
  const parsed = new URL(request.url);
  return parsed.origin === self.location.origin
    && request.method === "GET"
    && (parsed.pathname === "/" || parsed.pathname.startsWith("/static/"));
};

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(SHELL_ASSETS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith(MANAGED_PREFIX) && key !== SHELL)
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (!isShell(event.request)) return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const requested = new URL(event.request.url);
        const returned = new URL(response.url);
        if (
          response.status === 200
          && !response.redirected
          && returned.origin === self.location.origin
          && returned.pathname === requested.pathname
        ) {
          event.waitUntil(
            caches.open(SHELL).then((cache) => cache.put(event.request, response.clone())),
          );
        }
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
