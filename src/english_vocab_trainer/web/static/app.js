const DB_NAME = "english-vocab-trainer";
const DB_VERSION = 2;
const STATE_KEY = "active-set";
const STATE_FORMAT = 1;
const AUDIO_CACHE = "pwa-private-audio-v4";
const AUDIO_PRELOAD_CONCURRENCY = 4;
const SYNC_BATCH_SIZE = 100;

const emptyState = () => ({
  format: STATE_FORMAT,
  cards: [],
  current: 0,
  active: null,
  phase: "unavailable",
  event: null,
  learningQueue: [],
  learningStepSeconds: 600,
  finished: false,
});

let state = emptyState();
let busy = false;
let syncFlight = null;
let syncProblem = null;
let learningTimer = null;
let audioCacheState = { expected: 0, cached: 0, ready: false, failed: 0 };
let audioCacheEpoch = 0;
let preloadController = null;
let preloadFlight = null;
let audioPresentation = null;

const $ = (selector) => document.querySelector(selector);
const card = $("#card");
const term = $("#term");
const tier = $("#tier");
const transcript = $("#transcript");
const audio = $("#audio");
const replayButton = $("#replay");
const showAnswerButton = $("#show-answer");
const knownButton = $("#known");
const unknownButton = $("#unknown");
const nextSetButton = $("#next-set");
const finishButton = $("#finish");
const excludeButton = $("#exclude");
const undoButton = $("#undo");
const discardFailedButton = $("#discard-failed");
const manageExclusionsButton = $("#manage-exclusions");
const exclusions = $("#exclusions");
const exclusionsList = $("#exclusions-list");
const exclusionsEmpty = $("#exclusions-empty");
const progress = $("#progress");
const networkStatus = $("#network-status");
const syncStatus = $("#sync-status");
const cacheStatus = $("#cache-status");
const notice = $("#notice");
const logoutButton = $("#logout");

function csrfToken() {
  return document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith("__Host-vocab-csrf=") || value.startsWith("vocab-csrf="))
    ?.split("=")
    .slice(1)
    .join("=");
}

async function apiFetch(url, options = {}) {
  const { redirectOn401 = true, ...requestOptions } = options;
  const method = (requestOptions.method || "GET").toUpperCase();
  const headers = new Headers(requestOptions.headers || {});
  const token = csrfToken();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && token) {
    headers.set("X-CSRF-Token", token);
  }
  const response = await fetch(url, { ...requestOptions, headers });
  if (redirectOn401 && response.status === 401 && navigator.onLine) {
    location.assign("/login");
  }
  return response;
}

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("events")) {
        request.result.createObjectStore("events", { keyPath: "id" });
      }
      if (!request.result.objectStoreNames.contains("failedEvents")) {
        request.result.createObjectStore("failedEvents", { keyPath: "id" });
      }
      if (!request.result.objectStoreNames.contains("state")) {
        request.result.createObjectStore("state", { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function transaction(storeNames, mode, action) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const names = Array.isArray(storeNames) ? storeNames : [storeNames];
    const tx = db.transaction(names, mode);
    let value;
    tx.oncomplete = () => {
      db.close();
      resolve(value);
    };
    tx.onerror = () => {
      db.close();
      reject(tx.error);
    };
    tx.onabort = () => {
      db.close();
      reject(tx.error);
    };
    value = action(tx);
  });
}

