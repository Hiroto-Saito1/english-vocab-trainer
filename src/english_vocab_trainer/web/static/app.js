const DB_NAME = "english-vocab-trainer";
const STATE_KEY = "active-session";
const AUDIO_CACHE = "pwa-private-audio-v3";
let state = { sessionId: null, cards: [], current: 0, phase: "unavailable", event: null, undoDeadline: 0 };
let busy = false, syncFlight = null, undoTimer = null;
let audioCacheState = { sessionId: null, expected: 0, cached: 0, ready: false, failed: 0 };
let audioCacheEpoch = 0, preloadFlight = null;
const $ = (selector) => document.querySelector(selector);
const card = $("#card"), term = $("#term"), transcript = $("#transcript"), audio = $("#audio");
const known = $("#known"), unknown = $("#unknown"), continueButton = $("#continue"), undoButton = $("#undo");
const startButton = $("#start"), progress = $("#progress"), status = $("#status");

function openDb() { return new Promise((resolve, reject) => { const request = indexedDB.open(DB_NAME, 1); request.onupgradeneeded = () => { request.result.createObjectStore("events", { keyPath: "id" }); request.result.createObjectStore("state", { keyPath: "key" }); }; request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
async function transaction(storeName, mode, action) { const db = await openDb(); return new Promise((resolve, reject) => { const tx = db.transaction(storeName, mode); let value; tx.oncomplete = () => { db.close(); resolve(value); }; tx.onerror = () => { db.close(); reject(tx.error); }; tx.onabort = () => { db.close(); reject(tx.error); }; value = action(tx.objectStore(storeName)); }); }
const getState = async () => transaction("state", "readonly", (store) => new Promise((resolve, reject) => { const request = store.get(STATE_KEY); request.onsuccess = () => resolve(request.result && request.result.value); request.onerror = () => reject(request.error); }));
const saveState = () => transaction("state", "readwrite", (store) => store.put({ key: STATE_KEY, value: state }));
const addEvent = (event) => transaction("events", "readwrite", (store) => store.put(event));
const removeEvent = (id) => transaction("events", "readwrite", (store) => store.delete(id));
const allEvents = () => transaction("events", "readonly", (store) => new Promise((resolve, reject) => { const request = store.getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }));

function showStatus(message) { status.textContent = message; }
function currentWord() { return state.cards[state.current]; }
function setActions(disabled) { known.disabled = disabled; unknown.disabled = disabled; continueButton.disabled = disabled; }
function play() { audio.play().then(() => { startButton.hidden = true; }).catch(() => { startButton.hidden = false; }); }
function clearUndoTimer() { if (undoTimer) clearTimeout(undoTimer); undoTimer = null; }
function armUndoTimer() { clearUndoTimer(); const remaining = state.undoDeadline - Date.now(); if (remaining <= 0) { state.event = null; state.undoDeadline = 0; saveState(); return; } undoTimer = setTimeout(async () => { state.event = null; state.undoDeadline = 0; await saveState(); render(); }, remaining); }
function render() {
  const word = currentWord(), revealed = state.phase === "revealed";
  if (state.phase === "unavailable") { card.textContent = "Reconnect to start your study session."; term.hidden = true; transcript.hidden = true; audio.removeAttribute("src"); setActions(true); progress.textContent = "Offline"; return; }
  progress.textContent = `${Math.min(state.current + 1, state.cards.length)} of ${state.cards.length}`;
  undoButton.hidden = !state.event || state.undoDeadline <= Date.now();
  term.hidden = !revealed; transcript.hidden = !revealed; continueButton.hidden = !revealed;
  if (!word) { state.phase = "complete"; card.textContent = "Daily study complete."; term.hidden = true; transcript.hidden = true; audio.removeAttribute("src"); setActions(true); progress.textContent = `${state.cards.length} of ${state.cards.length}`; saveState(); return; }
  card.textContent = revealed ? "Review the word and continue when ready." : "Listen, then choose.";
  term.textContent = word.term; transcript.textContent = word.transcript || "Transcript is unavailable.";
  if (!revealed) { audio.src = word.audio_url; audio.currentTime = 0; setActions(false); play(); }
  else { setActions(true); continueButton.disabled = false; audio.currentTime = 0; play(); }
}

function sendWorkerMessage(data) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve(value);
    };
    const timeout = setTimeout(() => finish(false), 2000);
    if (!("serviceWorker" in navigator)) return finish(false);
    navigator.serviceWorker.ready.then((registration) => {
      const worker = navigator.serviceWorker.controller || registration.active;
      if (!worker) return finish(false);
      const channel = new MessageChannel();
      channel.port1.onmessage = (event) => finish(Boolean(event.data && event.data.ok));
      try { worker.postMessage(data, [channel.port2]); } catch (_) { finish(false); }
    }).catch(() => finish(false));
  });
}

