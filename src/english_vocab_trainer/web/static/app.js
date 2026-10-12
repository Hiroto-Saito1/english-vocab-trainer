const DB_NAME = "english-vocab-trainer";
const DB_VERSION = 5;
const STATE_KEY = "active-set";
const STATE_FORMAT = 4;
const SYNC_BATCH_SIZE = 100;
const WRITER_LOCK = "english-vocab-trainer-writer";

const emptyState = () => ({
  format: STATE_FORMAT,
  cards: [],
  current: 0,
  history: [],
  cursor: 0,
  emptyReason: null,
  nextDue: null,
});

let state = emptyState();
let busy = false;
let writer = false;
let releaseWriterLock = null;
let syncFlight = null;
let syncRequested = false;
let loadFlight = null;
let nextDueTimer = null;
let syncProblem = null;
let userNotice = null;
let syncSummary = { pending: [], failed: [] };
let audioPresentation = null;

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
const retrySyncButton = $("#retry-sync");
const syncIssues = $("#sync-issues");
const syncIssuesList = $("#sync-issues-list");
const progress = $("#progress");
const status = $("#status");
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
    && Number.isInteger(saved.current)
    && saved.current >= 0
    && saved.current <= saved.cards.length
    && Array.isArray(saved.history)
    && Number.isInteger(saved.cursor)
    && saved.cursor >= 0
    && saved.cursor <= saved.history.length;
}

async function commitState(next) {
  await writeState(next);
  state = next;
}

function displayedEntry() {
  return state.cursor < state.history.length ? state.history[state.cursor] : null;
}

function liveWord() {
  return state.current < state.cards.length ? state.cards[state.current] : null;
}

function displayedWord() {
  return displayedEntry()?.word || liveWord();
}

function batchConsumed() {
  return state.cards.length > 0 && state.current === state.cards.length;
}

function nextDueReached() {
  return state.emptyReason === "next_due"
    && state.nextDue !== null
    && Date.parse(state.nextDue) <= Date.now();
}

function shouldLoadBatch() {
  return writer
    && navigator.onLine
    && !syncSummary.pending.length
    && !syncSummary.failed.length
    && (batchConsumed()
      || (!state.cards.length && state.emptyReason === null)
      || nextDueReached());
}

function formatTier(word) {
  const name = word.tier === "upper" ? "Upper" : "Ultra";
  return `${name} · ${word.level == null ? "level unknown" : `SVL ${word.level}`}`;
}

function wordForEvent(event) {
  return state.history.find((entry) => entry.word.id === event.word_id)?.word
    || state.cards.find((word) => word.id === event.word_id);
}

function renderSyncIssues(failed) {
  syncIssuesList.replaceChildren();
  syncIssues.hidden = failed.length === 0;
  for (const event of failed) {
    const item = document.createElement("li");
    const detail = document.createElement("p");
    const word = wordForEvent(event);
    const answer = event.action === "known" ? "Known" : "Unknown";
    detail.textContent = `${word?.term || "This word"}: the ${answer} answer was not saved on the server.`;
    const retry = document.createElement("button");
    retry.textContent = "Retry answer";
    retry.disabled = busy || !writer || !navigator.onLine;
    retry.addEventListener("click", () => retryFailedEvent(event.id));
    const discard = document.createElement("button");
    discard.textContent = "Discard local change";
    discard.disabled = busy || !writer;
    discard.addEventListener("click", () => discardFailedEvent(event.id));
    item.append(detail, retry, discard);
    syncIssuesList.append(item);
  }
}

