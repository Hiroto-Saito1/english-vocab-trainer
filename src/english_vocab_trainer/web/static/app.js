const DB_NAME = "english-vocab-trainer";
const DB_VERSION = 5;
const STATE_KEY = "active-set";
const STATE_FORMAT = 4;
const SYNC_BATCH_SIZE = 100;

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
let syncFlight = null;
let syncRequested = false;
let syncProblem = null;
let userNotice = null;
let syncSummary = { pending: [], failed: [] };
let audioPresentation = null;
let lastView = null;

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

function formatTier(word) {
  const name = word.tier === "upper" ? "Upper" : "Ultra";
  return `${name} · ${word.level == null ? "level unknown" : `SVL ${word.level}`}`;
}

function renderSyncIssues(failed) {
  syncIssuesList.replaceChildren();
  syncIssues.hidden = failed.length === 0;
  for (const event of failed) {
    const item = document.createElement("li");
    const detail = document.createElement("p");
    detail.textContent = `${event.word_id}: ${event.action} · ${event.terminalStatus} · ${event.id}`;
    const retry = document.createElement("button");
    retry.textContent = "Retry this review";
    retry.addEventListener("click", () => retryFailedEvent(event.id));
    const dismiss = document.createElement("button");
    dismiss.textContent = "Dismiss this review";
    dismiss.addEventListener("click", () => dismissFailedEvent(event.id));
    item.append(detail, retry, dismiss);
    syncIssuesList.append(item);
  }
}

function renderStatus() {
  let message = userNotice;
  if (!message && syncSummary.failed.length) {
    const count = syncSummary.failed.length;
    message = `${count} review${count === 1 ? "" : "s"} need attention.`;
  } else if (!message && !navigator.onLine && syncSummary.pending.length) {
    message = `Offline · ${syncSummary.pending.length} review${syncSummary.pending.length === 1 ? "" : "s"} waiting.`;
  } else if (!message && !navigator.onLine) {
    message = "Offline. Audio is unavailable until you reconnect.";
  } else if (!message && syncProblem) {
    message = syncProblem;
  } else if (!message && syncSummary.pending.length) {
    message = `${syncSummary.pending.length} review${syncSummary.pending.length === 1 ? "" : "s"} waiting to sync.`;
  }
  status.textContent = message || "";
  status.hidden = !message;
}

async function refreshSyncStatus() {
  const [pending, failed] = await Promise.all([allEvents(), allFailedEvents()]);
  syncSummary = { pending, failed };
  retrySyncButton.hidden = pending.length + failed.length === 0;
  renderSyncIssues(failed);
  renderStatus();
  return syncSummary;
}

function setVisible(button, visible, enabled = true) {
  button.hidden = !visible;
  button.disabled = busy || !enabled;
}

function isComplete() {
  return state.cards.length > 0
    && state.current === state.cards.length
    && state.cursor === state.history.length;
}

function currentView() {
  if (state.cursor < state.history.length) return "history";
  if (liveWord()) return "current";
  if (isComplete()) return "complete";
  return state.emptyReason || "unavailable";
}

function focusPrimary(view) {
  const target = {
    current: unknownButton,
    history: nextButton,
    complete: nextSetButton,
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
  if (isComplete()) return "Set complete.";
  return "Reconnect to start a study set.";
}

function presentAudio(word) {
  if (!word) {
    audioPresentation = null;
    audio.pause();
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
  const historical = displayedEntry();
  const word = displayedWord();
  const complete = isComplete();
  const syncBlocked = complete && syncSummary.pending.length + syncSummary.failed.length > 0;
  const view = currentView();

  logoutButton.disabled = busy;
  progress.textContent = historical
    ? `History: ${state.cursor + 1}/${state.history.length} · ${state.cards.length - state.current} cards remaining`
    : `${Math.min(state.current + 1, state.cards.length)}/${state.cards.length} cards`;

  setVisible(previousButton, Boolean(state.history.length), state.cursor > 0);
  setVisible(nextButton, Boolean(historical));
  setVisible(knownButton, Boolean(word), historical?.action !== "known");
  setVisible(unknownButton, Boolean(word), historical?.action !== "unknown");
  knownButton.setAttribute("aria-pressed", String(historical?.action === "known"));
  unknownButton.setAttribute("aria-pressed", String(historical?.action === "unknown"));
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
  } else {
    card.textContent = emptyMessage(syncBlocked);
    progress.textContent = state.cards.length ? `${state.cards.length}/${state.cards.length} cards` : "No set loaded";
  }
  presentAudio(word);
  renderStatus();

  const focused = document.activeElement;
  if (focused?.hidden || focused?.disabled || focused?.closest("[hidden]")) {
    focused.blur();
    requestAnimationFrame(() => focusPrimary(view));
  }
  if (view !== lastView) {
    lastView = view;
    requestAnimationFrame(() => focusPrimary(view));
  }
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
    syncProblem = `Server error (HTTP ${response.status}); reviews kept for retry.`;
    return false;
  }
  const payload = await response.json();
  const resultById = new Map(payload.results.map((result) => [result.id, result]));
  for (const snapshot of chunk) {
    const result = resultById.get(snapshot.id);
    if (!result) {
      syncProblem = "Server response omitted a review; it was kept for retry.";
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
        syncProblem = `Server error (HTTP ${voided.status}); correction kept for retry.`;
        return false;
      }
    } else {
      await removeSyncedEvent(snapshot);
    }
  }
  return true;
}

