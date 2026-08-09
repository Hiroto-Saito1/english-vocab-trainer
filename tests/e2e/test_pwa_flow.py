from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from json import dumps
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from argon2 import PasswordHasher
from playwright.sync_api import Page, Route, expect

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.auth import AuthService
from english_vocab_trainer.web.container import AppContainer


@pytest.fixture
def pwa_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    database = tmp_path / "vocab.db"
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    provider = SQLiteRepositoryProvider(database)
    repository = provider.for_user("local-user")
    try:
        for index in range(3):
            audio_key = f"word-{index}.mp3"
            (audio_root / audio_key).write_bytes(b"ID3\x04\x00\x00")
            repository.add_word(
                Word(
                    f"word-{index}",
                    f"word {index}",
                    2,
                    f"An English definition for word {index} includes an example.",
                    audio_key,
                    Tier.UPPER if index % 2 == 0 else Tier.ULTRA,
                )
            )
    finally:
        repository.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                AppContainer(provider, FilesystemAudioStore(audio_root), "test", "local-user")
            ),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", database
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def production_auth_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A live production-mode app; only the cookie Secure flag is relaxed for HTTP Chromium."""
    database = tmp_path / "production.db"
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    provider = SQLiteRepositoryProvider(database)
    repository = provider.for_user("primary")
    try:
        for index in range(2):
            audio_key = f"protected-{index}.mp3"
            (audio_root / audio_key).write_bytes(b"ID3\x04\x00\x00protected")
            repository.add_word(
                Word(
                    f"protected-{index}",
                    f"protected word {index}",
                    2,
                    f"An English definition for protected word {index}.",
                    audio_key,
                )
            )
    finally:
        repository.close()
    auth = AuthService(
        PasswordHasher().hash("browser-password"),
        b"p" * 32,
        SQLiteLoginAttemptLimiter(database),
        secure_cookies=False,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                AppContainer(provider, FilesystemAudioStore(audio_root), "production", auth=auth)
            ),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", database
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def event_rows(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute("SELECT rating, voided_at FROM review_events ORDER BY reviewed_at")
        )
    finally:
        connection.close()


def wait_for_rows(database: Path, count: int) -> list[sqlite3.Row]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = event_rows(database)
        if len(rows) >= count:
            return rows
        time.sleep(0.03)
    raise AssertionError(f"expected {count} review events, got {event_rows(database)}")


def wait_for_audio_cache(page: Page) -> list[str]:
    worker_ready = page.evaluate(
        """async () => Promise.race([
          navigator.serviceWorker.ready.then(() => true),
          new Promise((resolve) => setTimeout(() => resolve(false), 10_000)),
        ])"""
    )
    assert worker_ready is True, "service worker did not become ready within 10 seconds"
    page.wait_for_function(
        """() => {
          if (!navigator.serviceWorker.controller || !window.__pwa) return false;
          const state = window.__pwa.getState(), cache = window.__pwa.getAudioCacheState();
          return Boolean(
            state.sessionId && cache.ready && cache.sessionId === state.sessionId &&
            cache.expected === state.cards.length && cache.cached === cache.expected &&
            cache.failed === 0
          );
        }""",
        timeout=10_000,
    )
    urls = page.evaluate(
        """async () => caches.open("pwa-private-audio-v3")
          .then((cache) => cache.keys())
          .then((keys) => keys.map((key) => key.url).sort())"""
    )
    return cast(list[str], urls)


def active_audio_urls(page: Page) -> list[str]:
    return cast(
        list[str],
        page.evaluate(
            """() => window.__pwa.getState().cards
          .map((card) => new URL(card.audio_url, location.origin).href).sort()"""
        ),
    )


def wait_for_empty_outbox(page: Page) -> None:
    empty = page.evaluate(
        """async () => {
          const deadline = Date.now() + 10_000;
          const eventCount = () => Promise.race([
            new Promise((resolve, reject) => {
              const request = indexedDB.open("english-vocab-trainer", 1);
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const db = request.result;
                const count = db.transaction("events").objectStore("events").count();
                count.onerror = () => { db.close(); reject(count.error); };
                count.onsuccess = () => { db.close(); resolve(count.result); };
              };
            }),
            new Promise((_, reject) => setTimeout(
              () => reject(new Error("outbox probe timed out")), 1_000
            )),
          ]);
          while (Date.now() < deadline) {
            if (await eventCount() === 0) return true;
            await new Promise((resolve) => setTimeout(resolve, 50));
          }
          return false;
        }"""
    )
    assert empty is True, "outbox did not empty within 10 seconds"


def wait_for_outbox_ids(page: Page, expected: list[str]) -> None:
    matched = page.evaluate(
        """async (expected) => {
          const deadline = Date.now() + 10_000;
          const eventIds = () => Promise.race([
            new Promise((resolve, reject) => {
              const request = indexedDB.open("english-vocab-trainer", 1);
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const db = request.result;
                const keys = db.transaction("events").objectStore("events").getAllKeys();
                keys.onerror = () => { db.close(); reject(keys.error); };
                keys.onsuccess = () => { db.close(); resolve(keys.result); };
              };
            }),
            new Promise((_, reject) => setTimeout(
              () => reject(new Error("outbox ID probe timed out")), 1_000
            )),
          ]);
          while (Date.now() < deadline) {
            const ids = await eventIds();
            if (JSON.stringify(ids) === JSON.stringify(expected)) return true;
            await new Promise((resolve) => setTimeout(resolve, 50));
          }
          return false;
        }""",
        expected,
    )
    assert matched is True, f"outbox IDs did not become {expected!r} within 10 seconds"


@pytest.mark.e2e
def test_mobile_pwa_known_unknown_reload_and_undo(page: Page, pwa_server: tuple[str, Path]) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page.locator("#term")).to_be_hidden()
    expect(page.locator("#tier")).to_be_hidden()
    expect(page.locator("#transcript")).to_be_hidden()
    expect(page.locator("#progress")).to_have_text("1 of 3")

    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#progress")).to_have_text("2 of 3")
    assert wait_for_rows(database, 1)[0]["rating"] == "easy"

    page.get_by_role("button", name="Unknown", exact=True).click()
    expect(page.locator("#term")).to_be_visible()
    expect(page.locator("#tier")).to_contain_text("· SVL 2")
    expect(page.locator("#transcript")).to_contain_text("English definition")
    assert wait_for_rows(database, 2)[1]["rating"] == "again"
    page.reload()
    expect(page.locator("#term")).to_be_visible()
    assert len(event_rows(database)) == 2

    page.get_by_role("button", name="Continue").click()
    expect(page.locator("#progress")).to_have_text("3 of 3")
    page.get_by_role("button", name="Unknown", exact=True).click()
    assert len(wait_for_rows(database, 3)) == 3
    page.get_by_role("button", name="Undo").click()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and event_rows(database)[2]["voided_at"] is None:
        time.sleep(0.03)
    assert event_rows(database)[2]["voided_at"] is not None
    expect(page.locator("#progress")).to_have_text("3 of 3")

    page.context.set_offline(True)
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#card")).to_contain_text("Waiting for the next review:")
    page.get_by_role("button", name="Undo").click()
    expect(page.locator("#progress")).to_have_text("3 of 3")
    assert len(event_rows(database)) == 3
    page.context.set_offline(False)


@pytest.mark.e2e
def test_mobile_pwa_empty_daily_session_says_all_caught_up(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    for count in range(1, 4):
        page.get_by_role("button", name="Known", exact=True).click()
        wait_for_rows(database, count)
    expect(page.locator("#card")).to_have_text("Daily study complete.")
    page.reload()
    expect(page.locator("#card")).to_have_text("All caught up / nothing due.")
    expect(page.locator("#progress")).to_have_text("0 of 0")


@pytest.mark.e2e
def test_unknown_learning_replays_after_injected_ten_minute_clock(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    page.goto(base_url)
    page.get_by_role("button", name="Unknown", exact=True).click()
    page.get_by_role("button", name="Continue").click()
    queued = page.evaluate("window.__pwa.getState().learningQueue[0]")
    initial_card = page.evaluate(
        "window.__pwa.getState().cards[window.__pwa.getState().current].id"
    )
    page.evaluate(
        "(due) => { window.__pwaTestClock = { now: () => due }; window.__pwa.render(); }",
        queued["due_at"],
    )
    expect(page.locator("#card")).to_have_text("Listen, then choose.")
    assert (
        page.evaluate("window.__pwa.getState().cards[window.__pwa.getState().current].id")
        == initial_card
    )
    page.wait_for_timeout(50)  # A due queue must not schedule render(0) while a card remains.
    assert (
        page.evaluate("window.__pwa.getState().cards[window.__pwa.getState().current].id")
        == initial_card
    )
    page.get_by_role("button", name="Known", exact=True).click()
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#card")).to_have_text("Listen, then choose.")
    replayed = page.evaluate("window.__pwa.getState().learningQueue[0]")
    assert replayed["eventId"] == queued["eventId"]
    # A learning retry remains revealable even after its five-second Undo data
    # expires, and Continue never advances the initial admission cursor.
    page.get_by_role("button", name="Unknown", exact=True).click()
    expect(page.locator("#continue")).to_be_visible()
    retry = page.evaluate("window.__pwa.getState().learningQueue[0]")
    undo_deadline = page.evaluate("window.__pwa.getState().undoDeadline")
    page.evaluate(
        "(deadline) => { window.__pwaTestClock = { now: () => deadline + 1 }; "
        "window.__pwa.render(); }",
        undo_deadline,
    )
    assert page.evaluate("window.__pwa.getState().event") is None
    resumed = page.evaluate("window.__pwa.getState()")
    assert resumed["revealedLearningWordId"], resumed
    assert resumed["phase"] == "revealed", resumed
    expect(page.locator("#continue")).to_be_visible()
    page.get_by_role("button", name="Continue", exact=True).click()
    assert page.evaluate("window.__pwa.getState().current") == page.evaluate(
        "window.__pwa.getState().cards.length"
    )
    expect(page.locator("#card")).to_contain_text("Waiting for the next review:")
    retry = page.evaluate("window.__pwa.getState().learningQueue[0]")
    page.evaluate(
        "(due) => { window.__pwaTestClock = { now: () => due }; window.__pwa.render(); }",
        retry["due_at"],
    )
    expect(page.locator("#card")).to_have_text("Listen, then choose.")
    # A second expired retry also leaves the cursor clamped at the initial total.
    page.get_by_role("button", name="Unknown", exact=True).click()
    expect(page.locator("#continue")).to_be_visible()
    retry_again = page.evaluate("window.__pwa.getState().learningQueue[0]")
    undo_deadline = page.evaluate("window.__pwa.getState().undoDeadline")
    page.evaluate(
        "(deadline) => { window.__pwaTestClock = { now: () => deadline + 1 }; "
        "window.__pwa.render(); }",
        undo_deadline,
    )
    page.get_by_role("button", name="Continue", exact=True).click()
    assert page.evaluate("window.__pwa.getState().current") == page.evaluate(
        "window.__pwa.getState().cards.length"
    )
    page.evaluate(
        "(due) => { window.__pwaTestClock = { now: () => due }; window.__pwa.render(); }",
        retry_again["due_at"],
    )
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#card")).to_have_text("Daily study complete.")


@pytest.mark.e2e
def test_sync_keeps_only_conflicted_indexeddb_events(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    acknowledged = "00000000-0000-0000-0000-000000000101"
    conflicted = "00000000-0000-0000-0000-000000000102"

    def batch_response(route: Route) -> None:
        # This is the only mocked request: it models a mixed server batch
        # result while the page/session/audio still use the live ASGI server.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"results":[{"id":"' + acknowledged + '","word_id":"word-0","status":"applied"},'
                '{"id":"' + conflicted + '","word_id":"word-1","status":"conflict"}],'
                '"acknowledged":["' + acknowledged + '"]}'
            ),
        )

    page.route("**/api/v1/review-events/batch", batch_response)
    page.goto(base_url)
    wait_for_audio_cache(page)
    page.evaluate(
        """async ([first, second]) => Promise.race([((async () => {
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 1);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          await new Promise((resolve, reject) => {
            const tx = db.transaction("events", "readwrite");
            tx.objectStore("events").put({
              id:first, word_id:"word-0", action:"known", reviewed_at:"2026-01-01T00:00:00Z"
            });
            tx.objectStore("events").put({
              id:second, word_id:"word-1", action:"unknown", reviewed_at:"2026-01-01T00:00:01Z"
            });
            tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
          });
          db.close();
          window.dispatchEvent(new Event("online"));
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("conflict outbox setup timed out")), 10_000
        ))])""",
        [acknowledged, conflicted],
    )
    wait_for_outbox_ids(page, [conflicted])
    expect(page.locator("#status")).to_have_text(
        "Some review updates need attention and remain on this device."
    )


@pytest.mark.e2e
def test_online_reconnect_chunks_more_than_two_hundred_outbox_events(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    batches: list[int] = []

    def acknowledge(route: Route) -> None:
        events = route.request.post_data_json
        assert isinstance(events, list)
        batches.append(len(events))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=dumps(
                {
                    "results": [
                        {"id": event["id"], "word_id": event["word_id"], "status": "applied"}
                        for event in events
                    ],
                    "acknowledged": [event["id"] for event in events],
                }
            ),
        )

    page.route("**/api/v1/review-events/batch", acknowledge)
    page.goto(base_url)
    wait_for_audio_cache(page)
    page.context.set_offline(True)
    events = [
        {
            "id": f"00000000-0000-0000-0000-{index:012d}",
            "word_id": "word-0",
            "action": "known",
            "reviewed_at": "2026-01-01T00:00:00Z",
        }
        for index in range(1, 206)
    ]
    page.evaluate(
        """async (events) => new Promise((resolve, reject) => {
          const request = indexedDB.open("english-vocab-trainer", 1);
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction("events", "readwrite");
            events.forEach((event) => tx.objectStore("events").put(event));
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onerror = () => { db.close(); reject(tx.error); };
          };
        })""",
        events,
    )
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_empty_outbox(page)
    assert batches == [100, 100, 5]


@pytest.mark.e2e
def test_offline_audio_outbox_rotation_and_private_reset(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    initial_cache_urls = wait_for_audio_cache(page)
    initial_urls = active_audio_urls(page)
    assert initial_cache_urls == initial_urls
    shell_keys = page.evaluate(
        """async () => Promise.race([
          caches.open("pwa-shell-v3").then((cache) => cache.keys()).then(
            (keys) => keys.map((key) => new URL(key.url).pathname)
          ),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error("shell cache inspection timed out")), 10_000
          )),
        ])"""
    )
    assert all(not key.startswith("/api/") for key in shell_keys)

    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#card")).to_have_text("Listen, then choose.")
    assert wait_for_audio_cache(page) == initial_urls
    cached_url = active_audio_urls(page)[0]
    normal, sliced, open_ended, suffix = page.evaluate(
        """async (url) => Promise.race([((async () => {
          const whole = await fetch(url);
          const range = await fetch(url, {headers: {Range: "bytes=1-3"}});
          return [
            {status: whole.status, bytes: [...new Uint8Array(await whole.arrayBuffer())]},
            {
              status: range.status, range: range.headers.get("Content-Range"),
              length: range.headers.get("Content-Length"),
              acceptRanges: range.headers.get("Accept-Ranges"),
              contentType: range.headers.get("Content-Type"), etag: range.headers.get("ETag"),
              bytes: [...new Uint8Array(await range.arrayBuffer())]
            },
            await fetch(url, {headers: {Range: "bytes=2-"}}).then(async (response) => ({
              status: response.status, range: response.headers.get("Content-Range"),
              bytes: [...new Uint8Array(await response.arrayBuffer())]
            })),
            await fetch(url, {headers: {Range: "bytes=-2"}}).then(async (response) => ({
              status: response.status, range: response.headers.get("Content-Range"),
              bytes: [...new Uint8Array(await response.arrayBuffer())]
            }))
          ];
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("offline audio fetch timed out")), 10_000
        ))])""",
        cached_url,
    )
    assert normal["status"] == 200
    audio_size = len(normal["bytes"])
    assert sliced == {
        "status": 206,
        "range": f"bytes 1-3/{audio_size}",
        "length": "3",
        "acceptRanges": "bytes",
        "contentType": "audio/mpeg",
        "etag": sliced["etag"],
        "bytes": normal["bytes"][1:4],
    }
    assert sliced["etag"]
    assert open_ended == {
        "status": 206,
        "range": f"bytes 2-{audio_size - 1}/{audio_size}",
        "bytes": normal["bytes"][2:],
    }
    assert suffix == {
        "status": 206,
        "range": f"bytes {audio_size - 2}-{audio_size - 1}/{audio_size}",
        "bytes": normal["bytes"][-2:],
    }
    invalid = page.evaluate(
        """async (url) => Promise.race([((async () => {
          return Promise.all(["bytes=0-1,3-4", "bytes=999-1000", "not-a-range"].map(
            async (value) => {
              const response = await fetch(url, {headers: {Range: value}});
              return [response.status, response.headers.get("Content-Range")];
            }
          ));
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("offline range fetch timed out")), 10_000
        ))])""",
        cached_url,
    )
    assert invalid == [[416, f"bytes */{audio_size}"]] * 3

    page.get_by_role("button", name="Unknown", exact=True).click()
    expect(page.locator("#term")).to_be_visible()
    page.get_by_role("button", name="Continue").click()
    page.get_by_role("button", name="Known", exact=True).click()
    assert event_rows(database) == []

    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    rows = wait_for_rows(database, 2)
    assert [row["rating"] for row in rows] == ["again", "easy"]
    wait_for_empty_outbox(page)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_empty_outbox(page)
    assert len(event_rows(database)) == 2

    page.get_by_role("button", name="Known", exact=True).click()
    wait_for_rows(database, 3)
    expect(page.locator("#card")).to_contain_text("Waiting for the next review:")
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#card")).to_contain_text("Waiting for the next review:")
    page.context.set_offline(False)
    queued = page.evaluate("window.__pwa.getState().learningQueue[0]")
    page.evaluate(
        "(due) => { window.__pwaTestClock = { now: () => due }; window.__pwa.render(); }",
        queued["due_at"],
    )
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#card")).to_have_text("Daily study complete.")
    audio_root = database.parent / "audio"
    (audio_root / "word-new.mp3").write_bytes(b"ID3\x04\x00\x00new")
    provider = SQLiteRepositoryProvider(database)
    repository = provider.for_user("local-user")
    try:
        repository.add_word(
            Word("word-new", "new word", 2, "An English definition for a new word.", "word-new.mp3")
        )
    finally:
        repository.close()

    page.reload()
    cache_urls = wait_for_audio_cache(page)
    next_urls = active_audio_urls(page)
    assert cache_urls == next_urls
    assert any(url.endswith("word-new") for url in next_urls)
    assert not set(initial_urls).difference(next_urls).intersection(cache_urls)

    page.context.set_offline(True)
    page.evaluate(
        """async () => Promise.race([((async () => {
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 1);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          await new Promise((resolve, reject) => {
            const tx = db.transaction("events", "readwrite");
            tx.objectStore("events").put({
              id: crypto.randomUUID(), word_id: "word-new", action: "known",
              reviewed_at: new Date().toISOString()
            });
            tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
          });
          db.close();
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("outbox setup timed out")), 10_000
        ))])"""
    )
    page.evaluate(
        """async () => Promise.race([
          window.clearPrivateCaches(),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error("private reset timed out")), 10_000
          )),
        ])"""
    )
    private_keys, db_state = page.evaluate(
        """async () => Promise.race([((async () => {
          const privateKeys = (await caches.keys()).filter((key) =>
            key === "pwa-private-audio-v3" || key.startsWith("pwa-user-")
          );
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 1);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          const values = await Promise.all(["events", "state"].map((name) =>
            new Promise((resolve, reject) => {
              const request = db.transaction(name).objectStore(name).getAll();
              request.onsuccess = () => resolve(request.result);
              request.onerror = () => reject(request.error);
            })
          ));
          db.close();
          return [privateKeys, values];
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("private reset verification timed out")), 10_000
        ))])"""
    )
    assert private_keys == []
    assert db_state == [[], []]
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#status")).to_have_text("Reconnect to start a new study session.")


@pytest.mark.e2e
def test_private_reset_cancels_an_inflight_audio_preload(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    page.add_init_script(
        """(() => {
          const nativeFetch = window.fetch.bind(window);
          let release;
          const gate = new Promise((resolve) => { release = resolve; });
          window.__releaseAudioPreload = release;
          window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            return url.includes("/api/v1/audio/")
              ? gate.then(() => nativeFetch(input, init))
              : nativeFetch(input, init);
          };
        })()"""
    )
    page.goto(base_url)
    page.wait_for_function(
        """() => {
          const cache = window.__pwa.getAudioCacheState();
          return Boolean(cache.sessionId && cache.expected > 0 && !cache.ready);
        }""",
        timeout=10_000,
    )
    remaining = page.evaluate(
        """async () => Promise.race([((async () => {
          const reset = window.clearPrivateCaches();
          window.__releaseAudioPreload();
          await reset;
          return (await caches.open("pwa-private-audio-v3")).keys().then(
            (keys) => keys.length
          );
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("inflight private reset timed out")), 10_000
        ))])"""
    )
    assert remaining == 0


@pytest.mark.e2e
def test_mobile_production_login_csrf_and_logout_privacy_boundary(
    page: Page, production_auth_server: tuple[str, Path]
) -> None:
    base_url, _ = production_auth_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page).to_have_url(f"{base_url}/login")
    expect(page.locator("h2")).to_have_text("Sign in")

    page.locator("#password").fill("wrong-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator(".error")).to_have_text("Sign in failed. Please try again.")
    assert "browser-password" not in page.content()

    page.locator("#password").fill("browser-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(f"{base_url}/")
    expect(page.locator("#card")).to_have_text("Listen, then choose.")
    cached_urls = wait_for_audio_cache(page)
    assert cached_urls == active_audio_urls(page)

    session_status, audio_status, csrf_mutation = page.evaluate(
        """async () => Promise.race([((async () => {
          const state = window.__pwa.getState();
          const token = document.cookie.split(";").map((value) => value.trim()).find(
            (value) => value.startsWith("vocab-csrf=")
          ).split("=").slice(1).join("=");
          const session = await fetch("/api/v1/sessions?mode=daily");
          const audio = await fetch(state.cards[0].audio_url);
          const review = await fetch("/api/v1/review-events/batch", {
            method: "POST",
            headers: {"content-type": "application/json", "X-CSRF-Token": token},
            body: JSON.stringify([{
              id: crypto.randomUUID(), word_id: state.cards[0].id, action: "known",
              reviewed_at: new Date().toISOString()
            }])
          });
          return [session.status, audio.status, review.status];
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("production protected request timed out")), 10_000
        ))])"""
    )
    assert [session_status, audio_status, csrf_mutation] == [200, 200, 200]

    # Seed a durable event to show that logout clears both user stores, not just audio.
    page.evaluate(
        """async () => Promise.race([((async () => {
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 1);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          await new Promise((resolve, reject) => {
            const tx = db.transaction("events", "readwrite");
            tx.objectStore("events").put({
              id: crypto.randomUUID(), word_id: "protected-0", action: "known",
              reviewed_at: new Date().toISOString()
            });
            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error);
          });
          db.close();
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("production outbox setup timed out")), 10_000
        ))])"""
    )
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page).to_have_url(f"{base_url}/login")
    cookie_names = [cookie["name"] for cookie in page.context.cookies()]
    assert "vocab-session" not in cookie_names and "vocab-csrf" not in cookie_names

    private_keys, db_values, api_status = page.evaluate(
        """async () => Promise.race([((async () => {
          const privateKeys = (await caches.keys()).filter((key) =>
            key === "pwa-private-audio-v3" || key.startsWith("pwa-user-")
          );
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 1);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          const values = await Promise.all(["events", "state"].map((name) =>
            new Promise((resolve, reject) => {
              const request = db.transaction(name).objectStore(name).getAll();
              request.onsuccess = () => resolve(request.result);
              request.onerror = () => reject(request.error);
            })
          ));
          db.close();
          return [privateKeys, values, (await fetch("/api/v1/progress")).status];
        })()), new Promise((_, reject) => setTimeout(
          () => reject(new Error("production logout verification timed out")), 10_000
        ))])"""
    )
    assert private_keys == []
    assert db_values == [[], []]
    assert api_status == 401
    page.evaluate(
        """async () => Promise.race([caches.open("pwa-shell-v3").then(
          (cache) => cache.delete(new URL("/", location.origin).href)
        ), new Promise((_, reject) => setTimeout(
          () => reject(new Error("shell cache reset timed out")), 10_000
        ))])"""
    )
    page.goto(base_url)
    expect(page).to_have_url(f"{base_url}/login")
    cached_login_root = page.evaluate(
        """async () => Promise.race([caches.open("pwa-shell-v3").then(
          async (cache) => Boolean(await cache.match(new URL("/", location.origin).href))
        ), new Promise((_, reject) => setTimeout(
          () => reject(new Error("redirect cache check timed out")), 10_000
        ))])"""
    )
    assert cached_login_root is False