function renderStatus() {
  retrySyncButton.hidden = !(writer && navigator.onLine && syncProblem);
  retrySyncButton.disabled = busy || !writer;
  let message = null;
  if (!writer) {
    message = "locks" in navigator
      ? "This app is already open in another tab. Close it, then reload this page to study here."
      : "This browser cannot safely save study changes because Web Locks are unavailable.";
  } else if (syncSummary.failed.length) {
    const count = syncSummary.failed.length;
    message = `${count} ${count === 1 ? "answer needs" : "answers need"} your attention.`;
  } else if (userNotice) {
    message = userNotice;
  } else if (!navigator.onLine && syncSummary.pending.length) {
    const count = syncSummary.pending.length;
    message = `Offline · ${count} answer${count === 1 ? "" : "s"} waiting to sync.`;
  } else if (!navigator.onLine) {
    message = "Offline. Audio is unavailable until you reconnect.";
  } else if (syncProblem) {
    message = syncProblem;
  }
  status.textContent = message || "";
  status.hidden = !message;
}

async function refreshSyncStatus() {
  const [pending, failed] = await Promise.all([allEvents(), allFailedEvents()]);
  syncSummary = { pending, failed };
  renderSyncIssues(failed);
  renderStatus();
  return syncSummary;
}

function setVisible(button, visible, enabled = true) {
  button.hidden = !visible;
  button.disabled = busy || !writer || !enabled;
}

function emptyMessage() {
  if (syncSummary.failed.length) {
    return "Resolve the answer that needs attention before continuing.";
  }
  if (syncSummary.pending.length) {
    if (!navigator.onLine) return "Reconnect to save your answers and continue.";
    if (syncProblem) return "Your answers could not be saved. Select Try again.";
  }
  if (syncProblem && !syncSummary.pending.length) {
    return navigator.onLine
      ? "More cards could not be loaded. Select Try again."
      : "More cards could not be loaded. Reconnect to try again.";
  }
  if (state.emptyReason === "next_due") {
    const next = state.nextDue ? new Date(state.nextDue).toLocaleString() : "later";
    return `All caught up. The next review is due ${next}.`;
  }
  if (state.emptyReason === "catalog_empty") {
    return "The catalog is empty. Publish a catalog before studying.";
  }
  if (batchConsumed()) return "Loading more cards…";
  if (!writer) return "Another tab is currently handling study changes.";
  return navigator.onLine ? "Loading cards…" : "Reconnect to continue studying.";
}

function releaseAudio() {
  audioPresentation = null;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  audio.hidden = true;
}

function presentAudio(word) {
  if (!word || !navigator.onLine) {
    releaseAudio();
    return;
  }
  audio.hidden = false;
  const identity = `${word.id}\u0000${word.audio_url}`;
  if (audioPresentation === identity) return;
  audioPresentation = identity;
  audio.src = word.audio_url;
  audio.load();
}

function scheduleNextDue() {
  if (nextDueTimer !== null) clearTimeout(nextDueTimer);
  nextDueTimer = null;
  if (state.emptyReason !== "next_due" || state.nextDue === null) return;
  const delay = Date.parse(state.nextDue) - Date.now();
  if (delay <= 0) {
    if (shouldLoadBatch()) queueMicrotask(() => void loadBatch());
    return;
  }
  nextDueTimer = setTimeout(() => {
    nextDueTimer = null;
    if (nextDueReached()) {
      if (shouldLoadBatch()) void loadBatch();
    } else {
      scheduleNextDue();
    }
  }, Math.min(delay, 2_147_483_647));
}