async function sync() {
  syncRequested = true;
  if (syncFlight) return syncFlight;
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
    if (!navigator.onLine && (await allEvents()).length) syncProblem = null;
    else if (!(await allEvents()).length) syncProblem = null;
    await refreshSyncStatus();
    render();
  })().catch(async () => {
    syncProblem = navigator.onLine ? "Network error; reviews kept for retry." : null;
    await refreshSyncStatus();
    render();
  });
  try {
    await syncFlight;
  } finally {
    syncFlight = null;
    if (syncRequested && navigator.onLine) void sync();
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
  syncRequested = true;
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
  syncRequested = true;
}

async function rate(action) {
  const historical = state.cursor < state.history.length;
  if (busy || (!historical && !liveWord())) return;
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
    await refreshSyncStatus();
    void sync();
  }
}

async function navigateHistory(direction) {
  if (busy || !state.history.length) return;
  const next = clone(state);
  const destination = next.cursor + direction;
  if (destination < 0 || destination > next.history.length) return;
  next.cursor = destination;
  await commitState(next);
  userNotice = null;
  render();
}

async function retryFailedEvent(id) {
  if (busy) return;
  await transaction(["events", "failedEvents"], "readwrite", async (tx) => {
    const failed = tx.objectStore("failedEvents");
    const event = await requestValue(failed.get(id));
    if (!event) return;
    const { terminalStatus: _, ...retry } = event;
    tx.objectStore("events").put(retry);
    failed.delete(id);
  });
  syncProblem = null;
  userNotice = null;
  await refreshSyncStatus();
  render();
  void sync();
}

async function retryAllFailed() {
  if (busy) return;
  const failed = await allFailedEvents();
  await Promise.all(failed.map((event) => retryFailedEvent(event.id)));
  void sync();
}

async function dismissFailedEvent(id) {
  if (busy) return;
  if (!window.confirm("Dismiss this review from this device? The server history will not be changed.")) return;
  await transaction("failedEvents", "readwrite", (tx) => {
    tx.objectStore("failedEvents").delete(id);
  });
  userNotice = null;
  await refreshSyncStatus();
  render();
}

async function loadSet() {
  if (busy || !navigator.onLine) {
    userNotice = "Reconnect to load a new set.";
    renderStatus();
    return;
  }
  busy = true;
  render();
  try {
    await sync();
    const outstanding = await refreshSyncStatus();
    if (outstanding.pending.length || outstanding.failed.length) {
      userNotice = "Resolve or retry saved reviews before starting the next set.";
      return;
    }
    const response = await apiFetch("/api/v1/sets");
    if (!response.ok) throw new Error("set unavailable");
    const payload = await response.json();
    const staged = {
      ...emptyState(),
      cards: payload.items,
      emptyReason: payload.items.length ? null : payload.status,
      nextDue: payload.next_due,
    };
    await writeState(staged);
    state = staged;
    userNotice = null;
  } catch (_) {
    userNotice = "A new set could not be loaded. Your current set was kept.";
  } finally {
    busy = false;
    render();
  }
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
  if (busy) return;
  if (!navigator.onLine) {
    userNotice = "You are offline. Sign-out was not sent and local study data was kept.";
    renderStatus();
    return;
  }
  const { pending, failed } = await refreshSyncStatus();
  if (pending.length + failed.length > 0
      && !window.confirm("Some reviews are not synced. Sign out and remove them from this device?")) {
    userNotice = "Sign-out was cancelled; local study data was kept.";
    renderStatus();
    return;
  }
  busy = true;
  logoutButton.disabled = true;
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

async function boot() {
  let saved = null;
  try {
    saved = await getState();
  } catch (_) {
    userNotice = "Local study storage is unavailable.";
  }
  if (validState(saved)) {
    state = saved;
    await refreshSyncStatus();
    render();
  } else if (navigator.onLine) {
    await refreshSyncStatus();
    await loadSet();
  } else {
    state = emptyState();
    await refreshSyncStatus();
    userNotice = "Reconnect to start a study set.";
    render();
  }
  void sync();
}

previousButton.addEventListener("click", () => navigateHistory(-1));
nextButton.addEventListener("click", () => navigateHistory(1));
knownButton.addEventListener("click", () => rate("known"));
unknownButton.addEventListener("click", () => rate("unknown"));
nextSetButton.addEventListener("click", loadSet);
retrySyncButton.addEventListener("click", retryAllFailed);
logoutButton.addEventListener("click", logout);
addEventListener("online", () => {
  userNotice = null;
  void sync();
});
addEventListener("offline", () => {
  void refreshSyncStatus().then(render);
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
boot().catch(() => {
  state = emptyState();
  userNotice = "Reconnect to start a study set.";
  render();
});
