const DB_NAME = "english-vocab-trainer";
const STATE_KEY = "active-session";
let state = { sessionId: null, cards: [], current: 0, phase: "listening", event: null, undoDeadline: 0 };
let busy = false, syncFlight = null, undoTimer = null;
const $ = (selector) => document.querySelector(selector);
const card = $("#card"), term = $("#term"), transcript = $("#transcript"), audio = $("#audio");
const known = $("#known"), unknown = $("#unknown"), continueButton = $("#continue"), undoButton = $("#undo");
const startButton = $("#start"), progress = $("#progress"), status = $("#status");

function openDb() { return new Promise((resolve, reject) => { const request = indexedDB.open(DB_NAME, 1); request.onupgradeneeded = () => { request.result.createObjectStore("events", { keyPath: "id" }); request.result.createObjectStore("state", { keyPath: "key" }); }; request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
async function transaction(storeName, mode, action) { const db = await openDb(); return new Promise((resolve, reject) => { const tx = db.transaction(storeName, mode); let value; tx.oncomplete = () => resolve(value); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error); value = action(tx.objectStore(storeName)); }); }
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
  progress.textContent = `${Math.min(state.current + 1, state.cards.length)} of ${state.cards.length}`;
  undoButton.hidden = !state.event || state.undoDeadline <= Date.now();
  term.hidden = !revealed; transcript.hidden = !revealed; continueButton.hidden = !revealed;
  if (!word) { state.phase = "complete"; card.textContent = "Daily study complete."; term.hidden = true; transcript.hidden = true; audio.removeAttribute("src"); setActions(true); progress.textContent = `${state.cards.length} of ${state.cards.length}`; saveState(); return; }
  card.textContent = revealed ? "Review the word and continue when ready." : "Listen, then choose.";
  term.textContent = word.term; transcript.textContent = word.transcript || "Transcript is unavailable.";
  if (!revealed) { audio.src = word.audio_url; audio.currentTime = 0; setActions(false); play(); }
  else { setActions(true); continueButton.disabled = false; audio.currentTime = 0; play(); }
}
async function sync() {
  if (syncFlight) return syncFlight;
  syncFlight = (async () => { const events = await allEvents(); if (!events.length || !navigator.onLine) return; try { const response = await fetch("/api/v1/review-events/batch", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(events) }); if (!response.ok) { showStatus("Review sync will retry when online."); return; } const payload = await response.json(); await Promise.all(payload.acknowledged.map(removeEvent)); } catch (_) { showStatus("Review sync will retry when online."); } })();
  try { await syncFlight; } finally { syncFlight = null; }
}
function preload() { state.cards.forEach((word) => fetch(word.audio_url).then((response) => { if (response.status === 200) return caches.open("m1-audio-v2").then((cache) => cache.put(word.audio_url, response)); return undefined; }).catch(() => {})); }
async function answer(action) {
  if (busy || !currentWord() || state.phase !== "listening") return;
  busy = true; setActions(true); audio.pause();
  state.event = { id: crypto.randomUUID(), word_id: currentWord().id, action, reviewed_at: new Date().toISOString() };
  state.undoDeadline = Date.now() + 5000;
  await addEvent(state.event);
  if (action === "known") { state.current += 1; state.phase = "listening"; } else { state.phase = "revealed"; }
  await saveState(); armUndoTimer(); render(); busy = false; sync();
}
async function continueStudy() { if (busy || state.phase !== "revealed") return; busy = true; state.current += 1; state.phase = "listening"; await saveState(); render(); busy = false; }
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
async function boot() {
  const saved = await getState();
  if (saved && saved.cards && saved.phase !== "complete") state = saved;
  else { const response = await fetch("/api/v1/sessions?mode=daily"); const session = await response.json(); state = { sessionId: session.id, cards: session.items, current: 0, phase: "listening", event: null, undoDeadline: 0 }; await saveState(); }
  preload(); armUndoTimer(); render(); sync();
}
known.addEventListener("click", () => answer("known")); unknown.addEventListener("click", () => answer("unknown")); continueButton.addEventListener("click", continueStudy); undoButton.addEventListener("click", undoLast); startButton.addEventListener("click", play); addEventListener("online", sync);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/sw.js"); boot();
