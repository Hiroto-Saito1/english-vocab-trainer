const DB_NAME = "english-vocab-trainer";
const DB_VERSION = 3;
const STATE_KEY = "active-set";
const STATE_FORMAT = 2;
const AUDIO_CACHE = "pwa-private-audio-v5";
const AUDIO_PRELOAD_CONCURRENCY = 4;
const SYNC_BATCH_SIZE = 100;
const STUDY_LOCK = "english-vocab-trainer-active-study";

const emptyState = () => ({
  format: STATE_FORMAT,
  cards: [],
  current: 0,
  active: null,
  phase: "unavailable",
  previous: null,
  learningQueue: [],
  learningStepSeconds: 600,
  finished: false,
  emptyReason: null,
  nextDue: null,
});

let state = emptyState();
let busy = false;
let readOnly = true;
let releaseStudyLock = null;
let queuedStudyLock = null;
let booted = false;
let syncFlight = null;
let syncRequested = false;
let syncProblem = null;
let syncSummary = { pending: [], failed: [] };
let learningTimer = null;
let audioCacheState = { expected: 0, cached: 0, ready: false, failed: 0 };
let audioCacheProblem = null;
let audioCacheEpoch = 0;
let preloadController = null;
let preloadFlight = null;
let audioPresentation = null;
let lastView = null;

const stateChannel = "BroadcastChannel" in window
  ? new BroadcastChannel("english-vocab-trainer-state-v2")
  : null;
const $ = (selector) => document.querySelector(selector);
const card = $("#card");
const term = $("#term");
const tier = $("#tier");
const transcript = $("#transcript");
const audio = $("#audio");
const previousButton = $("#previous");
const replayButton = $("#replay");
const showAnswerButton = $("#show-answer");
const knownButton = $("#known");
const unknownButton = $("#unknown");
const nextSetButton = $("#next-set");
const finishButton = $("#finish");
const retrySyncButton = $("#retry-sync");
const excludeButton = $("#exclude");
const manageExclusionsButton = $("#manage-exclusions");
const wordManagement = $("#word-management");
const exclusionsList = $("#exclusions-list");
const exclusionsEmpty = $("#exclusions-empty");
const syncIssues = $("#sync-issues");
const syncIssuesList = $("#sync-issues-list");
const progress = $("#progress");
const networkStatus = $("#network-status");
const syncStatus = $("#sync-status");
const cacheStatus = $("#cache-status");
const readOnlyStatus = $("#readonly-status");
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
    request.onupgradeneeded = (event) => {
      const db = request.result;
      for (const name of ["events", "failedEvents", "state"]) {
        if (!db.objectStoreNames.contains(name)) {
          db.createObjectStore(name, { keyPath: name === "state" ? "key" : "id" });
        }
      }
      if (event.oldVersion > 0) {
        for (const name of ["events", "failedEvents", "state"]) {
          request.transaction.objectStore(name).clear();
        }
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
    try {
      value = action(tx);
    } catch (error) {
      tx.abort();
      reject(error);
    }
  });
}