function render() {
  const historical = displayedEntry();
  const word = displayedWord();
  const loadBlocked = syncProblem && !syncSummary.pending.length;

  logoutButton.disabled = busy || !writer;
  if (historical) {
    progress.textContent = `Reviewing answer ${state.cursor + 1} of ${state.history.length}`;
  } else if (word) {
    progress.textContent = "Ready to answer";
  } else if (syncSummary.failed.length) {
    progress.textContent = "Answer needs attention";
  } else if (!navigator.onLine && syncSummary.pending.length) {
    progress.textContent = "Waiting to reconnect";
  } else if (syncProblem && syncSummary.pending.length) {
    progress.textContent = "Unable to save answers";
  } else if (loadBlocked) {
    progress.textContent = "Unable to load cards";
  } else if (batchConsumed()) {
    progress.textContent = "Loading more cards";
  } else if (state.emptyReason === "next_due") {
    progress.textContent = "No reviews due";
  } else {
    progress.textContent = "No cards available";
  }

  setVisible(previousButton, Boolean(state.history.length), state.cursor > 0);
  setVisible(nextButton, Boolean(historical));
  setVisible(knownButton, Boolean(word), historical?.action !== "known");
  setVisible(unknownButton, Boolean(word), historical?.action !== "unknown");
  knownButton.textContent = historical
    ? (historical.action === "known" ? "Known — saved" : "Change to Known")
    : "Known";
  unknownButton.textContent = historical
    ? (historical.action === "unknown" ? "Unknown — saved" : "Change to Unknown")
    : "Unknown";

  term.hidden = !word;
  tier.hidden = !word;
  transcript.hidden = !word;
  if (word) {
    term.textContent = word.term;
    tier.textContent = formatTier(word);
    transcript.textContent = word.transcript || "Transcript is unavailable.";
    card.textContent = historical
      ? "Review this saved answer, move through history, or choose the other answer to correct it."
      : "Choose Known or Unknown.";
  } else {
    card.textContent = emptyMessage();
  }
  presentAudio(word);
  renderSyncIssues(syncSummary.failed);
  renderStatus();
  scheduleNextDue();

  const focused = document.activeElement;
  if (focused?.hidden || focused?.disabled || focused?.closest("[hidden]")) focused.blur();
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
    if (current?.intent === snapshot.intent) store.delete(snapshot.id);
  });
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
    syncProblem = `The server could not save your answers (HTTP ${response.status}).`;
    return false;
  }
  const payload = await response.json();
  const resultById = new Map(payload.results.map((result) => [result.id, result]));
  for (const snapshot of chunk) {
    const result = resultById.get(snapshot.id);
    if (!result) {
      syncProblem = "The server did not confirm every answer. Your changes were kept for retry.";
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
        syncProblem = `The server could not save your correction (HTTP ${voided.status}).`;
        return false;
      }
    } else {
      await removeSyncedEvent(snapshot);
    }
  }
  return true;
}