function requestValue(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

const getState = () => transaction("state", "readonly", (tx) => requestValue(tx.objectStore("state").get(STATE_KEY)).then((record) => record?.value));
const saveState = () => transaction("state", "readwrite", (tx) => tx.objectStore("state").put({ key: STATE_KEY, value: state }));
const addEvent = (event) => transaction("events", "readwrite", (tx) => tx.objectStore("events").put(event));
const removeEvent = (id) => transaction("events", "readwrite", (tx) => tx.objectStore("events").delete(id));
const allEvents = () => transaction("events", "readonly", (tx) => requestValue(tx.objectStore("events").getAll()));
const allFailedEvents = () => transaction("failedEvents", "readonly", (tx) => requestValue(tx.objectStore("failedEvents").getAll()));
const removeFailedEvent = (id) => transaction("failedEvents", "readwrite", (tx) => tx.objectStore("failedEvents").delete(id));

function quarantineEvent(event, terminalStatus) {
  return transaction(["events", "failedEvents"], "readwrite", (tx) => {
    tx.objectStore("failedEvents").put({ ...event, terminalStatus });
    tx.objectStore("events").delete(event.id);
  });
}

function nowMs() {
  return window.__pwaTestClock?.now?.() ?? Date.now();
}

function validState(saved) {
  return saved?.format === STATE_FORMAT && Array.isArray(saved.cards) && Array.isArray(saved.learningQueue);
}

function clone(value) {
  return structuredClone(value);
}

function dueLearning() {
  return state.learningQueue
    .filter((item) => item.due_at <= nowMs())
    .sort((left, right) => left.due_at - right.due_at)[0] || null;
}

function chooseNextCard() {
  if (state.finished || state.active) return;
  const learning = dueLearning();
  if (learning) {
    state.active = { kind: "learning", word: learning.word };
    state.phase = "listening";
    return;
  }
  if (state.current < state.cards.length) {
    state.active = { kind: "initial", word: state.cards[state.current] };
    state.phase = "listening";
  }
}

function remainingInitial() {
  return Math.max(0, state.cards.length - state.current);
}

function formatTier(word) {
  const name = word.tier === "upper" ? "Upper" : "Ultra";
  return `${name} · ${word.level == null ? "level unknown" : `SVL ${word.level}`}`;
}

function updateNetworkStatus() {
  networkStatus.textContent = navigator.onLine ? "Network: online" : "Network: offline";
}

function updateCacheStatus() {
  if (!audioCacheState.expected) {
    cacheStatus.textContent = "Audio cache: no cards";
  } else if (!audioCacheState.ready) {
    cacheStatus.textContent = `Audio cache: ${audioCacheState.cached}/${audioCacheState.expected}`;
  } else if (audioCacheState.failed) {
    cacheStatus.textContent = `Audio cache: ${audioCacheState.failed} unavailable`;
  } else {
    cacheStatus.textContent = "Audio cache: ready offline";
  }
}

async function refreshSyncStatus() {
  const [pending, failed] = await Promise.all([allEvents(), allFailedEvents()]);
  discardFailedButton.hidden = failed.length === 0;
  if (failed.length) {
    syncStatus.textContent = `Sync: ${failed.length} review${failed.length === 1 ? "" : "s"} need attention and will not be retried.`;
  } else if (!navigator.onLine && pending.length) {
    syncStatus.textContent = `Sync: offline · ${pending.length} waiting.`;
  } else if (syncProblem) {
    syncStatus.textContent = `Sync: ${syncProblem}`;
  } else if (pending.length) {
    syncStatus.textContent = `Sync: ${pending.length} waiting.`;
  } else {
    syncStatus.textContent = "Sync: all reviews saved.";
  }
  return { pending, failed };
}

function playAudio(showFailure = true) {
  if (!state.active) return;
  audio.play().catch(() => {
    if (showFailure) notice.textContent = "Audio could not start. Try Replay audio again.";
  });
}

function presentAudio() {
  if (!state.active) {
    audioPresentation = null;
    audio.removeAttribute("src");
    return;
  }
  const identity = state.active.word.id;
  if (audioPresentation === identity) return;
  audioPresentation = identity;
  audio.src = state.active.word.audio_url;
  audio.currentTime = 0;
  playAudio(false);
}

function clearLearningTimer() {
  if (learningTimer) clearTimeout(learningTimer);
  learningTimer = null;
}

function armLearningTimer() {
  clearLearningTimer();
  if (state.finished || state.active || !state.learningQueue.length) return;
  const earliest = Math.min(...state.learningQueue.map((item) => item.due_at));
  const delay = Math.max(0, earliest - nowMs());
  learningTimer = setTimeout(wakeForLearning, delay);
}

async function wakeForLearning() {
  chooseNextCard();
  await saveState();
  render();
}

function setVisible(button, visible) {
  button.hidden = !visible;
  button.disabled = busy;
}

function render() {
  clearLearningTimer();
  updateNetworkStatus();
  updateCacheStatus();
  notice.textContent = notice.textContent || "";
  const active = state.active;
  const listening = Boolean(active) && state.phase === "listening";
  const answering = Boolean(active) && state.phase === "answer";
  progress.textContent = `Initial: ${remainingInitial()} remaining · Relearning: ${state.learningQueue.length}`;
  setVisible(replayButton, Boolean(active));
  setVisible(showAnswerButton, listening);
  setVisible(knownButton, answering);
  setVisible(unknownButton, answering);
  setVisible(excludeButton, Boolean(active));
  setVisible(undoButton, Boolean(state.event));
  setVisible(finishButton, !active && !state.finished && state.learningQueue.length > 0);
  setVisible(nextSetButton, !active && (state.finished || state.learningQueue.length === 0));

  term.hidden = !answering;
  tier.hidden = !answering;
  transcript.hidden = !answering;
  if (active) {
    card.textContent = listening ? "Listen to the word, then show the answer." : "Review the answer, then choose Known or Unknown.";
    term.textContent = active.word.term;
    tier.textContent = formatTier(active.word);
    transcript.textContent = active.word.transcript || "Transcript is unavailable.";
    presentAudio();
    return;
  }
  presentAudio();
  if (state.phase === "unavailable") {
    card.textContent = "Reconnect to start a study set.";
    progress.textContent = "No set loaded";
    nextSetButton.hidden = true;
    return;
  }
  if (state.finished) {
    card.textContent = "Finished for now. Start a new set when you are ready.";
    return;
  }
  if (state.learningQueue.length) {
    const earliest = Math.min(...state.learningQueue.map((item) => item.due_at));
    const seconds = Math.max(0, Math.ceil((earliest - nowMs()) / 1000));
    card.textContent = `Waiting for relearning: ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.`;
    armLearningTimer();
    return;
  }
  card.textContent = state.cards.length ? "Set complete." : "All caught up. Nothing is due.";
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
      channel.port1.onmessage = (event) => finish(Boolean(event.data?.ok));
      try {
        worker.postMessage(data, [channel.port2]);
      } catch (_) {
        finish(false);
      }
    }).catch(() => finish(false));
  });
}

