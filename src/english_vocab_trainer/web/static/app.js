const WRITER_LOCK = "english-vocab-trainer-writer";

let currentCard = null;
let nextDue;
let history = [];
let historyIndex = -1;
let olderCursor = null;
let busy = false;
let writer = false;
let releaseWriterLock = null;
let failure = null;
let nextDueTimer = null;
let bootFlight = null;

const $ = (selector) => document.querySelector(selector);
const card = $("#card");
const term = $("#term");
const definition = $("#definition");
const audio = $("#audio");
const previousButton = $("#previous");
const nextButton = $("#next");
const knownButton = $("#known");
const unknownButton = $("#unknown");
const retryButton = $("#retry");
const savedAnswer = $("#saved-answer");
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
  if (redirectOn401 && response.status === 401) location.assign("/login");
  return response;
}

function historicalEntry() {
  return historyIndex >= 0 ? history[historyIndex] : null;
}

function displayedWord() {
  return historicalEntry() || currentCard;
}

function moveNext() {
  if (historyIndex > 0) historyIndex -= 1;
  else if (historyIndex === 0) historyIndex = -1;
}

function scheduleNextDue() {
  if (nextDueTimer !== null) clearTimeout(nextDueTimer);
  nextDueTimer = null;
  if (nextDue == null || !writer || busy || failure !== null) return;
  const delay = Date.parse(nextDue) - Date.now();
  if (delay <= 0) {
    queueMicrotask(() => void loadCard());
    return;
  }
  nextDueTimer = setTimeout(
    () => {
      nextDueTimer = null;
      scheduleNextDue();
    },
    Math.min(delay, 2_147_483_647),
  );
}

function render() {
  const historical = historicalEntry();
  const word = displayedWord();
  const retryLocked = busy || !writer;
  const studyLocked = retryLocked || failure !== null;

  term.hidden = !word;
  definition.hidden = !word;
  audio.hidden = !word;
  if (word) {
    term.textContent = word.term;
    definition.textContent = word.definition;
    if (audio.dataset.wordId !== word.word_id) {
      audio.dataset.wordId = word.word_id;
      audio.src = word.audio_url;
      audio.load();
    }
    card.textContent = "";
  } else {
    audio.pause();
    audio.removeAttribute("src");
    audio.removeAttribute("data-word-id");
    audio.load();
    if (busy) {
      card.textContent = "Loading a card…";
    } else if (nextDue === undefined) {
      card.textContent = "Loading a card…";
    } else if (nextDue !== null) {
      card.textContent = `All caught up. The next review is due ${new Date(nextDue).toLocaleString()}.`;
    } else {
      card.textContent = "The catalog is empty. Publish a catalog before studying.";
    }
  }

  previousButton.disabled = studyLocked || (!history.length && historyIndex < 0)
    || (historyIndex === history.length - 1 && olderCursor === null);
  nextButton.disabled = studyLocked || historyIndex < 0;
  unknownButton.disabled = studyLocked || !word;
  knownButton.disabled = studyLocked || !word;
  savedAnswer.hidden = !historical;
  savedAnswer.textContent = historical
    ? `Saved answer: ${historical.action === "known" ? "Known" : "Unknown"}`
    : "";

  status.textContent = !writer
    ? ("locks" in navigator
      ? "This app is open in another tab. Close it and reload to study here."
      : "This browser cannot safely save answers because Web Locks are unavailable.")
    : failure?.message || "";
  status.hidden = !status.textContent;
  retryButton.hidden = failure === null;
  retryButton.disabled = retryLocked;
  retryButton.textContent = failure?.label || "Try again";
  if (logoutButton) logoutButton.disabled = studyLocked;

  const focused = document.activeElement;
  if (focused?.disabled || focused?.hidden || focused?.closest("[hidden]")) focused.blur();
  scheduleNextDue();
}

async function loadCard() {
  if (busy || !writer) return;
  busy = true;
  failure = null;
  nextDue = undefined;
  render();
  try {
    const response = await apiFetch("/api/v1/card");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    currentCard = payload.item;
    nextDue = payload.next_due;
  } catch (_) {
    failure = {
      message: "The next card could not be loaded.",
      label: "Retry next card",
      run: loadCard,
    };
  } finally {
    busy = false;
    render();
  }
}