async function sync() {
  if (!writer) return;
  syncRequested = true;
  if (syncFlight) return syncFlight;
  let loadAfterSync = false;
  syncFlight = (async () => {
    while (navigator.onLine) {
      syncRequested = false;
      const events = await allEvents();
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
    const pending = await allEvents();
    if (!navigator.onLine && pending.length) syncProblem = null;
    else if (!pending.length) syncProblem = null;
    await refreshSyncStatus();
    loadAfterSync = shouldLoadBatch();
    render();
  })().catch(async () => {
    syncProblem = navigator.onLine ? "A network error stopped answer sync. Your changes were kept." : null;
    await refreshSyncStatus();
    render();
  });
  try {
    await syncFlight;
  } finally {
    syncFlight = null;
    if (syncRequested && navigator.onLine) void sync();
    else if (loadAfterSync) void loadBatch();
  }
}

async function rateCurrent(action) {
  const word = clone(liveWord());
  const reviewedAt = new Date().toISOString();
  const event = {
    id: crypto.randomUUID(),
    word_id: word.id,
    action,
    reviewed_at: reviewedAt,
    intent: "review",
  };
  const next = clone(state);
  next.history.push({ eventId: event.id, word, action, reviewed_at: reviewedAt });
  next.current += 1;
  next.cursor = next.history.length;
  await transaction(["events", "state"], "readwrite", (tx) => {
    tx.objectStore("events").put(event);
    tx.objectStore("state").put({ key: STATE_KEY, value: next });
  });
  state = next;
}

async function rateHistorical(action) {
  const oldEntry = clone(state.history[state.cursor]);
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
  next.history[next.cursor] = { ...oldEntry, eventId: replacement.id, action };
  next.cursor += 1;
  await transaction(["events", "failedEvents", "state"], "readwrite", (tx) => {
    tx.objectStore("events").put(voided);
    tx.objectStore("events").put(replacement);
    tx.objectStore("failedEvents").delete(oldEntry.eventId);
    tx.objectStore("state").put({ key: STATE_KEY, value: next });
  });
  state = next;
}

async function rate(action) {
  const historical = displayedEntry();
  if (busy || !writer || (!historical && !liveWord()) || historical?.action === action) return;
  busy = true;
  render();
  try {
    if (historical) await rateHistorical(action);
    else await rateCurrent(action);
    userNotice = null;
  } catch (_) {
    userNotice = "The answer could not be saved locally. Please try again.";
  } finally {
    busy = false;
    render();
    if (navigator.onLine) void sync();
    else void refreshSyncStatus().then(render);
  }
}

async function navigateHistory(direction) {
  if (busy || !writer || !state.history.length) return;
  const next = clone(state);
  const destination = next.cursor + direction;
  if (destination < 0 || destination > next.history.length) return;
  next.cursor = destination;
  try {
    await commitState(next);
    userNotice = null;
  } catch (_) {
    userNotice = "Your place could not be saved locally.";
  }
  render();
}

async function retryFailedEvent(id) {
  if (busy || !writer || !navigator.onLine) return;
  busy = true;
  render();
  let queued = false;
  try {
    queued = await transaction(["events", "failedEvents"], "readwrite", async (tx) => {
      const failed = tx.objectStore("failedEvents");
      const event = await requestValue(failed.get(id));
      if (!event) return false;
      const { terminalStatus: _, ...retry } = event;
      tx.objectStore("events").put(retry);
      failed.delete(id);
      return true;
    });
    syncProblem = null;
    userNotice = null;
    await refreshSyncStatus();
  } catch (_) {
    userNotice = "The answer could not be prepared for retry.";
  } finally {
    busy = false;
    render();
  }
  if (queued) void sync();
}

async function discardFailedEvent(id) {
  if (busy || !writer) return;
  if (!window.confirm("Discard this local answer change? The server history will not be changed.")) return;
  const event = syncSummary.failed.find((failed) => failed.id === id);
  if (!event) return;
  busy = true;
  render();
  const next = clone(state);
  const historyIndex = next.history.findIndex((entry) => entry.eventId === event.id
    && entry.action === event.action
    && entry.reviewed_at === event.reviewed_at);
  if (historyIndex >= 0) {
    next.history.splice(historyIndex, 1);
    if (next.cursor > historyIndex) next.cursor -= 1;
  }
  let discarded = false;
  try {
    await transaction(["failedEvents", "state"], "readwrite", (tx) => {
      tx.objectStore("failedEvents").delete(id);
      tx.objectStore("state").put({ key: STATE_KEY, value: next });
    });
    state = next;
    userNotice = null;
    discarded = true;
    await refreshSyncStatus();
  } catch (_) {
    userNotice = "The local answer change could not be discarded.";
  } finally {
    busy = false;
    render();
  }
  if (discarded && shouldLoadBatch()) void loadBatch();
}

async function loadBatch() {
  if (!writer || !navigator.onLine || loadFlight) return loadFlight;
  loadFlight = (async () => {
    busy = true;
    render();
    try {
      const { pending, failed } = await refreshSyncStatus();
      if (pending.length || failed.length) return;
      const response = await apiFetch("/api/v1/sets");
      if (!response.ok) throw new Error("cards unavailable");
      const payload = await response.json();
      const staged = clone(state);
      staged.cards = payload.items;
      staged.current = 0;
      staged.emptyReason = payload.items.length ? null : payload.status;
      staged.nextDue = payload.next_due;
      await writeState(staged);
      state = staged;
      syncProblem = null;
      userNotice = null;
    } catch (_) {
      syncProblem = "More cards could not be loaded. Your study history was kept.";
      userNotice = null;
    } finally {
      busy = false;
      render();
    }
  })();
  try {
    await loadFlight;
  } finally {
    loadFlight = null;
  }
  return null;
}

async function retryNetworkSync() {
  if (busy || !writer || !navigator.onLine) return;
  syncProblem = null;
  userNotice = null;
  render();
  void sync();
}

async function clearPrivateData() {
  await transaction(["events", "failedEvents", "state"], "readwrite", (tx) => {
    tx.objectStore("events").clear();
    tx.objectStore("failedEvents").clear();
    tx.objectStore("state").clear();
  });
  state = emptyState();
  syncProblem = null;
  userNotice = null;
  syncSummary = { pending: [], failed: [] };
}

async function logout() {
  if (busy || !writer) return;
  if (!navigator.onLine) {
    userNotice = "You are offline. Sign-out was not sent and local study data was kept.";
    renderStatus();
    return;
  }
  const { pending, failed } = await refreshSyncStatus();
  if (pending.length + failed.length > 0
      && !window.confirm("Some answers are not synced. Sign out and remove them from this device?")) {
    userNotice = "Sign-out was cancelled; local study data was kept.";
    renderStatus();
    return;
  }
  busy = true;
  render();
  try {
    const response = await apiFetch("/auth/logout", { method: "POST", redirectOn401: false });
    if (![204, 401].includes(response.status)) throw new Error("logout failed");
    try {
      await clearPrivateData();
    } catch (_) {
      userNotice = "Server sign-out succeeded, but local study data could not be cleared.";
      busy = false;
      render();
      return;
    }
    location.assign("/login");
  } catch (_) {
    userNotice = "Sign-out did not reach the server. Local study data was kept.";
    busy = false;
    render();
  }
}

async function acquireWriterLock() {
  if (!("locks" in navigator)) {
    writer = false;
    return;
  }
  writer = await new Promise((resolve) => {
    navigator.locks.request(WRITER_LOCK, { ifAvailable: true }, (lock) => {
      if (!lock) {
        resolve(false);
        return undefined;
      }
      resolve(true);
      return new Promise((release) => { releaseWriterLock = release; });
    }).catch(() => resolve(false));
  });
}

async function boot() {
  await acquireWriterLock();
  let saved = null;
  try {
    saved = await getState();
  } catch (_) {
    userNotice = "Local study storage is unavailable.";
  }
  if (validState(saved)) state = saved;
  await refreshSyncStatus();
  render();
  if (!writer) return;
  if (validState(saved)) {
    void sync();
  } else if (navigator.onLine) {
    void loadBatch();
  } else {
    userNotice = "Reconnect to start studying.";
    render();
  }
}

function showStartupFailure() {
  state = emptyState();
  userNotice = "Reconnect to start studying.";
  render();
}

previousButton.addEventListener("click", () => navigateHistory(-1));
nextButton.addEventListener("click", () => navigateHistory(1));
knownButton.addEventListener("click", () => rate("known"));
unknownButton.addEventListener("click", () => rate("unknown"));
retrySyncButton.addEventListener("click", retryNetworkSync);
logoutButton.addEventListener("click", logout);
addEventListener("online", () => {
  userNotice = null;
  render();
  if (writer) void sync();
});
addEventListener("offline", () => {
  render();
  void refreshSyncStatus().then(render);
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && shouldLoadBatch()) void loadBatch();
});
addEventListener("pagehide", () => {
  if (releaseWriterLock) releaseWriterLock();
  releaseWriterLock = null;
  writer = false;
});
addEventListener("pageshow", (event) => {
  if (event.persisted && !writer) {
    void boot().catch(showStartupFailure);
  }
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
boot().catch(showStartupFailure);
