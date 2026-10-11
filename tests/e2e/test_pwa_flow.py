from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from json import dumps
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from argon2 import PasswordHasher
from fastapi import FastAPI
from playwright.sync_api import Page, Route, expect

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.auth import AuthService
from english_vocab_trainer.web.container import AppContainer


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn stopped before startup completed")
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _seed(database: Path, audio_root: Path, prefix: str, count: int) -> None:
    audio_root.mkdir()
    with SQLiteVocabularyRepository(database) as repository:
        for index in range(count):
            (audio_root / f"{prefix}-{index}.mp3").write_bytes(b"ID3\x04\x00\x00audio")
        repository.bulk_upsert_words(
            [
                Word(
                    f"{prefix}-{index}",
                    f"{prefix} word {index}",
                    index + 1,
                    f"An English definition for {prefix} word {index}.",
                    f"{prefix}-{index}.mp3",
                    Tier.UPPER if index % 2 == 0 else Tier.ULTRA,
                )
                for index in range(count)
            ]
        )


@pytest.fixture
def pwa_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    database = tmp_path / "vocab.db"
    audio_root = tmp_path / "audio"
    _seed(database, audio_root, "word", 3)
    app = create_app(AppContainer(database, FilesystemAudioStore(audio_root), "test"))
    with _serve(app) as base_url:
        yield base_url, database


