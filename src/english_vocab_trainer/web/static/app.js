const DB_NAME = "english-vocab-trainer";
const STATE_KEY = "active-session";
const AUDIO_CACHE = "pwa-private-audio-v3";
const AUDIO_PRELOAD_CONCURRENCY = 4;
const SYNC_BATCH_SIZE = 100;
let state = { sessionId: null, cards: [], current: 0, phase: "unavailable", event: null, undoDeadline: 0, learningQueue: [], learningStepSeconds: 600, revealedLearningWordId: null };
let busy = false, syncFlight = null, undoTimer = null, learningTimer = null;
let audioCacheState = { sessionId: null, expected: 0, cached: 0, ready: false, failed: 0 };
let audioCacheEpoch = 0, preloadFlight = null;
let audioPresentation = null;
const $ = (selector) => document.querySelector(selector);
const card = $("#card"), term = $("#term"), tier = $("#tier"), transcript = $("#transcript"), audio = $("#audio");
const known = $("#known"), unknown = $("#unknown"), continueButton = $("#continue"), undoButton = $("#undo");
const startButton = $("#start"), progress = $("#progress"), status = $("#status");
const logoutButton = $("#logout");

function csrfToken() { return document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith("__Host-vocab-csrf=") || value.startsWith("vocab-csrf="))?.split("=").slice(1).join("="); }
async function apiFetch(url, options = {}) {
  const { redirectOn401 = true, ...requestOptions } = options;
  const method = (requestOptions.method || "GET").toUpperCase();
  const headers = new Headers(requestOptions.headers || {}), token = csrfToken();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && token) headers.set("X-CSRF-Token", token);
  const response = await fetch(url, { ...requestOptions, headers });
  if (redirectOn401 && response.status === 401 && navigator.onLine) location.assign("/login");
  return response;
}

