const DB_NAME = "english-vocab-trainer";
const DB_VERSION = 4;
const STATE_KEY = "active-set";
const STATE_FORMAT = 3;
const AUDIO_CACHE = "pwa-private-audio-v6";
const AUDIO_PRELOAD_CONCURRENCY = 4;
const SYNC_BATCH_SIZE = 100;
const STUDY_LOCK = "english-vocab-trainer-active-study";

const emptyState = () => ({
  format: STATE_FORMAT,
  cards: [],
  initialIndex: 0,
  active: null,
  history: [],
  cursor: 0,
  learningStepSeconds: 600,
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
  ? new BroadcastChannel("english-vocab-trainer-state-v3")
  : null;
const $ = (selector) => document.querySelector(selector);
const card = $("#card");
const term = $("#term");
const tier = $("#tier");
const transcript = $("#transcript");
const audio = $("#audio");
const previousButton = $("#previous");
const nextButton = $("#next");
const knownButton = $("#known");
const unknownButton = $("#unknown");
const nextSetButton = $("#next-set");
const retrySyncButton = $("#retry-sync");
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

function clone(value) {
  return structuredClone(value);
}

function validState(saved) {
  return saved?.format === STATE_FORMAT
    && Array.isArray(saved.cards)
    && Array.isArray(saved.history)
    && Number.isInteger(saved.cursor)
    && saved.cursor >= 0
    && saved.cursor <= saved.history.length;
}

function broadcastState() {
  stateChannel?.postMessage({ type: "state-changed" });
}

async function commitState(next) {
  await writeState(next);
  state = next;
  broadcastState();
}

function nowMs() {
  return window.__pwaTestClock?.now?.() ?? Date.now();
}

function outstandingLearning(target) {
  const latest = new Map();
  for (const entry of target.history) latest.set(entry.word.id, entry);
  return [...latest.values()]
    .filter((entry) => entry.action === "unknown")
    .sort((left, right) => left.due_at - right.due_at);
}

function chooseLiveCard(target) {
  if (target.active) return;
  const learning = outstandingLearning(target).find((entry) => entry.due_at <= nowMs());
  if (learning) {
    target.active = { kind: "learning", word: learning.word };
    return;
  }
  if (target.initialIndex < target.cards.length) {
    target.active = { kind: "initial", word: target.cards[target.initialIndex] };
  }
}

function displayedEntry() {
  return state.cursor < state.history.length ? state.history[state.cursor] : null;
}

function displayedWord() {
  return displayedEntry()?.word || state.active?.word || null;
}

function remainingInitial() {
  return Math.max(0, state.cards.length - state.initialIndex);
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

function clearLearningTimer() {
  if (learningTimer) clearTimeout(learningTimer);
  learningTimer = null;
}

function armLearningTimer() {
  clearLearningTimer();
  const learning = outstandingLearning(state);
  if (readOnly || state.active || state.cursor < state.history.length || !learning.length) return;
  learningTimer = setTimeout(wakeForLearning, Math.max(0, learning[0].due_at - nowMs()));
}

async function wakeForLearning() {
  if (readOnly) return;
  const next = clone(state);
  chooseLiveCard(next);
  if (next.active) await commitState(next);
  render();
}

function setVisible(button, visible, enabled = true) {
  button.hidden = !visible;
  button.disabled = busy || readOnly || !enabled;
}

function currentView() {
  if (readOnly) return "readonly";
  if (state.cursor < state.history.length) return "history";
  if (state.active) return "current";
  if (outstandingLearning(state).length) return "waiting";
  return state.emptyReason || "complete";
}

function focusPrimary(view) {
  const target = {
    current: unknownButton,
    history: nextButton,
    waiting: previousButton,
    complete: nextSetButton,
    next_due: nextSetButton,
    catalog_empty: nextSetButton,
  }[view];
  if (target && !target.hidden && !target.disabled) target.focus();
}

function emptyMessage(syncBlocked) {
  if (syncBlocked) return "Sync is blocked. Review the saved errors or retry before starting the next set.";
  if (state.emptyReason === "next_due") {
    const next = state.nextDue ? new Date(state.nextDue).toLocaleString() : "later";
    return `All caught up. The next review is due ${next}.`;
  }
  if (state.emptyReason === "catalog_empty") {
    return "The catalog is empty. Publish a catalog before studying.";
  }
  return "Set complete.";
}

function presentAudio(word) {
  if (!word) {
    audioPresentation = null;
    audio.removeAttribute("src");
    audio.load();
    audio.hidden = true;
    return;
  }
  audio.hidden = false;
  const identity = `${word.id}\u0000${word.audio_url}`;
  if (audioPresentation === identity) return;
  audioPresentation = identity;
  audio.src = word.audio_url;
}

function render() {
  clearLearningTimer();
  updateNetworkStatus();
  updateCacheStatus();
  const historical = displayedEntry();
  const atBoundary = state.cursor === state.history.length;
  const word = displayedWord();
  const learning = outstandingLearning(state);
  const waiting = atBoundary && !state.active && learning.length > 0;
  const complete = atBoundary && !state.active && !waiting;
  const syncBlocked = complete && syncSummary.pending.length + syncSummary.failed.length > 0;
  const view = currentView();

  readOnlyStatus.hidden = !readOnly;
  logoutButton.disabled = busy || readOnly;
  progress.textContent = historical
    ? `History: ${state.cursor + 1}/${state.history.length} · Initial: ${remainingInitial()} remaining · Relearning: ${learning.length}`
    : `Initial: ${remainingInitial()} remaining · Relearning: ${learning.length}`;

  setVisible(previousButton, Boolean(state.history.length), state.cursor > 0);
  setVisible(nextButton, Boolean(historical));
  setVisible(knownButton, Boolean(word));
  setVisible(unknownButton, Boolean(word));
  setVisible(nextSetButton, complete, navigator.onLine && !syncBlocked);

  term.hidden = !word;
  tier.hidden = !word;
  transcript.hidden = !word;
  if (word) {
    term.textContent = word.term;
    tier.textContent = formatTier(word);
    transcript.textContent = word.transcript || "Transcript is unavailable.";
    card.textContent = historical
      ? `Earlier answer: ${historical.action === "known" ? "Known" : "Unknown"}. Navigate or correct it.`
      : "Choose Known or Unknown.";
  } else if (state.cards.length === 0 && state.emptyReason === null) {
    card.textContent = readOnly
      ? "Waiting for the active study tab to load a set."
      : "Reconnect to start a study set.";
    progress.textContent = "No set loaded";
    nextSetButton.hidden = true;
  } else if (waiting) {
    const seconds = Math.max(0, Math.ceil((learning[0].due_at - nowMs()) / 1000));
    card.textContent = `Waiting for relearning: ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}.`;
    armLearningTimer();
  } else {
    card.textContent = emptyMessage(syncBlocked);
  }
  presentAudio(word);

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
    ...target.cards.map((word) => word.audio_url),
    ...target.history.map((entry) => entry.word.audio_url),
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
    if (result.due_at) {
      const states = tx.objectStore("state");
      const record = await requestValue(states.get(STATE_KEY));
      if (validState(record?.value)) {
        const next = clone(record.value);
        const entry = next.history.find((candidate) => candidate.eventId === snapshot.id);
        const dueAt = Date.parse(result.due_at);
        if (entry && Number.isFinite(dueAt)) {
          entry.due_at = dueAt;
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
        syncProblem = `server error (HTTP ${voided.status}); correction kept for retry.`;
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
      const voids = events.filter((event) => event.intent === "void");
      const work = voids.length ? voids : events;
      let completed = true;
      for (let offset = 0; offset < work.length; offset += SYNC_BATCH_SIZE) {
        if (!await syncChunk(work.slice(offset, offset + SYNC_BATCH_SIZE))) {
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

function localDue(reviewedAt, action) {
  return action === "unknown"
    ? Date.parse(reviewedAt) + state.learningStepSeconds * 1000
    : null;
}

async function rateCurrent(action) {
  const active = clone(state.active);
  const reviewedAt = new Date(nowMs()).toISOString();
  const event = {
    id: crypto.randomUUID(),
    word_id: active.word.id,
    action,
    reviewed_at: reviewedAt,
    intent: "review",
  };
  const next = clone(state);
  next.history.push({
    eventId: event.id,
    word: active.word,
    action,
    reviewed_at: reviewedAt,
    due_at: localDue(reviewedAt, action),
    origin: active.kind,
  });
  if (active.kind === "initial") next.initialIndex += 1;
  next.active = null;
  next.cursor = next.history.length;
  chooseLiveCard(next);
  await transaction(["events", "state"], "readwrite", (tx) => {
    tx.objectStore("events").put(event);
    tx.objectStore("state").put({ key: STATE_KEY, value: next });
  });
  state = next;
  broadcastState();
  syncRequested = true;
}

async function rateHistorical(action) {
  const oldEntry = clone(state.history[state.cursor]);
  if (oldEntry.action === action) {
    const next = clone(state);
    next.cursor += 1;
    if (next.cursor === next.history.length) chooseLiveCard(next);
    await commitState(next);
    return;
  }
  const replacement = {
    id: crypto.randomUUID(),
    word_id: oldEntry.word.id,
    action,
    reviewed_at: oldEntry.reviewed_at,
    intent: "review",
  };
  const voided = {
    id: oldEntry.eventId,
    word_id: oldEntry.word.id,
    action: oldEntry.action,
    reviewed_at: oldEntry.reviewed_at,
    intent: "void",
  };
  const next = clone(state);
  next.history[next.cursor] = {
    ...oldEntry,
    eventId: replacement.id,
    action,
    due_at: localDue(oldEntry.reviewed_at, action),
  };
  next.cursor += 1;
  if (next.cursor === next.history.length) chooseLiveCard(next);
  await transaction(["events", "failedEvents", "state"], "readwrite", (tx) => {
    tx.objectStore("events").put(voided);
    tx.objectStore("events").put(replacement);
    tx.objectStore("failedEvents").delete(oldEntry.eventId);
    tx.objectStore("state").put({ key: STATE_KEY, value: next });
  });
  state = next;
  broadcastState();
  syncRequested = true;
}

async function rate(action) {
  const historical = state.cursor < state.history.length;
  if (busy || readOnly || (!historical && !state.active)) return;
  busy = true;
  render();
  try {
    if (historical) await rateHistorical(action);
    else await rateCurrent(action);
    notice.textContent = "";
  } catch (_) {
    notice.textContent = "The answer could not be saved locally. Please try again.";
  } finally {
    busy = false;
    render();
    await refreshSyncStatus();
    void sync();
  }
}

async function navigateHistory(direction) {
  if (busy || readOnly || !state.history.length) return;
  const next = clone(state);
  const destination = next.cursor + direction;
  if (destination < 0 || destination > next.history.length) return;
  next.cursor = destination;
  await commitState(next);
  notice.textContent = "";
  render();
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
  notice.textContent = "";
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
  await transaction("failedEvents", "readwrite", (tx) => {
    tx.objectStore("failedEvents").delete(id);
  });
  notice.textContent = "";
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
      learningStepSeconds: payload.learning_step_seconds,
      emptyReason: payload.items.length ? null : payload.status,
      nextDue: payload.next_due,
    };
    chooseLiveCard(staged);
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
    if (state.cursor === state.history.length && !state.active) {
      const next = clone(state);
      chooseLiveCard(next);
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
    if (!readOnly && state.cursor === state.history.length && !state.active) {
      const next = clone(state);
      chooseLiveCard(next);
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

previousButton.addEventListener("click", () => navigateHistory(-1));
nextButton.addEventListener("click", () => navigateHistory(1));
knownButton.addEventListener("click", () => rate("known"));
unknownButton.addEventListener("click", () => rate("unknown"));
nextSetButton.addEventListener("click", loadSet);
retrySyncButton.addEventListener("click", retryAllFailed);
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