@pytest.fixture
def production_auth_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A live production app; cookie Secure is relaxed only for HTTP Chromium."""
    database = tmp_path / "production.db"
    audio_root = tmp_path / "audio"
    _seed(database, audio_root, "protected", 2)
    auth = AuthService(
        PasswordHasher().hash("browser-password"),
        b"p" * 32,
        SQLiteLoginAttemptLimiter(database),
        secure_cookies=False,
    )
    app = create_app(
        AppContainer(database, FilesystemAudioStore(audio_root), "production", auth=auth)
    )
    with _serve(app) as base_url:
        yield base_url, database


def event_rows(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                "SELECT id,rating,voided_at FROM review_events ORDER BY reviewed_at,id"
            )
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


def wait_for_voided_event(database: Path, event_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = event_rows(database)
        if any(row["id"] == event_id and row["voided_at"] is not None for row in rows):
            return
        time.sleep(0.03)
    raise AssertionError(f"event {event_id} was not voided: {event_rows(database)}")


def wait_for_audio_cache(page: Page) -> list[str]:
    worker_ready = page.evaluate(
        """async () => Promise.race([
          navigator.serviceWorker.ready.then(() => true),
          new Promise((resolve) => setTimeout(() => resolve(false), 10_000)),
        ])"""
    )
    assert worker_ready is True
    page.wait_for_function(
        """() => {
          if (!navigator.serviceWorker.controller || !window.__pwa) return false;
          const cache = window.__pwa.getAudioCacheState();
          return cache.ready && cache.cached === cache.expected && cache.failed === 0;
        }""",
        timeout=10_000,
    )
    return cast(
        list[str],
        page.evaluate(
            """async () => caches.open("pwa-private-audio-v4")
              .then((cache) => cache.keys())
              .then((keys) => keys.map((key) => key.url).sort())"""
        ),
    )


def wait_for_store_ids(page: Page, store: str, expected: list[str]) -> None:
    matched = page.evaluate(
        """async ({store, expected}) => {
          const deadline = Date.now() + 10_000;
          while (Date.now() < deadline) {
            const ids = await new Promise((resolve, reject) => {
              const request = indexedDB.open("english-vocab-trainer", 2);
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const db = request.result;
                const keys = db.transaction(store).objectStore(store).getAllKeys();
                keys.onerror = () => { db.close(); reject(keys.error); };
                keys.onsuccess = () => { db.close(); resolve(keys.result); };
              };
            });
            if (JSON.stringify(ids) === JSON.stringify(expected)) return true;
            await new Promise((resolve) => setTimeout(resolve, 50));
          }
          return false;
        }""",
        {"store": store, "expected": expected},
    )
    assert matched is True, f"{store} IDs did not become {expected!r}"


def put_outbox_events(page: Page, events: list[dict[str, str]]) -> None:
    page.evaluate(
        """async (events) => {
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 2);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          try {
            await new Promise((resolve, reject) => {
              const tx = db.transaction("events", "readwrite");
              events.forEach((event) => tx.objectStore("events").put(event));
              tx.oncomplete = resolve;
              tx.onerror = () => reject(tx.error);
              tx.onabort = () => reject(tx.error);
            });
          } finally { db.close(); }
        }""",
        events,
    )


def private_storage_state(page: Page) -> tuple[list[str], list[list[object]]]:
    return cast(
        tuple[list[str], list[list[object]]],
        page.evaluate(
            """async () => {
              const privateKeys = (await caches.keys()).filter(
                (key) => key === "pwa-private-audio-v4"
              );
              const db = await new Promise((resolve, reject) => {
                const request = indexedDB.open("english-vocab-trainer", 2);
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
              });
              try {
                const values = await Promise.all(["events", "failedEvents", "state"].map(
                  (name) => new Promise((resolve, reject) => {
                    const request = db.transaction(name).objectStore(name).getAll();
                    request.onsuccess = () => resolve(request.result);
                    request.onerror = () => reject(request.error);
                  })
                ));
                return [privateKeys, values];
              } finally { db.close(); }
            }"""
        ),
    )


def reveal_and_answer(page: Page, answer: str) -> None:
    page.get_by_role("button", name="Show answer", exact=True).click()
    page.get_by_role("button", name=answer, exact=True).click()
    page.wait_for_function("() => window.__pwa.getState().phase !== 'answer'")


@pytest.mark.e2e
def test_mobile_answer_gate_reload_and_undo(page: Page, pwa_server: tuple[str, Path]) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page.locator("#card")).to_have_text("Listen to the word, then show the answer.")
    expect(page.get_by_role("button", name="Show answer", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Known", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name="Unknown", exact=True)).to_be_hidden()
    assert page.locator("#audio").get_attribute("controls") is None

    page.get_by_role("button", name="Show answer", exact=True).click()
    expect(page.locator("#term")).to_be_visible()
    expect(page.locator("#transcript")).to_contain_text("English definition")
    expect(page.get_by_role("button", name="Show answer", exact=True)).to_be_hidden()
    page.get_by_role("button", name="Unknown", exact=True).click()
    first = wait_for_rows(database, 1)[0]
    assert first["rating"] == "again"
    expect(page.locator("#progress")).to_contain_text("Initial: 2 remaining")

    page.reload()
    expect(page.locator("#card")).to_have_text("Listen to the word, then show the answer.")
    expect(page.get_by_role("button", name="Undo last answer", exact=True)).to_be_visible()
    page.get_by_role("button", name="Undo last answer", exact=True).click()
    wait_for_voided_event(database, first["id"])
    expect(page.locator("#term")).to_be_visible()
    expect(page.get_by_role("button", name="Known", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Undo last answer", exact=True)).to_be_hidden()

    page.get_by_role("button", name="Known", exact=True).click()
    assert wait_for_rows(database, 2)[1]["rating"] == "good"


@pytest.mark.e2e
def test_set_completion_survives_reload_until_explicit_next_set(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    for count in range(1, 4):
        reveal_and_answer(page, "Known")
        wait_for_rows(database, count)
    expect(page.locator("#card")).to_have_text("Set complete.")
    expect(page.get_by_role("button", name="Start next set", exact=True)).to_be_visible()
    page.reload()
    expect(page.locator("#card")).to_have_text("Set complete.")
    assert len(event_rows(database)) == 3
    page.get_by_role("button", name="Start next set", exact=True).click()
    expect(page.locator("#card")).to_have_text("All caught up. Nothing is due.")


@pytest.mark.e2e
def test_due_relearning_wins_at_next_card_boundary_and_wait_can_finish(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    page.goto(base_url)
    expect(page.locator("#card")).to_have_text("Listen to the word, then show the answer.")
    first_word = page.evaluate("window.__pwa.getState().active.word.id")
    reveal_and_answer(page, "Unknown")
    wait_for_store_ids(page, "events", [])
    queued = page.evaluate("window.__pwa.getState().learningQueue[0]")
    second_word = page.evaluate("window.__pwa.getState().active.word.id")
    assert second_word != first_word
    page.evaluate(
        "(due) => { window.__pwaTestClock = { now: () => due }; window.__pwa.render(); }",
        queued["due_at"],
    )
    assert page.evaluate("window.__pwa.getState().active.word.id") == second_word
    reveal_and_answer(page, "Known")
    active = page.evaluate("window.__pwa.getState().active")
    assert active == {"kind": "learning", "word": queued["word"]}
    expect(page.locator("#progress")).to_contain_text("Initial: 1 remaining · Relearning: 1")
    reveal_and_answer(page, "Known")
    assert page.evaluate("window.__pwa.getState().active.kind") == "initial"
    reveal_and_answer(page, "Unknown")
    expect(page.locator("#card")).to_contain_text("Waiting for relearning:")
    page.get_by_role("button", name="Finish for now", exact=True).click()
    expect(page.locator("#card")).to_contain_text("Finished for now.")
    page.reload()
    expect(page.locator("#card")).to_contain_text("Finished for now.")
    expect(page.get_by_role("button", name="Start next set", exact=True)).to_be_visible()


@pytest.mark.e2e
def test_suspend_and_restore_word(page: Page, pwa_server: tuple[str, Path]) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    expect(page.locator("#card")).to_have_text("Listen to the word, then show the answer.")
    active = page.evaluate("window.__pwa.getState().active.word")
    page.get_by_role("button", name="Suspend word", exact=True).click()
    expect(page.locator("#notice")).to_have_text(f"{active['term']} was suspended.")
    assert page.evaluate("window.__pwa.getState().active.word.id") != active["id"]
    with SQLiteVocabularyRepository(database) as repository:
        assert [word.id for word in repository.list_excluded_words()] == [active["id"]]
        assert active["id"] not in {word.id for word in repository.list_words()}

    page.get_by_role("button", name="Manage suspended words", exact=True).click()
    expect(page.locator("#exclusions-list")).to_contain_text(active["term"])
    page.get_by_role("button", name="Restore", exact=True).click()
    expect(page.locator("#exclusions-empty")).to_have_text("No suspended words.")
    with SQLiteVocabularyRepository(database) as repository:
        assert repository.list_excluded_words() == []


@pytest.mark.e2e
def test_terminal_sync_result_is_quarantined_and_can_be_discarded(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    acknowledged = "00000000-0000-0000-0000-000000000101"
    conflicted = "00000000-0000-0000-0000-000000000102"

    def batch_response(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=dumps(
                {
                    "results": [
                        {"id": acknowledged, "status": "applied", "due_at": None},
                        {"id": conflicted, "status": "conflict", "due_at": None},
                    ]
                }
            ),
        )

    page.route("**/api/v1/review-events/batch", batch_response)
    page.goto(base_url)
    put_outbox_events(
        page,
        [
            {
                "id": acknowledged,
                "word_id": "word-0",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": conflicted,
                "word_id": "word-1",
                "action": "unknown",
                "reviewed_at": "2026-01-01T00:00:01Z",
            },
        ],
    )
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_store_ids(page, "events", [])
    wait_for_store_ids(page, "failedEvents", [conflicted])
    expect(page.locator("#sync-status")).to_contain_text("need attention and will not be retried")
    page.get_by_role("button", name="Discard failed reviews", exact=True).click()
    wait_for_store_ids(page, "failedEvents", [])
    expect(page.locator("#sync-status")).to_have_text("Sync: all reviews saved.")


@pytest.mark.e2e
def test_reconnect_chunks_more_than_two_hundred_reviews(
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
                        {"id": event["id"], "status": "applied", "due_at": None} for event in events
                    ]
                }
            ),
        )

    page.route("**/api/v1/review-events/batch", acknowledge)
    page.goto(base_url)
    page.context.set_offline(True)
    put_outbox_events(
        page,
        [
            {
                "id": f"00000000-0000-0000-0000-{index:012d}",
                "word_id": "word-0",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:00Z",
            }
            for index in range(1, 206)
        ],
    )
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_store_ids(page, "events", [])
    assert batches == [100, 100, 5]


@pytest.mark.e2e
def test_offline_audio_range_outbox_and_private_reset(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    cached_urls = wait_for_audio_cache(page)
    assert len(cached_urls) == 3
    shell_paths = page.evaluate(
        """async () => caches.open("pwa-shell-v4").then((cache) => cache.keys()).then(
          (keys) => keys.map((key) => new URL(key.url).pathname)
        )"""
    )
    assert all(not path.startswith("/api/") for path in shell_paths)

    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#card")).to_have_text("Listen to the word, then show the answer.")
    whole, sliced = page.evaluate(
        """async (url) => {
          const whole = await fetch(url);
          const range = await fetch(url, {headers: {Range: "bytes=1-3"}});
          return [
            {status: whole.status, bytes: [...new Uint8Array(await whole.arrayBuffer())]},
            {
              status: range.status,
              range: range.headers.get("Content-Range"),
              bytes: [...new Uint8Array(await range.arrayBuffer())]
            }
          ];
        }""",
        cached_urls[0],
    )
    assert whole["status"] == 200
    assert sliced == {
        "status": 206,
        "range": f"bytes 1-3/{len(whole['bytes'])}",
        "bytes": whole["bytes"][1:4],
    }

    reveal_and_answer(page, "Unknown")
    reveal_and_answer(page, "Known")
    assert event_rows(database) == []
    expect(page.locator("#sync-status")).to_contain_text("reviews kept for retry")
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    assert [row["rating"] for row in wait_for_rows(database, 2)] == ["again", "good"]
    wait_for_store_ids(page, "events", [])

    page.context.set_offline(True)
    put_outbox_events(
        page,
        [
            {
                "id": "00000000-0000-0000-0000-000000000201",
                "word_id": "word-2",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    page.evaluate("window.clearPrivateCaches()")
    private_keys, db_state = private_storage_state(page)
    assert private_keys == []
    assert db_state == [[], [], []]


@pytest.mark.e2e
def test_production_logout_aborts_preload_and_preserves_unsynced_data_until_confirmed(
    page: Page, production_auth_server: tuple[str, Path]
) -> None:
    base_url, _ = production_auth_server
    aborts: list[bool] = []
    page.expose_function("recordPreloadAbort", lambda: aborts.append(True))
    page.add_init_script(
        """(() => {
          const nativeFetch = window.fetch.bind(window);
          window.fetch = (input, init = {}) => {
            const url = typeof input === "string" ? input : input.url;
            if (!url.includes("/api/v1/audio/")) return nativeFetch(input, init);
            return new Promise((resolve, reject) => {
              init.signal?.addEventListener("abort", () => {
                window.recordPreloadAbort();
                reject(new DOMException("Aborted", "AbortError"));
              }, {once: true});
            });
          };
        })()"""
    )
    page.goto(base_url)
    page.locator("#password").fill("browser-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(f"{base_url}/")
    page.wait_for_function(
        """() => window.__pwa.getAudioCacheState().expected > 0 &&
          !window.__pwa.getAudioCacheState().ready"""
    )
    assert page.evaluate("fetch('/api/v1/sets').then((response) => response.status)") == 200

    pending_id = "00000000-0000-0000-0000-000000000202"
    page.context.set_offline(True)
    put_outbox_events(
        page,
        [
            {
                "id": pending_id,
                "word_id": "protected-0",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page.locator("#notice")).to_have_text(
        "You are offline. Sign-out was not sent and local study data was kept."
    )
    wait_for_store_ids(page, "events", [pending_id])

    # Keep an unsynced event deterministic while exercising the confirmation.
    page.route("**/api/v1/review-events/batch", lambda route: route.abort())
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    put_outbox_events(
        page,
        [
            {
                "id": pending_id,
                "word_id": "protected-0",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page.locator("#notice")).to_contain_text("Sign-out was cancelled")
    wait_for_store_ids(page, "events", [pending_id])

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page).to_have_url(f"{base_url}/login")
    assert aborts
    cookie_names = [cookie["name"] for cookie in page.context.cookies()]
    assert "vocab-session" not in cookie_names and "vocab-csrf" not in cookie_names
    private_keys, db_values = private_storage_state(page)
    assert private_keys == []
    assert db_values == [[], [], []]
    assert page.evaluate("fetch('/api/v1/sets').then((response) => response.status)") == 401
