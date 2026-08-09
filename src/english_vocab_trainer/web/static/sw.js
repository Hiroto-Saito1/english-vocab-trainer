const SHELL = "pwa-shell-v3";
const AUDIO = "pwa-private-audio-v3";
const MANAGED_PREFIXES = ["m1-", "pwa-"];
const SHELL_ASSETS = ["/", "/static/app.js", "/static/manifest.json", "/static/icon.svg"];
let activeAudioUrls = new Set();

const audioRequest = (url) => new Request(url, { method: "GET" });
const isAudio = (url) => new URL(url).pathname.startsWith("/api/v1/audio/");
const isShell = (request) => {
  const path = new URL(request.url).pathname;
  return request.method === "GET" && (path === "/" || path.startsWith("/static/"));
};

async function rotateAudioCache(urls, rotate) {
  const cache = await caches.open(AUDIO);
  const expected = new Set(urls.map((url) => new URL(url, self.location.origin).href));
  if (rotate) await caches.delete(AUDIO);
  const current = rotate ? await caches.open(AUDIO) : cache;
  await Promise.all(
    (await current.keys())
      .filter((request) => !expected.has(request.url))
      .map((request) => current.delete(request)),
  );
  activeAudioUrls = expected;
}

async function clearPrivateCaches() {
  activeAudioUrls = new Set();
  await Promise.all(
    (await caches.keys())
      .filter((key) => key === AUDIO || key.startsWith("pwa-user-"))
      .map((key) => caches.delete(key)),
  );
}

function rangeFromHeader(header, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(header || "");
  if (!match || (!match[1] && !match[2])) return null;
  const startText = match[1], endText = match[2];
  if (!startText) {
    const suffix = Number(endText);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    return [Math.max(0, size - suffix), size - 1];
  }
  const start = Number(startText);
  const end = endText ? Number(endText) : size - 1;
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || start > end) return null;
  return [start, Math.min(end, size - 1)];
}

async function cachedAudioRange(request) {
  const cache = await caches.open(AUDIO);
  const cached = await cache.match(audioRequest(request.url));
  if (!cached) return new Response(null, { status: 416, headers: { "Content-Range": "bytes */0" } });
  const body = await cached.arrayBuffer();
  const range = rangeFromHeader(request.headers.get("Range"), body.byteLength);
  if (!range) {
    return new Response(null, {
      status: 416,
      headers: { "Accept-Ranges": "bytes", "Content-Range": `bytes */${body.byteLength}` },
    });
  }
  const [start, end] = range;
  const headers = new Headers({
    "Accept-Ranges": "bytes",
    "Content-Range": `bytes ${start}-${end}/${body.byteLength}`,
    "Content-Length": String(end - start + 1),
    "Content-Type": cached.headers.get("Content-Type") || "audio/mpeg",
  });
  const etag = cached.headers.get("ETag");
  if (etag) headers.set("ETag", etag);
  return new Response(body.slice(start, end + 1), { status: 206, headers });
}

async function fetchAudio(request, event) {
  try {
    const response = await fetch(request);
    if (
      !request.headers.has("Range") &&
      response.status === 200 &&
      activeAudioUrls.has(request.url)
    ) {
      event.waitUntil(
        caches.open(AUDIO).then((cache) => cache.put(audioRequest(request.url), response.clone())).catch(() => undefined),
      );
    }
    return response;
  } catch (_) {
    if (request.headers.has("Range")) return cachedAudioRange(request);
    const cache = await caches.open(AUDIO);
    return (await cache.match(audioRequest(request.url))) || Response.error();
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(SHELL_ASSETS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => MANAGED_PREFIXES.some((prefix) => key.startsWith(prefix)) && key !== SHELL && key !== AUDIO)
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  const reply = (payload) => event.ports[0] && event.ports[0].postMessage(payload);
  if (data.type === "set-active-audio") {
    event.waitUntil(rotateAudioCache(data.urls || [], Boolean(data.rotate)).then(() => reply({ ok: true })).catch(() => reply({ ok: false })));
  }
  if (data.type === "clear-private-caches") {
    event.waitUntil(clearPrivateCaches().then(() => reply({ ok: true })).catch(() => reply({ ok: false })));
  }
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  if (isAudio(event.request.url)) {
    event.respondWith(fetchAudio(event.request, event));
    return;
  }
  if (isShell(event.request)) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response.status === 200) event.waitUntil(caches.open(SHELL).then((cache) => cache.put(event.request, response.clone())));
          return response;
        })
        .catch(() => caches.match(event.request)),
    );
  }
});