async function loadHistory(reset = false) {
  if (busy || !writer) return;
  busy = true;
  failure = null;
  render();
  try {
    const cursor = reset || olderCursor === null
      ? ""
      : `?cursor=${encodeURIComponent(olderCursor)}`;
    const response = await apiFetch(`/api/v1/history${cursor}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (reset) {
      history = payload.items;
      historyIndex = -1;
    } else {
      history.push(...payload.items);
    }
    olderCursor = payload.next_cursor;
  } catch (_) {
    failure = {
      message: "Answer history could not be loaded.",
      label: "Retry history",
      run: () => loadHistory(reset),
    };
  } finally {
    busy = false;
    render();
  }
}

async function previous() {
  if (busy || !writer || failure !== null) return;
  if (historyIndex < 0) {
    if (!history.length) return;
    historyIndex = 0;
    render();
    return;
  }
  if (historyIndex + 1 < history.length) {
    historyIndex += 1;
    render();
    return;
  }
  if (olderCursor === null) return;
  const oldLength = history.length;
  await loadHistory();
  if (history.length > oldLength) historyIndex += 1;
  render();
}

function next() {
  if (busy || !writer || failure !== null || historyIndex < 0) return;
  moveNext();
  render();
}

function reviewPayload(entry, action) {
  if (entry) {
    return {
      id: entry.review_id,
      word_id: entry.word_id,
      action,
    };
  }
  return {
    id: crypto.randomUUID(),
    word_id: currentCard.word_id,
    action,
  };
}

async function saveAnswer(operation) {
  if (busy || !writer) return;
  busy = true;
  failure = null;
  render();
  try {
    const response = await apiFetch("/api/v1/reviews", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(operation.payload),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (operation.kind === "history") {
      history[operation.index] = { ...history[operation.index], action: operation.payload.action };
      historyIndex = operation.index;
      moveNext();
      currentCard = null;
      nextDue = undefined;
    } else {
      const answered = {
        review_id: operation.payload.id,
        word_id: currentCard.word_id,
        action: operation.payload.action,
        term: currentCard.term,
        definition: currentCard.definition,
        audio_url: currentCard.audio_url,
      };
      history.unshift(answered);
      currentCard = null;
      nextDue = undefined;
    }
  } catch (_) {
    failure = {
      message: operation.kind === "history"
        ? "The corrected answer could not be saved."
        : "The answer could not be saved.",
      label: "Retry answer",
      run: () => saveAnswer(operation),
    };
    busy = false;
    render();
    return;
  }
  busy = false;
  render();
  await loadCard();
}

async function rate(action) {
  if (busy || !writer || failure !== null) return;
  const entry = historicalEntry();
  const word = displayedWord();
  if (!word) return;
  if (entry?.action === action) {
    moveNext();
    render();
    return;
  }
  const operation = {
    kind: entry ? "history" : "current",
    index: historyIndex,
    payload: reviewPayload(entry, action),
  };
  await saveAnswer(operation);
}

async function logout() {
  if (busy || !writer || failure !== null) return;
  busy = true;
  failure = null;
  render();
  try {
    const response = await apiFetch("/auth/logout", { method: "POST", redirectOn401: false });
    if (![204, 401].includes(response.status)) throw new Error(`HTTP ${response.status}`);
    location.assign("/login");
  } catch (_) {
    failure = {
      message: "Sign-out failed. Please try again.",
      label: "Retry sign-out",
      run: logout,
    };
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
  if (bootFlight) return bootFlight;
  bootFlight = (async () => {
    await acquireWriterLock();
    render();
    if (!writer) {
      const [cardResponse, historyResponse] = await Promise.all([
        apiFetch("/api/v1/card"),
        apiFetch("/api/v1/history"),
      ]);
      if (cardResponse.ok) {
        const payload = await cardResponse.json();
        currentCard = payload.item;
        nextDue = payload.next_due;
      }
      if (historyResponse.ok) {
        const payload = await historyResponse.json();
        history = payload.items;
        olderCursor = payload.next_cursor;
      }
      render();
      return;
    }
    await loadHistory(true);
    const historyFailure = failure;
    await loadCard();
    if (historyFailure && failure === null) {
      failure = historyFailure;
      render();
    }
  })();
  try {
    await bootFlight;
  } finally {
    bootFlight = null;
  }
}

previousButton.addEventListener("click", previous);
nextButton.addEventListener("click", next);
knownButton.addEventListener("click", () => void rate("known"));
unknownButton.addEventListener("click", () => void rate("unknown"));
retryButton.addEventListener("click", () => void failure?.run());
logoutButton?.addEventListener("click", logout);

addEventListener("pagehide", () => {
  if (releaseWriterLock) releaseWriterLock();
  releaseWriterLock = null;
  writer = false;
  if (nextDueTimer !== null) clearTimeout(nextDueTimer);
  nextDueTimer = null;
});
addEventListener("pageshow", (pageShow) => {
  if (pageShow.persisted && !writer) void boot();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible"
      && writer
      && failure === null
      && nextDue != null
      && Date.parse(nextDue) <= Date.now()) {
    void loadCard();
  }
});

void boot();
