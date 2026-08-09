const DB_NAME = "english-vocab-trainer";
const SESSION_KEY = "active-session";
let cards = [], current = 0, undo = null, revealed = false, busy = false;
const card = document.querySelector("#card");
const audio = document.querySelector("#audio");
const transcript = document.querySelector("#transcript");
const known = document.querySelector("#known");
const unknown = document.querySelector("#unknown");
const continueButton = document.querySelector("#continue");
const undoButton = document.querySelector("#undo");
const startButton = document.querySelector("#start");
const progress = document.querySelector("#progress");

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore("events", { keyPath: "id" });
      request.result.createObjectStore("state", { keyPath: "key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function getState(key) { const db = await openDb(); return new Promise((resolve) => { const q = db.transaction("state").objectStore("state").get(key); q.onsuccess = () => resolve(q.result && q.result.value); }); }
async function putState(key, value) { const db = await openDb(); db.transaction("state", "readwrite").objectStore("state").put({ key, value }); }
async function addEvent(event) { const db = await openDb(); db.transaction("events", "readwrite").objectStore("events").put(event); }
async function removeEvent(id) { const db = await openDb(); db.transaction("events", "readwrite").objectStore("events").delete(id); }
async function allEvents() { const db = await openDb(); return new Promise((resolve) => { const q = db.transaction("events").objectStore("events").getAll(); q.onsuccess = () => resolve(q.result); }); }

async function sync() {
  const events = await allEvents();
  if (!events.length || !navigator.onLine) return;
  try {
    const response = await fetch("/api/v1/review-events/batch", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(events) });
    if (!response.ok) return;
    const payload = await response.json();
    await Promise.all(payload.acknowledged.map(removeEvent));
  } catch (_) { /* retain unsynchronised events for retry */ }
}
function preload() { cards.forEach((word) => fetch(word.audio_url).then((response) => caches.open("m1-audio").then((cache) => cache.put(word.audio_url, response))).catch(() => {})); }
function setButtons(disabled) { known.disabled = disabled; unknown.disabled = disabled; }
function play() { audio.play().then(() => startButton.hidden = true).catch(() => { startButton.hidden = false; }); }
function render() {
  const word = cards[current];
  undoButton.hidden = !undo;
  progress.textContent = `${Math.min(current + 1, cards.length)} of ${cards.length}`;
  revealed = false; continueButton.hidden = true; transcript.hidden = true;
  if (!word) { card.textContent = "Daily study complete."; audio.removeAttribute("src"); setButtons(true); progress.textContent = `${cards.length} of ${cards.length}`; return; }
  card.textContent = "Listen, then choose.";
  audio.src = word.audio_url; audio.currentTime = 0; setButtons(false); play();
}
async function persistSession() { await putState(SESSION_KEY, { cards, current }); }
async function answer(action) {
  if (busy || !cards[current]) return;
  busy = true; setButtons(true); audio.pause();
  const event = { id: crypto.randomUUID(), word_id: cards[current].id, action, reviewed_at: new Date().toISOString() };
  await addEvent(event);
  undo = { event, index: current };
  if (action === "unknown") {
    revealed = true; transcript.textContent = cards[current].transcript || "Transcript is unavailable.";
    transcript.hidden = false; continueButton.hidden = false; audio.currentTime = 0; play();
  } else {
    current += 1; await persistSession(); render();
  }
  setTimeout(() => { undo = null; undoButton.hidden = true; }, 5000);
  busy = false;
  if (action !== "unknown") setButtons(false);
  sync();
}
async function nextAfterUnknown() { if (!revealed) return; current += 1; await persistSession(); render(); }
async function undoLast() {
  if (!undo) return;
  const item = undo; undo = null; undoButton.hidden = true; audio.pause();
  const pending = (await allEvents()).some((event) => event.id === item.event.id);
  if (pending) await removeEvent(item.event.id);
  else { try { await fetch(`/api/v1/review-events/${item.event.id}/void`, { method: "POST" }); } catch (_) { await addEvent(item.event); } }
  current = item.index; await persistSession(); render();
}
async function boot() {
  const saved = await getState(SESSION_KEY);
  if (saved && saved.cards && saved.current < saved.cards.length) { cards = saved.cards; current = saved.current; }
  else { const response = await fetch("/api/v1/sessions?mode=daily"); const session = await response.json(); cards = session.items; current = 0; await persistSession(); }
  preload(); render(); sync();
}
known.addEventListener("click", () => answer("known"));
unknown.addEventListener("click", () => answer("unknown"));
continueButton.addEventListener("click", nextAfterUnknown);
undoButton.addEventListener("click", undoLast);
startButton.addEventListener("click", play);
addEventListener("online", sync);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/sw.js");
boot();