function openDb() { return new Promise((resolve, reject) => { const request = indexedDB.open(DB_NAME, 1); request.onupgradeneeded = () => { request.result.createObjectStore("events", { keyPath: "id" }); request.result.createObjectStore("state", { keyPath: "key" }); }; request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
async function transaction(storeName, mode, action) { const db = await openDb(); return new Promise((resolve, reject) => { const tx = db.transaction(storeName, mode); let value; tx.oncomplete = () => { db.close(); resolve(value); }; tx.onerror = () => { db.close(); reject(tx.error); }; tx.onabort = () => { db.close(); reject(tx.error); }; value = action(tx.objectStore(storeName)); }); }
const getState = async () => transaction("state", "readonly", (store) => new Promise((resolve, reject) => { const request = store.get(STATE_KEY); request.onsuccess = () => resolve(request.result && request.result.value); request.onerror = () => reject(request.error); }));
const saveState = () => transaction("state", "readwrite", (store) => store.put({ key: STATE_KEY, value: state }));
const addEvent = (event) => transaction("events", "readwrite", (store) => store.put(event));
const removeEvent = (id) => transaction("events", "readwrite", (store) => store.delete(id));
const allEvents = () => transaction("events", "readonly", (store) => new Promise((resolve, reject) => { const request = store.getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }));

function showStatus(message) { status.textContent = message; }
function nowMs() { return window.__pwaTestClock?.now?.() ?? Date.now(); }
function nextLearning() { return state.learningQueue.filter((item) => item.due_at <= nowMs()).sort((a, b) => a.due_at - b.due_at)[0]; }
function currentLearning() {
  if (state.current < state.cards.length) return undefined;
  if (state.phase === "revealed" && state.revealedLearningWordId) {
    return state.learningQueue.find((item) => item.word.id === state.revealedLearningWordId);
  }
  return nextLearning();
}
function currentWord() { return state.cards[state.current] || currentLearning()?.word; }
function formatTier(word) {
  const name = word.tier === "upper" ? "Upper" : word.tier === "ultra" ? "Ultra" : "Unknown";
  return `${name} · ${word.level == null ? "level unknown" : `SVL ${word.level}`}`;
}
function setActions(disabled) { known.disabled = disabled; unknown.disabled = disabled; continueButton.disabled = disabled; }
function play() { audio.play().then(() => { startButton.hidden = true; }).catch(() => { startButton.hidden = false; }); }
function presentAudio(word, revealed) {
  const identity = `${word.id}:${revealed ? "revealed" : "listening"}`;
  if (audioPresentation === identity) return;
  audioPresentation = identity;
  audio.src = word.audio_url;
  audio.currentTime = 0;
  play();
}
function clearAudioPresentation() {
  audioPresentation = null;
  audio.removeAttribute("src");
}
function clearUndoTimer() { if (undoTimer) clearTimeout(undoTimer); undoTimer = null; }
function clearLearningTimer() { if (learningTimer) clearTimeout(learningTimer); learningTimer = null; }
function armLearningTimer() {
  clearLearningTimer();
  // Do not let a due review interrupt an initial card or repeatedly render at
  // zero delay while that card is on screen.
  if (state.current < state.cards.length || currentLearning() || !state.learningQueue.length) return;
  const earliest = Math.min(...state.learningQueue.map((item) => item.due_at));
  if (earliest <= nowMs()) return;
  learningTimer = setTimeout(() => render(), Math.max(0, earliest - nowMs()));
}
function updateUndoPresentation() { undoButton.hidden = !state.event || state.undoDeadline <= nowMs(); }
function expireUndo() {
  if (!state.event || state.undoDeadline > nowMs()) return false;
  state.event = null; state.undoDeadline = 0; clearUndoTimer(); updateUndoPresentation();
  return true;
}
function armUndoTimer() {
  clearUndoTimer();
  const remaining = state.undoDeadline - nowMs();
  if (remaining <= 0) { if (expireUndo()) saveState(); return; }
  undoTimer = setTimeout(async () => { if (expireUndo()) await saveState(); }, remaining);
}
function render() {
  if (expireUndo()) saveState();
  if (
    state.current >= state.cards.length
    && state.revealedLearningWordId
    && state.learningQueue.some((item) => item.word.id === state.revealedLearningWordId)
  ) state.phase = "revealed";
  const word = currentWord();
  if (word && state.phase === "waiting") state.phase = "listening";
  const revealed = state.phase === "revealed";
  if (state.phase === "unavailable") { card.textContent = "Reconnect to start your study session."; term.hidden = true; tier.hidden = true; transcript.hidden = true; clearAudioPresentation(); setActions(true); progress.textContent = "Offline"; return; }
  progress.textContent = `${Math.min(state.current + 1, state.cards.length)} of ${state.cards.length}`;
  updateUndoPresentation();
  term.hidden = !revealed; tier.hidden = !revealed; transcript.hidden = !revealed; continueButton.hidden = !revealed;
  if (!word) {
    term.hidden = true; tier.hidden = true; transcript.hidden = true; clearAudioPresentation(); setActions(true);
    if (state.learningQueue.length) {
      state.phase = "waiting";
      const seconds = Math.max(0, Math.ceil((Math.min(...state.learningQueue.map((item) => item.due_at)) - nowMs()) / 1000));
      card.textContent = `Waiting for the next review: ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.`;
      progress.textContent = `${state.cards.length} of ${state.cards.length}`; armLearningTimer(); saveState(); return;
    }
    state.phase = "complete"; card.textContent = state.cards.length ? "Daily study complete." : "All caught up / nothing due."; progress.textContent = `${state.cards.length} of ${state.cards.length}`; saveState(); return;
  }
  card.textContent = revealed ? "Review the word and continue when ready." : "Listen, then choose.";
  term.textContent = word.term; tier.textContent = formatTier(word); transcript.textContent = word.transcript || "Transcript is unavailable.";
  if (!revealed) setActions(false);
  else { setActions(true); continueButton.disabled = false; }
  presentAudio(word, revealed);
  armLearningTimer();
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
  let next = 0;
  const load = async () => {
    while (next < urls.length) {
      const url = urls[next++];
      try {
        const response = await window.fetch(url);
        if (audioCacheEpoch !== epoch || state.sessionId !== sessionId || response.status !== 200) throw new Error("audio unavailable");
        await cache.put(new Request(url, { method: "GET" }), response.clone());
        audioCacheState.cached += 1;
      } catch (_) { audioCacheState.failed += 1; }
    }
  };
  await Promise.all(Array.from({ length: Math.min(AUDIO_PRELOAD_CONCURRENCY, urls.length) }, load));
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
  syncFlight = (async () => {
    const events = await allEvents();
    if (!events.length || !navigator.onLine) return;
    let unresolved = false;
    try {
      for (let offset = 0; offset < events.length; offset += SYNC_BATCH_SIZE) {
        const response = await apiFetch("/api/v1/review-events/batch", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify(events.slice(offset, offset + SYNC_BATCH_SIZE).map((event) => ({
            id: event.id, word_id: event.word_id, action: event.action, reviewed_at: event.reviewed_at,
          }))),
        });
        if (!response.ok) { showStatus("Review sync will retry when online."); return; }
        const payload = await response.json();
        const eventById = new Map(events.slice(offset, offset + SYNC_BATCH_SIZE).map((event) => [event.id, event]));
        for (const result of payload.results) {
          const event = eventById.get(result.id);
          if (!event || !["applied", "idempotent"].includes(result.status)) continue;
          const dueAt = result.due_at ? Date.parse(result.due_at) : Number.NaN;
          if (event.action === "unknown" && Number.isFinite(dueAt)) {
            const queued = state.learningQueue.find((item) => item.eventId === event.id);
            if (queued) queued.due_at = dueAt;
          }
        }
        await saveState();
        if (state.phase === "waiting") render(); else armLearningTimer();
        await Promise.all(payload.acknowledged.map(removeEvent));
        unresolved ||= payload.results.some((result) => result.status === "conflict" || result.status === "missing");
      }
      if (unresolved) showStatus("Some review updates need attention and remain on this device.");
    } catch (_) { showStatus("Review sync will retry when online."); }
  })();
  try { await syncFlight; } finally { syncFlight = null; }
}
async function answer(action) {
  if (busy || !currentWord() || state.phase !== "listening") return;
  busy = true; setActions(true); audio.pause();
  const word = currentWord(), learning = currentLearning();
  state.event = { id: crypto.randomUUID(), word_id: word.id, action, reviewed_at: new Date(nowMs()).toISOString(), wasLearning: Boolean(learning), learningEntry: learning || null };
  state.undoDeadline = nowMs() + 5000;
  await addEvent(state.event);
  if (action === "known") {
    if (learning) {
      state.learningQueue = state.learningQueue.filter((item) => item.word.id !== word.id);
      state.revealedLearningWordId = null;
    }
    else state.current += 1;
    state.phase = "listening";
  } else {
    state.learningQueue = state.learningQueue.filter((item) => item.word.id !== word.id);
    state.learningQueue.push({ eventId: state.event.id, word, due_at: nowMs() + state.learningStepSeconds * 1000 });
    state.revealedLearningWordId = learning ? word.id : null;
    state.phase = "revealed";
  }
  await saveState(); armUndoTimer(); render(); if (!currentWord()) await saveState(); busy = false; sync();
}
async function continueStudy() { if (busy || state.phase !== "revealed") return; busy = true; if (state.current < state.cards.length) state.current += 1; state.revealedLearningWordId = null; state.phase = "listening"; await saveState(); render(); if (!currentWord()) await saveState(); busy = false; }
async function undoLast() {
  if (busy || !state.event || state.undoDeadline <= nowMs()) return;
  busy = true; setActions(true); const event = state.event; await sync();
  const pending = (await allEvents()).some((item) => item.id === event.id);
  try {
    if (pending) await removeEvent(event.id);
    else { const response = await apiFetch(`/api/v1/review-events/${event.id}/void`, { method: "POST" }); if (!response.ok) throw new Error("void failed"); }
    if (event.action === "unknown") {
      state.learningQueue = state.learningQueue.filter((item) => item.eventId !== event.id);
      if (event.wasLearning && event.learningEntry) state.learningQueue.push(event.learningEntry);
    }
    if (event.action === "known" && event.learningEntry) state.learningQueue.push(event.learningEntry);
    state.current = Math.max(0, state.current - (!event.wasLearning && state.phase === "listening" ? 1 : 0)); state.revealedLearningWordId = null; state.phase = "listening"; state.event = null; state.undoDeadline = 0; clearUndoTimer(); await saveState(); render();
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
  state = { sessionId: null, cards: [], current: 0, phase: "unavailable", event: null, undoDeadline: 0, learningQueue: [], learningStepSeconds: 600, revealedLearningWordId: null };
  audioCacheState = { sessionId: null, expected: 0, cached: 0, ready: false, failed: 0 };
  clearUndoTimer(); clearLearningTimer(); render(); showStatus("Private study data was cleared.");
}

async function boot() {
  let saved = null;
  try { saved = await getState(); } catch (_) { showStatus("Local study storage is unavailable."); }
  if (saved && saved.cards && saved.phase !== "complete") {
    state = { ...state, ...saved, learningQueue: Array.isArray(saved.learningQueue) ? saved.learningQueue.filter((item) => item && item.word && Number.isFinite(item.due_at)) : [], learningStepSeconds: Number.isFinite(saved.learningStepSeconds) ? saved.learningStepSeconds : 600 };
    state.current = Number.isInteger(saved.current) ? Math.max(0, Math.min(saved.current, state.cards.length)) : 0;
    state.revealedLearningWordId = typeof saved.revealedLearningWordId === "string" ? saved.revealedLearningWordId : null;
    try { await configureAudioCache(false); } catch (_) { showStatus("Audio cache is unavailable; listening still works online."); }
  } else {
    try {
      const response = await apiFetch("/api/v1/sessions?mode=daily");
      if (!response.ok) throw new Error("session unavailable");
      const session = await response.json();
      state = { sessionId: session.id, cards: session.items, current: 0, phase: "listening", event: null, undoDeadline: 0, learningQueue: [], learningStepSeconds: session.learning_step_seconds, revealedLearningWordId: null };
      try { await configureAudioCache(true); } catch (_) { showStatus("Audio cache is unavailable; listening still works online."); }
      await saveState();
    } catch (_) {
      state = saved || state;
      if (!saved) state = { ...state, phase: "unavailable" };
      render(); showStatus("Reconnect to start a new study session.");
      return;
    }
  }
  armUndoTimer(); armLearningTimer(); render(); startPreload().catch(() => showStatus("Some audio could not be saved for offline listening.")); sync();
}

async function logout() {
  logoutButton.disabled = true;
  try { await window.clearPrivateCaches(); } catch (_) { showStatus("Private study data could not be cleared. Please try again."); logoutButton.disabled = false; return; }
  try {
    const response = await apiFetch("/auth/logout", { method: "POST", redirectOn401: false });
    if (response.status === 204 || response.status === 401) { location.assign("/login"); return; }
    showStatus("Reconnect to finish signing out.");
  } catch (_) { showStatus("Reconnect to finish signing out."); }
  logoutButton.disabled = false;
}
window.clearPrivateCaches = clearPrivateCaches;
window.__pwa = { getAudioCacheState: () => ({ ...audioCacheState }), getState: () => ({ ...state }), render };
known.addEventListener("click", () => answer("known")); unknown.addEventListener("click", () => answer("unknown")); continueButton.addEventListener("click", continueStudy); undoButton.addEventListener("click", undoLast); startButton.addEventListener("click", play); logoutButton.addEventListener("click", logout); addEventListener("online", sync);
if ("serviceWorker" in navigator) window.__pwa.serviceWorkerRegistration = navigator.serviceWorker.register("/sw.js").then(() => "ready", (error) => String(error));
boot().catch(() => { render(); showStatus("Reconnect to start a new study session."); });