function requestValue(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

const getState = () => transaction(
  "state",
  "readonly",
  (tx) => requestValue(tx.objectStore("state").get(STATE_KEY)).then((record) => record?.value),
);
const writeState = (next) => transaction(
  "state",
  "readwrite",
  (tx) => tx.objectStore("state").put({ key: STATE_KEY, value: next }),
);
const allEvents = () => transaction(
  "events", "readonly", (tx) => requestValue(tx.objectStore("events").getAll()),
);
const allFailedEvents = () => transaction(
  "failedEvents", "readonly", (tx) => requestValue(tx.objectStore("failedEvents").getAll()),
);

function validState(saved) {
  return saved?.format === STATE_FORMAT
    && Array.isArray(saved.cards)
    && Array.isArray(saved.learningQueue);
}

function clone(value) {
  return structuredClone(value);
}

function broadcastState() {
  stateChannel?.postMessage({ type: "state-changed" });
}

async function commitState(next) {
  await writeState(next);
  state = next;
  broadcastState();
}

function dueLearning(target) {
  return target.learningQueue
    .filter((item) => item.due_at <= nowMs())
    .sort((left, right) => left.due_at - right.due_at)[0] || null;
}

function chooseNextCard(target) {
  if (target.finished || target.active) return;
  const learning = dueLearning(target);
  if (learning) {
    target.active = { kind: "learning", word: learning.word };
    target.phase = "listening";
    return;
  }
  if (target.current < target.cards.length) {
    target.active = { kind: "initial", word: target.cards[target.current] };
    target.phase = "listening";
  }
}

function nowMs() {
  return window.__pwaTestClock?.now?.() ?? Date.now();
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
  if (audioCacheProblem) {
    cacheStatus.textContent = audioCacheProblem;
  } else if (!audioCacheState.expected) {
    cacheStatus.textContent = "Audio cache: no cards";
  } else if (!audioCacheState.ready) {
    cacheStatus.textContent = `Audio cache: ${audioCacheState.cached}/${audioCacheState.expected}`;
  } else if (audioCacheState.failed) {
    cacheStatus.textContent = `Audio cache: ${audioCacheState.failed} unavailable`;
  } else {
    cacheStatus.textContent = "Audio cache: ready offline";
  }
}

function normalizedEvent(event) {
  return { ...event, intent: event.intent || "review" };
}

function renderSyncIssues(failed) {
  syncIssuesList.replaceChildren();
  syncIssues.hidden = failed.length === 0;
  for (const record of failed) {
    const event = normalizedEvent(record);
    const item = document.createElement("li");
    const detail = document.createElement("p");
    detail.textContent = `${event.word_id}: ${event.action} · ${event.terminalStatus} · ${event.id}`;
    const retry = document.createElement("button");
    retry.textContent = "Retry this review";
    retry.disabled = readOnly;
    retry.addEventListener("click", () => retryFailedEvent(event.id));
    const dismiss = document.createElement("button");
    dismiss.textContent = "Dismiss this review";
    dismiss.disabled = readOnly;
    dismiss.addEventListener("click", () => dismissFailedEvent(event.id));
    item.append(detail, retry, dismiss);
    syncIssuesList.append(item);
  }
}

async function refreshSyncStatus() {
  const [pending, failed] = await Promise.all([allEvents(), allFailedEvents()]);
  syncSummary = { pending, failed };
  retrySyncButton.hidden = pending.length + failed.length === 0 || readOnly;
  renderSyncIssues(failed);
  if (failed.length) {
    syncStatus.textContent = `Sync: ${failed.length} review${failed.length === 1 ? "" : "s"} need attention.`;
  } else if (!navigator.onLine && pending.length) {
    syncStatus.textContent = `Sync: offline · ${pending.length} waiting.`;
  } else if (syncProblem) {
    syncStatus.textContent = `Sync: ${syncProblem}`;
  } else if (pending.length) {
    syncStatus.textContent = `Sync: ${pending.length} waiting.`;
  } else {
    syncStatus.textContent = "Sync: all reviews saved.";
  }
  return syncSummary;
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
  if (!readOnly) playAudio(false);
}

function clearLearningTimer() {
  if (learningTimer) clearTimeout(learningTimer);
  learningTimer = null;
}

function armLearningTimer() {
  clearLearningTimer();
  if (readOnly || state.finished || state.active || !state.learningQueue.length) return;
  const earliest = Math.min(...state.learningQueue.map((item) => item.due_at));
  learningTimer = setTimeout(wakeForLearning, Math.max(0, earliest - nowMs()));
}

async function wakeForLearning() {
  if (readOnly) return;
  const next = clone(state);
  chooseNextCard(next);
  if (next.active) await commitState(next);
  render();
}

function setVisible(button, visible, enabled = true) {
  button.hidden = !visible;
  button.disabled = busy || readOnly || !enabled;
}

function currentView() {
  if (readOnly) return "readonly";
  if (state.active) return state.phase;
  if (!state.finished && state.learningQueue.length) return "waiting";
  return "complete";
}

function focusPrimary(view) {
  const target = {
    listening: showAnswerButton,
    answer: unknownButton,
    waiting: finishButton,
    complete: nextSetButton,
  }[view];
  if (target && !target.hidden && !target.disabled) target.focus();
}

function emptyMessage(syncBlocked) {
  if (syncBlocked) return "Sync is blocked. Review the saved errors or retry before starting the next set.";
  if (state.finished) return "Set complete. Start the next set when you are ready.";
  if (state.emptyReason === "next_due") {
    const next = state.nextDue ? new Date(state.nextDue).toLocaleString() : "later";
    return `All caught up. The next review is due ${next}.`;
  }
  if (state.emptyReason === "all_suspended") {
    return "All words are suspended. Restore a word to study again.";
  }
  if (state.emptyReason === "catalog_empty") {
    return "The catalog is empty. Publish a catalog before studying.";
  }
  return state.cards.length ? "Set complete." : "All caught up. Nothing is due.";
}

function render() {
  clearLearningTimer();
  updateNetworkStatus();
  updateCacheStatus();
  const active = state.active;
  const listening = Boolean(active) && state.phase === "listening";
  const answering = Boolean(active) && state.phase === "answer";
  const waiting = !active && !state.finished && state.learningQueue.length > 0;
  const complete = !active && !waiting;
  const syncBlocked = complete && syncSummary.pending.length + syncSummary.failed.length > 0;
  const view = currentView();

  readOnlyStatus.hidden = !readOnly;
  logoutButton.disabled = busy || readOnly;
  wordManagement.hidden = readOnly;
  progress.textContent = `Initial: ${remainingInitial()} remaining · Relearning: ${state.learningQueue.length}`;
  setVisible(previousButton, Boolean(active) || waiting || complete, Boolean(state.previous));
  setVisible(replayButton, listening);
  setVisible(showAnswerButton, listening);
  setVisible(knownButton, answering);
  setVisible(unknownButton, answering);
  setVisible(finishButton, waiting);
  setVisible(nextSetButton, complete, navigator.onLine && !syncBlocked);
  setVisible(excludeButton, Boolean(active));

  term.hidden = !answering;
  tier.hidden = !answering;
  transcript.hidden = !answering;
  if (active) {
    card.textContent = listening
      ? "Listen to the word, then show the answer."
      : "Review the answer, then choose Known or Unknown.";
    term.textContent = active.word.term;
    tier.textContent = formatTier(active.word);
    transcript.textContent = active.word.transcript || "Transcript is unavailable.";
    presentAudio();
  } else {
    presentAudio();
    if (state.phase === "unavailable") {
      card.textContent = readOnly
        ? "Waiting for the active study tab to load a set."
        : "Reconnect to start a study set.";
      progress.textContent = "No set loaded";
      nextSetButton.hidden = true;
    } else if (waiting) {
      const earliest = Math.min(...state.learningQueue.map((item) => item.due_at));
      const seconds = Math.max(0, Math.ceil((earliest - nowMs()) / 1000));
      card.textContent = `Waiting for relearning: ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.`;
      armLearningTimer();
    } else {
      card.textContent = emptyMessage(syncBlocked);
    }
  }

  const focused = document.activeElement;
  if (focused?.hidden || focused?.closest("[hidden]")) focused.blur();
  if (view !== lastView) {
    lastView = view;
    requestAnimationFrame(() => focusPrimary(view));
  }
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

function audioUrls(target = state) {
  return [...new Set([
    ...target.cards.slice(target.current).map((word) => word.audio_url),
    ...target.learningQueue.map((item) => item.word.audio_url),
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
  audioCacheProblem = null;
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
  flight.catch(() => {
    if (!controller.signal.aborted) {
      audioCacheProblem = "Audio cache is unavailable; listening still works online.";
      updateCacheStatus();
    }
  }).finally(() => {
    if (preloadFlight === flight) preloadFlight = null;
    if (preloadController === controller) preloadController = null;
  });
  return flight;
}

async function quarantineEvent(snapshot, terminalStatus) {
  await transaction(["events", "failedEvents"], "readwrite", async (tx) => {
    const events = tx.objectStore("events");
    const current = await requestValue(events.get(snapshot.id));
    if (!current) return;
    tx.objectStore("failedEvents").put({ ...current, terminalStatus });
    events.delete(snapshot.id);
  });
}

async function removeSyncedEvent(snapshot) {
  await transaction("events", "readwrite", async (tx) => {
    const store = tx.objectStore("events");
    const current = await requestValue(store.get(snapshot.id));
    if ((current?.intent || "review") === snapshot.intent) store.delete(snapshot.id);
  });
}

async function acknowledgeReview(snapshot, result) {
  let updatedState = null;
  await transaction(["events", "state"], "readwrite", async (tx) => {
    const events = tx.objectStore("events");
    const current = await requestValue(events.get(snapshot.id));
    if ((current?.intent || "review") !== snapshot.intent) return;
    if (snapshot.action === "unknown" && result.due_at) {
      const states = tx.objectStore("state");
      const record = await requestValue(states.get(STATE_KEY));
      if (validState(record?.value)) {
        const next = clone(record.value);
        const queued = next.learningQueue.find((item) => item.eventId === snapshot.id);
        const dueAt = Date.parse(result.due_at);
        if (queued && Number.isFinite(dueAt)) {
          queued.due_at = dueAt;
          states.put({ key: STATE_KEY, value: next });
          updatedState = next;
        }
      }
    }
    events.delete(snapshot.id);
  });
  if (updatedState) {
    state = updatedState;
    broadcastState();
  }
}

async function syncChunk(chunk) {
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
    return false;
  }
  const payload = await response.json();
  const resultById = new Map(payload.results.map((result) => [result.id, result]));
  for (const snapshot of chunk) {
    const result = resultById.get(snapshot.id);
    if (!result) {
      syncProblem = "server response omitted a review; it was kept for retry.";
      return false;
    }
    if (["conflict", "missing"].includes(result.status)) {
      await quarantineEvent(snapshot, result.status);
      continue;
    }
    if (snapshot.intent === "void" && result.status !== "voided") {
      const voided = await apiFetch(`/api/v1/review-events/${snapshot.id}/void`, { method: "POST" });
      if (voided.status === 404) {
        await quarantineEvent(snapshot, "missing");
      } else if (voided.status === 204) {
        await removeSyncedEvent(snapshot);
      } else {
        syncProblem = `server error (HTTP ${voided.status}); undo kept for retry.`;
        return false;
      }
    } else if (snapshot.intent === "void") {
      await removeSyncedEvent(snapshot);
    } else {
      await acknowledgeReview(snapshot, result);
    }
  }
  return true;
}

async function sync() {
  if (readOnly) return;
  syncRequested = true;
  if (syncFlight) return syncFlight;
  syncFlight = (async () => {
    while (navigator.onLine) {
      syncRequested = false;
      const events = (await allEvents()).map(normalizedEvent);
      if (!events.length) break;
      let completed = true;
      for (let offset = 0; offset < events.length; offset += SYNC_BATCH_SIZE) {
        if (!await syncChunk(events.slice(offset, offset + SYNC_BATCH_SIZE))) {
          completed = false;
          break;
        }
      }
      if (!completed) break;
    }
    if (!navigator.onLine && (await allEvents()).length) syncProblem = null;
    else if (!(await allEvents()).length) syncProblem = null;
    await refreshSyncStatus();
    render();
    stateChannel?.postMessage({ type: "outbox-changed" });
  })().catch(async () => {
    syncProblem = navigator.onLine ? "network error; reviews kept for retry." : null;
    await refreshSyncStatus();
    render();
    stateChannel?.postMessage({ type: "outbox-changed" });
  });
  try {
    await syncFlight;
  } finally {
    syncFlight = null;
    if (syncRequested && navigator.onLine) void sync();
  }
}

async function showAnswer() {
  if (busy || readOnly || !state.active || state.phase !== "listening") return;
  const next = clone(state);
  next.phase = "answer";
  await commitState(next);
  render();
}

async function answer(action) {
  if (busy || readOnly || !state.active || state.phase !== "answer") return;
  busy = true;
  render();
  audio.pause();
  const active = clone(state.active);
  const event = {
    id: crypto.randomUUID(),
    word_id: active.word.id,
    action,
    reviewed_at: new Date(nowMs()).toISOString(),
    intent: "review",
    previousCurrent: state.current,
    previousActive: active,
    previousLearningQueue: clone(state.learningQueue),
  };
  const next = clone(state);
  if (active.kind === "initial") next.current += 1;
  next.learningQueue = next.learningQueue.filter((item) => item.word.id !== active.word.id);
  if (action === "unknown") {
    next.learningQueue.push({
      eventId: event.id,
      word: active.word,
      due_at: nowMs() + next.learningStepSeconds * 1000,
    });
  }
  next.previous = event;
  next.active = null;
  next.phase = "waiting";
  chooseNextCard(next);
  try {
    await transaction(["events", "state"], "readwrite", (tx) => {
      tx.objectStore("events").put(event);
      tx.objectStore("state").put({ key: STATE_KEY, value: next });
    });
    state = next;
    broadcastState();
    syncRequested = true;
  } catch (_) {
    notice.textContent = "The answer could not be saved locally. Please try again.";
  } finally {
    try {
      await refreshSyncStatus();
    } finally {
      busy = false;
      render();
      void sync();
    }
  }
}

async function previous() {
  if (busy || readOnly || !state.previous) return;
  busy = true;
  render();
  audio.pause();
  const event = clone(state.previous);
  const queued = { ...event, intent: "void" };
  const next = clone(state);
  next.current = event.previousCurrent;
  next.active = event.previousActive;
  next.learningQueue = event.previousLearningQueue;
  next.phase = "listening";
  next.finished = false;
  next.previous = null;
  try {
    await transaction(["events", "failedEvents", "state"], "readwrite", (tx) => {
      tx.objectStore("events").put(queued);
      tx.objectStore("failedEvents").delete(event.id);
      tx.objectStore("state").put({ key: STATE_KEY, value: next });
    });
    state = next;
    lastView = null;
    broadcastState();
    notice.textContent = "The previous answer is being undone. Answer the word again when ready.";
    syncRequested = true;
  } catch (_) {
    notice.textContent = "Previous could not be saved locally. Please try again.";
  } finally {
    busy = false;
    render();
    await refreshSyncStatus();
    void sync();
  }
}

async function retryFailedEvent(id) {
  if (busy || readOnly) return;
  await transaction(["events", "failedEvents"], "readwrite", async (tx) => {
    const failed = tx.objectStore("failedEvents");
    const event = await requestValue(failed.get(id));
    if (!event) return;
    const { terminalStatus: _, ...retry } = event;
    tx.objectStore("events").put(retry);
    failed.delete(id);
  });
  syncProblem = null;
  await refreshSyncStatus();
  render();
  void sync();
}

async function retryAllFailed() {
  if (busy || readOnly) return;
  const failed = await allFailedEvents();
  await Promise.all(failed.map((event) => retryFailedEvent(event.id)));
  void sync();
}

async function dismissFailedEvent(id) {
  if (busy || readOnly) return;
  if (!window.confirm("Dismiss this review from this device? The server history will not be changed.")) return;
  const next = clone(state);
  if (next.previous?.id === id) next.previous = null;
  await transaction(["failedEvents", "state"], "readwrite", (tx) => {
    tx.objectStore("failedEvents").delete(id);
    tx.objectStore("state").put({ key: STATE_KEY, value: next });
  });
  state = next;
  broadcastState();
  await refreshSyncStatus();
  render();
}

async function loadSet() {
  if (busy || readOnly || !navigator.onLine) {
    notice.textContent = readOnly
      ? "Use the active study tab to start a set."
      : "Reconnect to load a new set.";
    return;
  }
  busy = true;
  render();
  try {
    await sync();
    const outstanding = await refreshSyncStatus();
    if (outstanding.pending.length || outstanding.failed.length) {
      notice.textContent = "Resolve or retry saved reviews before starting the next set.";
      return;
    }
    const response = await apiFetch("/api/v1/sets");
    if (!response.ok) throw new Error("set unavailable");
    const payload = await response.json();
    const staged = {
      ...emptyState(),
      cards: payload.items,
      phase: "waiting",
      learningStepSeconds: payload.learning_step_seconds,
      emptyReason: payload.items.length ? null : payload.status,
      nextDue: payload.next_due,
    };
    chooseNextCard(staged);
    await writeState(staged);
    state = staged;
    broadcastState();
    notice.textContent = "";
    render();
    try {
      await configureAudioCache(true);
      void startPreload();
    } catch (_) {
      audioCacheProblem = "Audio cache setup failed; the saved set is still available online.";
      updateCacheStatus();
    }
  } catch (_) {
    notice.textContent = "A new set could not be loaded. Your current set was kept.";
  } finally {
    busy = false;
    render();
  }
}

async function finishForNow() {
  if (busy || readOnly || state.active) return;
  const next = clone(state);
  next.finished = true;
  await commitState(next);
  render();
}

async function excludeActiveWord() {
  if (busy || readOnly || !state.active) return;
  if (!navigator.onLine) {
    notice.textContent = "Reconnect before suspending a word.";
    return;
  }
  busy = true;
  audio.pause();
  await cancelPreload();
  render();
  const active = clone(state.active);
  try {
    const response = await apiFetch(`/api/v1/exclusions/${encodeURIComponent(active.word.id)}`, { method: "POST" });
    if (!response.ok) throw new Error("suspend failed");
    const next = clone(state);
    const removedIndex = next.cards.findIndex((word) => word.id === active.word.id);
    if (removedIndex >= 0) {
      next.cards.splice(removedIndex, 1);
      if (removedIndex < next.current) next.current -= 1;
      if (next.previous && removedIndex < next.previous.previousCurrent) {
        next.previous.previousCurrent -= 1;
      }
    }
    next.learningQueue = next.learningQueue.filter((item) => item.word.id !== active.word.id);
    if (next.previous?.word_id === active.word.id) {
      next.previous = null;
    } else if (next.previous) {
      next.previous.previousLearningQueue = next.previous.previousLearningQueue.filter(
        (item) => item.word.id !== active.word.id,
      );
    }
    next.active = null;
    next.phase = "waiting";
    chooseNextCard(next);
    // Pending review delivery is deliberately left in the outbox. A request may
    // already have reached the server, so only UUID-idempotent sync can settle it.
    await writeState(next);
    state = next;
    broadcastState();
    await configureAudioCache(false);
    void startPreload();
    notice.textContent = `${active.word.term} was suspended.`;
    if (wordManagement.open) await loadExclusions();
  } catch (_) {
    notice.textContent = "The word could not be suspended. Please try again.";
    void startPreload();
  } finally {
    busy = false;
    await refreshSyncStatus();
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
    restore.disabled = readOnly;
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
  audioCacheProblem = null;
  syncProblem = null;
  clearLearningTimer();
  broadcastState();
  render();
  await refreshSyncStatus();
}

async function logout() {
  if (busy || readOnly) return;
  if (!navigator.onLine) {
    notice.textContent = "You are offline. Sign-out was not sent and local study data was kept.";
    return;
  }
  const { pending, failed } = await refreshSyncStatus();
  if (pending.length + failed.length > 0
      && !window.confirm("Some reviews are not synced. Sign out and remove them from this device?")) {
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

async function acquireStudyLock() {
  if (!navigator.locks) {
    readOnly = false;
    return;
  }
  readOnly = !await new Promise((resolve) => {
    navigator.locks.request(STUDY_LOCK, { ifAvailable: true }, (lock) => {
      if (!lock) {
        resolve(false);
        return undefined;
      }
      resolve(true);
      return new Promise((release) => { releaseStudyLock = release; });
    }).catch(() => resolve(false));
  });
  if (readOnly) waitForStudyLock();
}

function waitForStudyLock() {
  if (!navigator.locks || queuedStudyLock) return;
  queuedStudyLock = navigator.locks.request(STUDY_LOCK, async () => {
    readOnly = false;
    releaseStudyLock = null;
    const hold = new Promise((release) => { releaseStudyLock = release; });
    if (booted) await becomeActiveTab();
    await hold;
  }).finally(() => { queuedStudyLock = null; });
}

async function becomeActiveTab() {
  const saved = await getState();
  if (validState(saved)) {
    state = saved;
    if (!state.active && !state.finished) {
      const next = clone(state);
      chooseNextCard(next);
      if (next.active) await commitState(next);
    }
  }
  audioPresentation = null;
  await refreshSyncStatus();
  render();
  try {
    await configureAudioCache(false);
    void startPreload();
  } catch (_) {
    audioCacheProblem = "Audio cache is unavailable; listening still works online.";
  }
  void sync();
}

async function reloadReadOnlyState() {
  if (!readOnly) return;
  const saved = await getState();
  if (validState(saved)) state = saved;
  await refreshSyncStatus();
  render();
}

async function boot() {
  await acquireStudyLock();
  let saved = null;
  try {
    saved = await getState();
  } catch (_) {
    notice.textContent = "Local study storage is unavailable.";
  }
  if (validState(saved)) {
    state = saved;
    if (!readOnly && !state.active && !state.finished) {
      const next = clone(state);
      chooseNextCard(next);
      if (next.active) await commitState(next);
    }
    try {
      await configureAudioCache(false);
      if (!readOnly) void startPreload();
    } catch (_) {
      audioCacheProblem = "Audio cache is unavailable; listening still works online.";
    }
    await refreshSyncStatus();
    render();
  } else if (!readOnly && navigator.onLine) {
    await refreshSyncStatus();
    await loadSet();
  } else {
    state = emptyState();
    await refreshSyncStatus();
    render();
    if (!readOnly) notice.textContent = "Reconnect to start a study set.";
  }
  booted = true;
  void sync();
}

stateChannel?.addEventListener("message", (event) => {
  if (["state-changed", "outbox-changed"].includes(event.data?.type)) {
    void reloadReadOnlyState();
  }
});
addEventListener("pagehide", () => releaseStudyLock?.());

window.clearPrivateCaches = clearPrivateCaches;
window.__pwa = {
  getAudioCacheState: () => ({ ...audioCacheState }),
  getState: () => clone(state),
  isReadOnly: () => readOnly,
  render,
  sync,
};

previousButton.addEventListener("click", previous);
replayButton.addEventListener("click", playAudio);
showAnswerButton.addEventListener("click", showAnswer);
knownButton.addEventListener("click", () => answer("known"));
unknownButton.addEventListener("click", () => answer("unknown"));
nextSetButton.addEventListener("click", loadSet);
finishButton.addEventListener("click", finishForNow);
retrySyncButton.addEventListener("click", retryAllFailed);
excludeButton.addEventListener("click", excludeActiveWord);
manageExclusionsButton.addEventListener("click", () => loadExclusions().catch(() => {
  exclusionsEmpty.textContent = "Suspended words could not be loaded.";
}));
wordManagement.addEventListener("toggle", () => {
  if (wordManagement.open) manageExclusionsButton.click();
});
logoutButton.addEventListener("click", logout);
addEventListener("online", () => {
  updateNetworkStatus();
  void sync();
});
addEventListener("offline", () => {
  updateNetworkStatus();
  void refreshSyncStatus().then(render);
});
if ("serviceWorker" in navigator) {
  window.__pwa.serviceWorkerRegistration = navigator.serviceWorker.register("/sw.js")
    .then(() => "ready", (error) => String(error));
}
boot().catch(() => {
  state = emptyState();
  render();
  notice.textContent = "Reconnect to start a study set.";
});