async function configureAudioCache(rotate) {
  if (rotate) {
    audioCacheEpoch += 1;
    const pendingPreload = preloadFlight;
    if (pendingPreload) await pendingPreload.catch(() => undefined);
  }
  const urls = state.cards.map((word) => new URL(word.audio_url, location.origin).href);
  if (rotate) await caches.delete(AUDIO_CACHE);
  const cache = await caches.open(AUDIO_CACHE);
  const expected = new Set(urls);
  await Promise.all((await cache.keys()).filter((request) => !expected.has(request.url)).map((request) => cache.delete(request)));
  // Cache Storage is shared with the worker, so this completed rotation is the
  // privacy boundary before any preload can write a new response.  The worker
  // receives the same allow-list before playback requests can populate it.
  await sendWorkerMessage({ type: "set-active-audio", urls, rotate: false });
}

async function preload() {
  const sessionId = state.sessionId, epoch = audioCacheEpoch, urls = state.cards.map((word) => word.audio_url);
  audioCacheState = { sessionId, expected: urls.length, cached: 0, ready: false, failed: 0 };
  const cache = await caches.open(AUDIO_CACHE);
  await Promise.all(urls.map(async (url) => {
    try {
      const response = await window.fetch(url);
      if (audioCacheEpoch !== epoch || state.sessionId !== sessionId || response.status !== 200) throw new Error("audio unavailable");
      await cache.put(new Request(url, { method: "GET" }), response.clone());
      audioCacheState.cached += 1;
    } catch (_) { audioCacheState.failed += 1; }
  }));
  audioCacheState.ready = true;
  if (audioCacheState.failed) showStatus("Some audio could not be saved for offline listening.");
  else showStatus("Audio is ready for offline listening.");
}

function startPreload() {
  const flight = preload();
  preloadFlight = flight;
  flight.then(
    () => { if (preloadFlight === flight) preloadFlight = null; },
    () => { if (preloadFlight === flight) preloadFlight = null; },
  );
  return flight;
}