function audioUrls() {
  return [...new Set([
    ...state.cards.map((word) => word.audio_url),
    ...state.learningQueue.map((item) => item.word.audio_url),
  ])];
}

async function cancelPreload() {
  audioCacheEpoch += 1;
  preloadController?.abort();
  const pending = preloadFlight;
  if (pending) await pending.catch(() => undefined);
}

async function configureAudioCache(rotate) {
  if (rotate) await cancelPreload();
  const urls = audioUrls().map((url) => new URL(url, location.origin).href);
  if (rotate) await caches.delete(AUDIO_CACHE);
  const cache = await caches.open(AUDIO_CACHE);
  const expected = new Set(urls);
  await Promise.all(
    (await cache.keys())
      .filter((request) => !expected.has(request.url))
      .map((request) => cache.delete(request)),
  );
  await sendWorkerMessage({ type: "set-active-audio", urls });
}

async function preload(signal, epoch) {
  const urls = audioUrls();
  audioCacheState = { expected: urls.length, cached: 0, ready: false, failed: 0 };
  updateCacheStatus();
  const cache = await caches.open(AUDIO_CACHE);
  let next = 0;
  const load = async () => {
    while (next < urls.length && !signal.aborted) {
      const url = urls[next++];
      try {
        const response = await window.fetch(url, { signal });
        if (signal.aborted || audioCacheEpoch !== epoch) return;
        if (response.status !== 200) throw new Error("audio unavailable");
        await cache.put(new Request(url, { method: "GET" }), response.clone());
        audioCacheState.cached += 1;
      } catch (error) {
        if (signal.aborted || error?.name === "AbortError") return;
        audioCacheState.failed += 1;
      }
      updateCacheStatus();
    }
  };
  await Promise.all(Array.from({ length: Math.min(AUDIO_PRELOAD_CONCURRENCY, urls.length) }, load));
  if (!signal.aborted && audioCacheEpoch === epoch) {
    audioCacheState.ready = true;
    updateCacheStatus();
  }
}

function startPreload() {
  preloadController?.abort();
  const controller = new AbortController();
  preloadController = controller;
  const epoch = audioCacheEpoch;
  const flight = preload(controller.signal, epoch);
  preloadFlight = flight;
  flight.finally(() => {
    if (preloadFlight === flight) preloadFlight = null;
    if (preloadController === controller) preloadController = null;
  });
  return flight;
}

async function sync() {
  if (syncFlight) return syncFlight;
  syncFlight = (async () => {
    const events = await allEvents();
    if (!events.length) {
      syncProblem = null;
      await refreshSyncStatus();
      return;
    }
    if (!navigator.onLine) {
      await refreshSyncStatus();
      return;
    }
    try {
      for (let offset = 0; offset < events.length; offset += SYNC_BATCH_SIZE) {
        const chunk = events.slice(offset, offset + SYNC_BATCH_SIZE);
        const response = await apiFetch("/api/v1/review-events/batch", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(chunk.map((event) => ({
            id: event.id,
            word_id: event.word_id,
            action: event.action,
            reviewed_at: event.reviewed_at,
          }))),
        });
        if (!response.ok) {
          syncProblem = `server error (HTTP ${response.status}); reviews kept for retry.`;
          await refreshSyncStatus();
          return;
        }
        const payload = await response.json();
        const eventById = new Map(chunk.map((event) => [event.id, event]));
        for (const result of payload.results) {
          const event = eventById.get(result.id);
          if (!event) continue;
          if (["conflict", "missing"].includes(result.status)) {
            await quarantineEvent(event, result.status);
            continue;
          }
          if (["applied", "idempotent", "voided"].includes(result.status)) {
            if (event.action === "unknown" && result.due_at) {
              const dueAt = Date.parse(result.due_at);
              const queued = state.learningQueue.find((item) => item.eventId === event.id);
              if (queued && Number.isFinite(dueAt)) queued.due_at = dueAt;
            }
            await removeEvent(event.id);
          }
        }
        await saveState();
      }
      syncProblem = null;
      render();
      await refreshSyncStatus();
    } catch (_) {
      syncProblem = navigator.onLine
        ? "network error; reviews kept for retry."
        : null;
      await refreshSyncStatus();
    }
  })();
  try {
    await syncFlight;
  } finally {
    syncFlight = null;
  }
}

async function showAnswer() {
  if (busy || !state.active || state.phase !== "listening") return;
  state.phase = "answer";
  await saveState();
  render();
}

async function answer(action) {
  if (busy || !state.active || state.phase !== "answer") return;
  busy = true;
  render();
  audio.pause();
  const active = clone(state.active);
  const event = {
    id: crypto.randomUUID(),
    word_id: active.word.id,
    action,
    reviewed_at: new Date(nowMs()).toISOString(),
    previousCurrent: state.current,
    previousActive: active,
    previousLearningQueue: clone(state.learningQueue),
  };
  await addEvent(event);
  if (active.kind === "initial") state.current += 1;
  state.learningQueue = state.learningQueue.filter((item) => item.word.id !== active.word.id);
  if (action === "unknown") {
    state.learningQueue.push({
      eventId: event.id,
      word: active.word,
      due_at: nowMs() + state.learningStepSeconds * 1000,
    });
  }
  state.event = event;
  state.active = null;
  state.phase = "waiting";
  chooseNextCard();
  await saveState();
  busy = false;
  render();
  await refreshSyncStatus();
  void sync();
}