async function sync() {
  if (syncFlight) return syncFlight;
  syncFlight = (async () => { const events = await allEvents(); if (!events.length || !navigator.onLine) return; try { const response = await fetch("/api/v1/review-events/batch", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(events) }); if (!response.ok) { showStatus("Review sync will retry when online."); return; } const payload = await response.json(); await Promise.all(payload.acknowledged.map(removeEvent)); } catch (_) { showStatus("Review sync will retry when online."); } })();
  try { await syncFlight; } finally { syncFlight = null; }
}
async function answer(action) {
  if (busy || !currentWord() || state.phase !== "listening") return;
  busy = true; setActions(true); audio.pause();
  state.event = { id: crypto.randomUUID(), word_id: currentWord().id, action, reviewed_at: new Date().toISOString() };
  state.undoDeadline = Date.now() + 5000;
  await addEvent(state.event);
  if (action === "known") { state.current += 1; state.phase = "listening"; } else { state.phase = "revealed"; }
  await saveState(); armUndoTimer(); render(); if (!currentWord()) await saveState(); busy = false; sync();
}
async function continueStudy() { if (busy || state.phase !== "revealed") return; busy = true; state.current += 1; state.phase = "listening"; await saveState(); render(); if (!currentWord()) await saveState(); busy = false; }
async function undoLast() {
  if (busy || !state.event || state.undoDeadline <= Date.now()) return;
  busy = true; setActions(true); const event = state.event; await sync();
  const pending = (await allEvents()).some((item) => item.id === event.id);
  try {
    if (pending) await removeEvent(event.id);
    else { const response = await fetch(`/api/v1/review-events/${event.id}/void`, { method: "POST" }); if (!response.ok) throw new Error("void failed"); }
    state.current = Math.max(0, state.current - (state.phase === "listening" ? 1 : 0)); state.phase = "listening"; state.event = null; state.undoDeadline = 0; clearUndoTimer(); await saveState(); render();
  } catch (_) { showStatus("Undo could not be completed. Please try again."); render(); }
  busy = false;
}

async function clearPrivateCaches() {
  audioCacheEpoch += 1;
  const pendingPreload = preloadFlight;
  if (pendingPreload) await pendingPreload.catch(() => undefined);
  const workerCleared = await sendWorkerMessage({ type: "clear-private-caches" });
  if (navigator.serviceWorker && navigator.serviceWorker.controller && !workerCleared) {
    throw new Error("private cache worker did not confirm the reset");
  }
  await Promise.all((await caches.keys()).filter((key) => key === AUDIO_CACHE || key.startsWith("pwa-user-")).map((key) => caches.delete(key)));
  // Clear the durable records transactionally instead of deleting the database:
  // a live browser connection can otherwise block deleteDatabase indefinitely.
  await transaction("events", "readwrite", (store) => store.clear());
  await transaction("state", "readwrite", (store) => store.clear());
  state = { sessionId: null, cards: [], current: 0, phase: "unavailable", event: null, undoDeadline: 0 };
  audioCacheState = { sessionId: null, expected: 0, cached: 0, ready: false, failed: 0 };
  clearUndoTimer(); render(); showStatus("Private study data was cleared.");
}

async function boot() {
  let saved = null;
  try { saved = await getState(); } catch (_) { showStatus("Local study storage is unavailable."); }
  if (saved && saved.cards && saved.phase !== "complete") {
    state = saved;
    try { await configureAudioCache(false); } catch (_) { showStatus("Audio cache is unavailable; listening still works online."); }
  } else {
    try {
      const response = await fetch("/api/v1/sessions?mode=daily");
      if (!response.ok) throw new Error("session unavailable");
      const session = await response.json();
      state = { sessionId: session.id, cards: session.items, current: 0, phase: "listening", event: null, undoDeadline: 0 };
      try { await configureAudioCache(true); } catch (_) { showStatus("Audio cache is unavailable; listening still works online."); }
      await saveState();
    } catch (_) {
      state = saved || state;
      if (!saved) state = { ...state, phase: "unavailable" };
      render(); showStatus("Reconnect to start a new study session.");
      return;
    }
  }
  armUndoTimer(); render(); startPreload().catch(() => showStatus("Some audio could not be saved for offline listening.")); sync();
}

window.clearPrivateCaches = clearPrivateCaches;
window.__pwa = { getAudioCacheState: () => ({ ...audioCacheState }), getState: () => ({ ...state }) };
known.addEventListener("click", () => answer("known")); unknown.addEventListener("click", () => answer("unknown")); continueButton.addEventListener("click", continueStudy); undoButton.addEventListener("click", undoLast); startButton.addEventListener("click", play); addEventListener("online", sync);
if ("serviceWorker" in navigator) window.__pwa.serviceWorkerRegistration = navigator.serviceWorker.register("/sw.js").then(() => "ready", (error) => String(error));
boot().catch(() => { render(); showStatus("Reconnect to start a new study session."); });