async function undoLast() {
  if (busy || !state.event) return;
  busy = true;
  render();
  const event = state.event;
  await sync();
  const [pending, failed] = await Promise.all([allEvents(), allFailedEvents()]);
  try {
    if (pending.some((item) => item.id === event.id)) {
      await removeEvent(event.id);
    } else if (failed.some((item) => item.id === event.id)) {
      await removeFailedEvent(event.id);
    } else {
      const response = await apiFetch(`/api/v1/review-events/${event.id}/void`, { method: "POST" });
      if (!response.ok) throw new Error("void failed");
    }
    state.current = event.previousCurrent;
    state.active = event.previousActive;
    state.learningQueue = event.previousLearningQueue;
    state.phase = "answer";
    state.finished = false;
    state.event = null;
    await saveState();
    notice.textContent = "The last answer was undone.";
  } catch (_) {
    notice.textContent = "Undo could not be completed. Please try again.";
  } finally {
    busy = false;
    render();
    await refreshSyncStatus();
  }
}

async function loadSet() {
  if (busy || !navigator.onLine) {
    notice.textContent = "Reconnect to load a new set.";
    return;
  }
  busy = true;
  render();
  try {
    await sync();
    const response = await apiFetch("/api/v1/sets");
    if (!response.ok) throw new Error("set unavailable");
    const payload = await response.json();
    state = {
      ...emptyState(),
      cards: payload.items,
      phase: "waiting",
      learningStepSeconds: payload.learning_step_seconds,
    };
    chooseNextCard();
    await configureAudioCache(true);
    await saveState();
    notice.textContent = "";
    startPreload().catch(() => undefined);
  } catch (_) {
    notice.textContent = "A new set could not be loaded. Please try again.";
  } finally {
    busy = false;
    render();
  }
}

async function finishForNow() {
  if (busy || state.active) return;
  state.finished = true;
  await saveState();
  render();
}

async function excludeActiveWord() {
  if (busy || !state.active) return;
  if (!navigator.onLine) {
    notice.textContent = "Reconnect before suspending a word.";
    return;
  }
  busy = true;
  render();
  const active = state.active;
  try {
    const response = await apiFetch(`/api/v1/exclusions/${encodeURIComponent(active.word.id)}`, { method: "POST" });
    if (!response.ok) throw new Error("suspend failed");
    if (active.kind === "initial") state.cards.splice(state.current, 1);
    state.learningQueue = state.learningQueue.filter((item) => item.word.id !== active.word.id);
    state.active = null;
    state.phase = "waiting";
    chooseNextCard();
    await saveState();
    await configureAudioCache(false);
    notice.textContent = `${active.word.term} was suspended.`;
    if (!exclusions.hidden) await loadExclusions();
  } catch (_) {
    notice.textContent = "The word could not be suspended. Please try again.";
  } finally {
    busy = false;
    render();
  }
}

async function loadExclusions() {
  exclusionsList.replaceChildren();
  exclusionsEmpty.textContent = "Loading…";
  const response = await apiFetch("/api/v1/exclusions");
  if (!response.ok) throw new Error("suspended words unavailable");
  const payload = await response.json();
  exclusionsEmpty.textContent = payload.items.length ? "" : "No suspended words.";
  for (const word of payload.items) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${word.term} (${word.level == null ? "level unknown" : `SVL ${word.level}`})`;
    const restore = document.createElement("button");
    restore.textContent = "Restore";
    restore.addEventListener("click", async () => {
      restore.disabled = true;
      try {
        const restored = await apiFetch(`/api/v1/exclusions/${encodeURIComponent(word.id)}`, { method: "DELETE" });
        if (!restored.ok) throw new Error("restore failed");
        notice.textContent = `${word.term} was restored.`;
        await loadExclusions();
      } catch (_) {
        restore.disabled = false;
        notice.textContent = "The word could not be restored. Please try again.";
      }
    });
    item.append(label, restore);
    exclusionsList.append(item);
  }
}

async function toggleExclusions() {
  exclusions.hidden = !exclusions.hidden;
  manageExclusionsButton.textContent = exclusions.hidden ? "Manage suspended words" : "Close suspended words";
  if (!exclusions.hidden) {
    try {
      await loadExclusions();
    } catch (_) {
      exclusionsEmpty.textContent = "Suspended words could not be loaded.";
    }
  }
}

async function discardFailed() {
  const failed = await allFailedEvents();
  await Promise.all(failed.map((event) => removeFailedEvent(event.id)));
  if (state.event && failed.some((event) => event.id === state.event.id)) {
    state.event = null;
    await saveState();
    render();
  }
  syncProblem = null;
  notice.textContent = "Failed review records were discarded.";
  await refreshSyncStatus();
}

async function clearPrivateCaches() {
  await cancelPreload();
  const workerCleared = await sendWorkerMessage({ type: "clear-private-caches" });
  if (navigator.serviceWorker?.controller && !workerCleared) {
    throw new Error("private cache worker did not confirm the reset");
  }
  await caches.delete(AUDIO_CACHE);
  await transaction(["events", "failedEvents", "state"], "readwrite", (tx) => {
    tx.objectStore("events").clear();
    tx.objectStore("failedEvents").clear();
    tx.objectStore("state").clear();
  });
  state = emptyState();
  audioCacheState = { expected: 0, cached: 0, ready: false, failed: 0 };
  syncProblem = null;
  clearLearningTimer();
  render();
  await refreshSyncStatus();
}

async function logout() {
  if (busy) return;
  if (!navigator.onLine) {
    notice.textContent = "You are offline. Sign-out was not sent and local study data was kept.";
    return;
  }
  const { pending, failed } = await refreshSyncStatus();
  if (pending.length + failed.length > 0 && !window.confirm("Some reviews are not synced. Sign out and remove them from this device?")) {
    notice.textContent = "Sign-out was cancelled; local study data was kept.";
    return;
  }
  busy = true;
  logoutButton.disabled = true;
  await cancelPreload();
  try {
    const response = await apiFetch("/auth/logout", { method: "POST", redirectOn401: false });
    if (![204, 401].includes(response.status)) throw new Error("logout failed");
    try {
      await clearPrivateCaches();
    } catch (_) {
      notice.textContent = "Server sign-out succeeded, but local study data could not be cleared.";
      busy = false;
      logoutButton.disabled = false;
      render();
      return;
    }
    location.assign("/login");
  } catch (_) {
    notice.textContent = "Sign-out did not reach the server. Local study data was kept.";
    busy = false;
    logoutButton.disabled = false;
    render();
  }
}

async function boot() {
  let saved = null;
  try {
    saved = await getState();
  } catch (_) {
    notice.textContent = "Local study storage is unavailable.";
  }
  if (validState(saved)) {
    state = saved;
    if (!state.active && !state.finished) chooseNextCard();
    await saveState();
    try {
      await configureAudioCache(false);
    } catch (_) {
      cacheStatus.textContent = "Audio cache is unavailable; listening still works online.";
    }
    render();
    startPreload().catch(() => undefined);
  } else if (navigator.onLine) {
    await loadSet();
  } else {
    state = emptyState();
    render();
    notice.textContent = "Reconnect to start a study set.";
  }
  await refreshSyncStatus();
  void sync();
}

window.clearPrivateCaches = clearPrivateCaches;
window.__pwa = {
  getAudioCacheState: () => ({ ...audioCacheState }),
  getState: () => clone(state),
  render,
};

replayButton.addEventListener("click", playAudio);
showAnswerButton.addEventListener("click", showAnswer);
knownButton.addEventListener("click", () => answer("known"));
unknownButton.addEventListener("click", () => answer("unknown"));
nextSetButton.addEventListener("click", loadSet);
finishButton.addEventListener("click", finishForNow);
excludeButton.addEventListener("click", excludeActiveWord);
undoButton.addEventListener("click", undoLast);
discardFailedButton.addEventListener("click", discardFailed);
manageExclusionsButton.addEventListener("click", toggleExclusions);
logoutButton.addEventListener("click", logout);
addEventListener("online", () => {
  updateNetworkStatus();
  void sync();
});
addEventListener("offline", () => {
  updateNetworkStatus();
  void refreshSyncStatus();
});
if ("serviceWorker" in navigator) {
  window.__pwa.serviceWorkerRegistration = navigator.serviceWorker.register("/sw.js").then(() => "ready", (error) => String(error));
}
boot().catch(() => {
  state = emptyState();
  render();
  notice.textContent = "Reconnect to start a study set.";
});
