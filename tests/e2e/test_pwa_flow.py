from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from json import dumps
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
import uvicorn
from argon2 import PasswordHasher
from fastapi import FastAPI
from playwright.sync_api import Page, Route, expect

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Rating, ReviewEvent, Tier, Word
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
def large_pwa_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    database = tmp_path / "large.db"
    audio_root = tmp_path / "large-audio"
    _seed(database, audio_root, "large", 31)
    app = create_app(AppContainer(database, FilesystemAudioStore(audio_root), "test"))
    with _serve(app) as base_url:
        yield base_url, database


@pytest.fixture
def production_auth_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
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
                "SELECT id,word_id,rating,reviewed_at,voided_at "
                "FROM review_events ORDER BY reviewed_at,id"
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


def wait_for_store_ids(page: Page, store: str, expected: list[str]) -> None:
    matched = page.evaluate(
        """async ({store, expected}) => {
          const wanted = [...expected].sort();
          const deadline = Date.now() + 10_000;
          while (Date.now() < deadline) {
            const ids = await new Promise((resolve, reject) => {
              const request = indexedDB.open("english-vocab-trainer", 5);
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const db = request.result;
                const keys = db.transaction(store).objectStore(store).getAllKeys();
                keys.onerror = () => { db.close(); reject(keys.error); };
                keys.onsuccess = () => { db.close(); resolve(keys.result.sort()); };
              };
            });
            if (JSON.stringify(ids) === JSON.stringify(wanted)) return true;
            await new Promise((resolve) => setTimeout(resolve, 50));
          }
          return false;
        }""",
        {"store": store, "expected": expected},
    )
    assert matched is True, f"{store} IDs did not become {expected!r}"


def put_outbox_events(page: Page, events: list[dict[str, str]], *, store: str = "events") -> None:
    page.evaluate(
        """async ({events, store}) => {
          const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open("english-vocab-trainer", 5);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          try {
            await new Promise((resolve, reject) => {
              const tx = db.transaction(store, "readwrite");
              events.forEach((event) => tx.objectStore(store).put(event));
              tx.oncomplete = resolve;
              tx.onerror = () => reject(tx.error);
              tx.onabort = () => reject(tx.error);
            });
          } finally { db.close(); }
        }""",
        {"events": events, "store": store},
    )


def stored_state(page: Page) -> dict[str, object]:
    return cast(
        dict[str, object],
        page.evaluate(
            """async () => {
              const db = await new Promise((resolve, reject) => {
                const request = indexedDB.open("english-vocab-trainer", 5);
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
              });
              try {
                return await new Promise((resolve, reject) => {
                  const request = db.transaction("state").objectStore("state").get("active-set");
                  request.onsuccess = () => resolve(request.result.value);
                  request.onerror = () => reject(request.error);
                });
              } finally { db.close(); }
            }"""
        ),
    )


def private_storage_state(page: Page) -> list[list[object]]:
    return cast(
        list[list[object]],
        page.evaluate(
            """async () => {
              const db = await new Promise((resolve, reject) => {
                const request = indexedDB.open("english-vocab-trainer", 5);
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
              });
              try {
                return await Promise.all(["events", "failedEvents", "state"].map(
                  (name) => new Promise((resolve, reject) => {
                    const request = db.transaction(name).objectStore(name).getAll();
                    request.onsuccess = () => resolve(request.result);
                    request.onerror = () => reject(request.error);
                  })
                ));
              } finally { db.close(); }
            }"""
        ),
    )


def wait_for_shell(page: Page) -> list[str]:
    page.wait_for_function(
        """async () => {
          await navigator.serviceWorker.ready;
          if (!navigator.serviceWorker.controller) return false;
          return (await caches.keys()).includes("pwa-shell-v8");
        }""",
        timeout=10_000,
    )
    return cast(list[str], page.evaluate("async () => caches.keys()"))


def answer(page: Page, value: str) -> None:
    before = page.locator("#term").text_content()
    page.get_by_role("button", name=value, exact=True).click()
    page.wait_for_function(
        """(term) => document.querySelector('#term').hidden ||
          document.querySelector('#term').textContent !== term""",
        arg=before,
    )


def install_batch_response_loss(page: Page, *, repeat: bool = False) -> None:
    page.add_init_script(
        """(() => {
          const nativeFetch = window.fetch.bind(window);
          let lost = false;
          window.__loseBatchResponses = true;
          window.fetch = async (input, init = {}) => {
            const url = typeof input === "string" ? input : input.url;
            if (!url.includes("/api/v1/review-events/batch") ||
                !window.__loseBatchResponses || (!__REPEAT__ && lost)) {
              return nativeFetch(input, init);
            }
            lost = true;
            await nativeFetch(input, init);
            throw new TypeError("simulated response loss");
          };
        })()""".replace("__REPEAT__", str(repeat).lower())
    )


@pytest.mark.e2e
def test_single_card_history_navigation_and_controls(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page.locator("#card")).to_have_text("Choose Known or Unknown.")
    expect(page.locator("#term")).to_be_visible()
    expect(page.locator("#transcript")).to_contain_text("English definition")
    expect(page.locator("#status")).to_be_hidden()
    assert page.locator("#audio").get_attribute("controls") == ""
    assert page.locator("#audio").get_attribute("preload") == "metadata"
    assert (
        page.get_by_role("button", name="Known", exact=True).get_attribute("aria-pressed") is None
    )
    assert page.locator("#unknown").evaluate("element => element.parentElement.id") == (
        "rating-actions"
    )
    assert page.locator("#previous").evaluate("element => element.parentElement.id") == (
        "navigation-actions"
    )
    assert page.evaluate("document.activeElement === document.body") is True
    first_word = page.locator("#term").text_content()

    answer(page, "Known")
    answer(page, "Unknown")
    wait_for_rows(database, 2)
    wait_for_store_ids(page, "events", [])
    current_word = page.locator("#term").text_content()

    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#progress")).to_contain_text("Reviewing answer 2 of 2")
    expect(page.get_by_role("button", name="Unknown — saved", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Change to Known", exact=True)).to_be_enabled()
    second_word = page.locator("#term").text_content()
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#term")).to_have_text(first_word or "")
    expect(page.get_by_role("button", name="Known — saved", exact=True)).to_be_disabled()
    page.reload()
    expect(page.locator("#term")).to_have_text(first_word or "")

    rows_before = [tuple(row) for row in event_rows(database)]
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#term")).to_have_text(second_word or "")
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#term")).to_have_text(current_word or "")
    wait_for_store_ids(page, "events", [])
    assert [tuple(row) for row in event_rows(database)] == rows_before


@pytest.mark.e2e
def test_second_tab_is_read_only_for_every_local_mutation(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    answer(page, "Known")
    wait_for_rows(database, 1)
    page.get_by_role("button", name="Previous", exact=True).click()
    failed = {
        "id": "00000000-0000-0000-0000-000000000401",
        "word_id": "word-0",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "intent": "review",
        "terminalStatus": "conflict",
    }
    put_outbox_events(page, [failed], store="failedEvents")

    follower = page.context.new_page()
    try:
        follower.goto(base_url)
        expect(follower.locator("#status")).to_contain_text("already open in another tab")
        expect(follower.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
        expect(follower.get_by_role("button", name="Next", exact=True)).to_be_disabled()
        expect(follower.get_by_role("button", name="Known — saved", exact=True)).to_be_disabled()
        expect(
            follower.get_by_role("button", name="Change to Unknown", exact=True)
        ).to_be_disabled()
        expect(follower.get_by_role("button", name="Retry answer", exact=True)).to_be_disabled()
        expect(
            follower.get_by_role("button", name="Discard local change", exact=True)
        ).to_be_disabled()
        expect(follower.get_by_role("button", name="Logout", exact=True)).to_be_disabled()
    finally:
        follower.close()


@pytest.mark.e2e
def test_page_restored_from_cache_rechecks_the_writer_lock(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    page.goto(base_url)
    expect(page.get_by_role("button", name="Known", exact=True)).to_be_enabled()
    page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")

    new_writer = page.context.new_page()
    try:
        new_writer.goto(base_url)
        expect(new_writer.get_by_role("button", name="Known", exact=True)).to_be_enabled()
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator("#status")).to_contain_text("already open in another tab")
        expect(page.get_by_role("button", name="Known", exact=True)).to_be_disabled()
    finally:
        new_writer.close()


@pytest.mark.e2e
def test_historical_correction_converges_offline_after_response_loss(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    install_batch_response_loss(page, repeat=True)
    page.goto(base_url)
    expect(page.locator("#card")).to_have_text("Choose Known or Unknown.")
    wait_for_shell(page)
    original_word = page.locator("#term").text_content()
    answer(page, "Known")
    old = wait_for_rows(database, 1)[0]
    expect(page.locator("#status")).to_contain_text("network error")

    page.context.set_offline(True)
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#term")).to_have_text(original_word or "")
    original_entry = cast(list[dict[str, object]], stored_state(page)["history"])[0]
    expect(page.get_by_role("button", name="Known — saved", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Change to Unknown", exact=True).click()
    corrected = cast(list[dict[str, object]], stored_state(page)["history"])[0]
    assert corrected["eventId"] != original_entry["eventId"]
    assert corrected["reviewed_at"] == original_entry["reviewed_at"]
    wait_for_store_ids(page, "events", [old["id"], cast(str, corrected["eventId"])])

    page.reload(wait_until="domcontentloaded")
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.get_by_role("button", name="Unknown — saved", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Next", exact=True).click()
    page.evaluate("window.__loseBatchResponses = false")
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_voided_event(database, old["id"])
    rows = wait_for_rows(database, 2)
    replacement = next(row for row in rows if row["id"] == corrected["eventId"])
    assert replacement["rating"] == "again"
    assert replacement["reviewed_at"] == old["reviewed_at"]
    wait_for_store_ids(page, "events", [])


@pytest.mark.e2e
def test_answer_state_and_outbox_commit_atomically(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    expect(page.locator("#card")).to_have_text("Choose Known or Unknown.")
    before = stored_state(page)
    page.evaluate(
        """() => {
          const nativePut = IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put = function(value) {
            if (this.name === "state") throw new DOMException("simulated quota failure");
            return nativePut.call(this, value);
          };
        }"""
    )
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#status")).to_contain_text("could not be saved locally")
    assert stored_state(page) == before
    wait_for_store_ids(page, "events", [])
    assert event_rows(database) == []


@pytest.mark.e2e
def test_slow_successful_sync_does_not_show_normal_pending_noise(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.add_init_script(
        """(() => {
          const nativeFetch = window.fetch.bind(window);
          window.fetch = async (input, init = {}) => {
            const response = await nativeFetch(input, init);
            const url = typeof input === "string" ? input : input.url;
            if (url.includes("/api/v1/review-events/batch")) {
              await new Promise((resolve) => setTimeout(resolve, 600));
            }
            return response;
          };
        })()"""
    )
    page.goto(base_url)
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#progress")).to_have_text("Ready to answer")
    expect(page.locator("#status")).to_be_hidden()
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_hidden()
    wait_for_rows(database, 1)
    wait_for_store_ids(page, "events", [])
    expect(page.locator("#status")).to_be_hidden()


@pytest.mark.e2e
def test_sync_drains_event_added_during_active_flight(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    injected = "00000000-0000-0000-0000-000000000301"
    page.add_init_script(
        """(() => {
          const nativeFetch = window.fetch.bind(window);
          let inserted = false;
          window.fetch = async (input, init = {}) => {
            const url = typeof input === "string" ? input : input.url;
            const response = await nativeFetch(input, init);
            if (url.includes("/api/v1/review-events/batch") && !inserted) {
              inserted = true;
              const db = await new Promise((resolve, reject) => {
                const request = indexedDB.open("english-vocab-trainer", 5);
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
              });
              await new Promise((resolve, reject) => {
                const tx = db.transaction("events", "readwrite");
                tx.objectStore("events").put({
                  id: "__INJECTED__",
                  word_id: "word-1",
                  action: "known",
                  reviewed_at: new Date(Date.now() + 1).toISOString(),
                  intent: "review",
                });
                tx.oncomplete = resolve;
                tx.onerror = () => reject(tx.error);
              });
              db.close();
            }
            return response;
          };
        })()""".replace("__INJECTED__", injected)
    )
    page.goto(base_url)
    answer(page, "Known")
    assert len(wait_for_rows(database, 2)) == 2
    wait_for_store_ids(page, "events", [])


@pytest.mark.e2e
def test_next_batch_is_staged_automatically_and_keeps_history_cursor(
    page: Page, large_pwa_server: tuple[str, Path]
) -> None:
    base_url, database = large_pwa_server
    page.goto(base_url)
    expect(page.locator("#progress")).to_have_text("Ready to answer")
    page.context.set_offline(True)
    for _ in range(30):
        answer(page, "Known")
    expect(page.locator("#progress")).to_have_text("Waiting to reconnect")
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#progress")).to_contain_text("Reviewing answer 30 of 30")

    page.evaluate(
        """() => {
          window.__nativeIdbPut = IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put = function(value) {
            if (this.name === "state" && value?.value?.cards?.length === 1) {
              throw new DOMException("simulated quota failure");
            }
            return window.__nativeIdbPut.call(this, value);
          };
        }"""
    )
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_rows(database, 30)
    wait_for_store_ids(page, "events", [])
    expect(page.locator("#status")).to_contain_text("study history was kept")
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    failed_stage = stored_state(page)
    assert len(cast(list[object], failed_stage["cards"])) == 30
    assert failed_stage["current"] == 30 and failed_stage["cursor"] == 29

    page.get_by_role("button", name="Next", exact=True).click()
    page.context.set_offline(True)
    expect(page.locator("#card")).to_contain_text("Reconnect to try again")
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_hidden()
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    page.get_by_role("button", name="Previous", exact=True).click()
    page.evaluate("() => { IDBObjectStore.prototype.put = window.__nativeIdbPut; }")
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.locator("#progress")).to_have_text("Reviewing answer 30 of 30")
    expect(page.locator("#status")).to_be_hidden()
    page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#progress")).to_have_text("Ready to answer")
    assert page.get_by_role("button", name="Next set", exact=True).count() == 0


@pytest.mark.e2e
def test_catalog_empty_does_not_poll_or_offer_a_manual_next_batch(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    with SQLiteVocabularyRepository(database) as repository:
        repository.db.execute("DELETE FROM words")
    page.goto(base_url)
    expect(page.locator("#card")).to_contain_text("The catalog is empty.")
    assert page.get_by_role("button", name="Next set", exact=True).count() == 0


@pytest.mark.e2e
def test_next_due_rechecks_after_timer_fires_offline(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    with SQLiteVocabularyRepository(database) as repository:
        for index in range(3):
            repository.append_event(
                ReviewEvent(UUID(int=index + 1), f"word-{index}", Rating.GOOD, datetime.now(UTC))
            )
        due = datetime.now(UTC) + timedelta(seconds=1)
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id='word-0'", (due.isoformat(),)
        )
    page.goto(base_url)
    expect(page.locator("#card")).to_contain_text("The next review is due")
    page.context.set_offline(True)
    time.sleep(1.2)
    expect(page.locator("#term")).to_be_hidden()
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.locator("#term")).to_have_text("word word 0")
    expect(page.locator("#progress")).to_have_text("Ready to answer")


@pytest.mark.e2e
def test_next_due_loads_automatically_when_its_timer_fires_online(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    with SQLiteVocabularyRepository(database) as repository:
        for index in range(3):
            repository.append_event(
                ReviewEvent(UUID(int=index + 1), f"word-{index}", Rating.GOOD, datetime.now(UTC))
            )
        due = datetime.now(UTC) + timedelta(seconds=1)
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id='word-0'", (due.isoformat(),)
        )

    page.goto(base_url)

    expect(page.locator("#card")).to_contain_text("The next review is due")
    expect(page.locator("#term")).to_have_text("word word 0", timeout=5_000)
    expect(page.locator("#progress")).to_have_text("Ready to answer")


@pytest.mark.e2e
def test_due_timer_does_not_load_cards_before_a_pending_answer_syncs(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    with SQLiteVocabularyRepository(database) as repository:
        for index in range(3):
            repository.append_event(
                ReviewEvent(UUID(int=index + 1), f"word-{index}", Rating.GOOD, datetime.now(UTC))
            )
        due = datetime.now(UTC) + timedelta(seconds=1)
        repository.db.execute("UPDATE word_state SET due_at=?", (due.isoformat(),))
    set_requests = 0

    def count_set_request(route: Route) -> None:
        nonlocal set_requests
        set_requests += 1
        route.continue_()

    page.route("**/api/v1/sets", count_set_request)
    page.goto(base_url)
    expect(page.locator("#card")).to_contain_text("The next review is due")
    assert set_requests == 1
    pending_id = "00000000-0000-0000-0000-000000000501"
    put_outbox_events(
        page,
        [
            {
                "id": pending_id,
                "word_id": "word-0",
                "action": "known",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "intent": "review",
            }
        ],
    )

    page.wait_for_timeout(1_200)

    assert set_requests == 1
    wait_for_store_ids(page, "events", [pending_id])
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_store_ids(page, "events", [])
    deadline = time.monotonic() + 5
    while set_requests < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(30)
    assert set_requests == 2


@pytest.mark.e2e
def test_next_due_beyond_the_browser_timer_limit_is_rescheduled(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    with SQLiteVocabularyRepository(database) as repository:
        for index in range(3):
            repository.append_event(
                ReviewEvent(UUID(int=index + 1), f"word-{index}", Rating.GOOD, datetime.now(UTC))
            )
        due = datetime.now(UTC) + timedelta(days=30)
        repository.db.execute("UPDATE word_state SET due_at=?", (due.isoformat(),))
    page.add_init_script(
        """(() => {
          const nativeSetTimeout = window.setTimeout.bind(window);
          window.__longDueSchedules = 0;
          window.setTimeout = (callback, delay, ...args) => {
            if (delay === 2_147_483_647) {
              window.__longDueSchedules += 1;
              if (window.__longDueSchedules === 1) {
                return nativeSetTimeout(callback, 0, ...args);
              }
            }
            return nativeSetTimeout(callback, delay, ...args);
          };
        })()"""
    )

    page.goto(base_url)

    expect(page.locator("#card")).to_contain_text("The next review is due")
    page.wait_for_function("window.__longDueSchedules >= 2")


@pytest.mark.e2e
def test_terminal_sync_result_is_explained_per_answer_and_blocks_auto_load(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    acknowledged = "00000000-0000-0000-0000-000000000101"
    conflicted = "00000000-0000-0000-0000-000000000102"
    page.goto(base_url)
    for count in range(1, 4):
        answer(page, "Known")
        wait_for_rows(database, count)

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
    put_outbox_events(
        page,
        [
            {
                "id": acknowledged,
                "word_id": "word-0",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "intent": "review",
            },
            {
                "id": conflicted,
                "word_id": "word-1",
                "action": "unknown",
                "reviewed_at": "2026-01-01T00:00:01Z",
                "intent": "review",
            },
        ],
    )
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_store_ids(page, "events", [])
    wait_for_store_ids(page, "failedEvents", [conflicted])
    expect(page.locator("#status")).to_have_text("1 answer needs your attention.")
    expect(page.locator("#sync-issues-list")).to_contain_text(
        "word word 1: the Unknown answer was not saved on the server."
    )
    expect(page.locator("#sync-issues-list")).not_to_contain_text(conflicted)
    expect(page.get_by_role("button", name="Retry answer", exact=True)).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Discard local change", exact=True).click()
    wait_for_store_ids(page, "failedEvents", [])


@pytest.mark.e2e
def test_terminal_issue_has_priority_and_distinct_retry_actions(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    failed_id = "00000000-0000-0000-0000-000000000601"
    pending_id = "00000000-0000-0000-0000-000000000602"
    page.goto(base_url)
    put_outbox_events(
        page,
        [
            {
                "id": failed_id,
                "word_id": "word-0",
                "action": "unknown",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "intent": "review",
                "terminalStatus": "conflict",
            }
        ],
        store="failedEvents",
    )
    put_outbox_events(
        page,
        [
            {
                "id": pending_id,
                "word_id": "word-1",
                "action": "known",
                "reviewed_at": "2026-01-01T00:00:01Z",
                "intent": "review",
            }
        ],
    )
    page.route("**/api/v1/review-events/batch", lambda route: route.abort())

    page.evaluate("window.dispatchEvent(new Event('online'))")

    expect(page.locator("#status")).to_have_text("1 answer needs your attention.")
    expect(page.get_by_role("button", name="Retry answer", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()


@pytest.mark.e2e
def test_discarding_an_unsaved_answer_removes_it_from_history(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server

    def reject_answer(route: Route) -> None:
        events = route.request.post_data_json
        assert isinstance(events, list)
        event = events[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=dumps({"results": [{"id": event["id"], "status": "conflict", "due_at": None}]}),
        )

    page.route("**/api/v1/review-events/batch", reject_answer)
    page.goto(base_url)
    answer(page, "Known")
    history = cast(list[dict[str, object]], stored_state(page)["history"])
    failed_id = cast(str, history[0]["eventId"])
    wait_for_store_ids(page, "failedEvents", [failed_id])

    page.once("dialog", lambda dialog: dialog.accept())
    page.evaluate(
        """() => {
          const answer = document.querySelector("#known");
          document.querySelector("#sync-issues-list button:last-child").click();
          answer.click();
        }"""
    )

    wait_for_store_ids(page, "failedEvents", [])
    assert stored_state(page)["history"] == []
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_hidden()
    assert event_rows(database) == []


@pytest.mark.e2e
def test_retry_blocks_discard_until_the_answer_is_requeued(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server

    def reject_answer(route: Route) -> None:
        events = route.request.post_data_json
        assert isinstance(events, list)
        event = events[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=dumps({"results": [{"id": event["id"], "status": "conflict", "due_at": None}]}),
        )

    page.route("**/api/v1/review-events/batch", reject_answer)
    page.goto(base_url)
    answer(page, "Known")
    history = cast(list[dict[str, object]], stored_state(page)["history"])
    failed_id = cast(str, history[0]["eventId"])
    wait_for_store_ids(page, "failedEvents", [failed_id])
    page.unroute("**/api/v1/review-events/batch", reject_answer)
    page.route("**/api/v1/review-events/batch", lambda route: route.abort())
    page.once("dialog", lambda dialog: dialog.accept())

    page.evaluate(
        """() => {
          const retry = document.querySelector("#sync-issues-list button:first-of-type");
          const discard = document.querySelector("#sync-issues-list button:last-child");
          retry.click();
          discard.click();
        }"""
    )

    wait_for_store_ids(page, "failedEvents", [])
    wait_for_store_ids(page, "events", [failed_id])
    assert len(cast(list[object], stored_state(page)["history"])) == 1


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
                "intent": "review",
            }
            for index in range(1, 206)
        ],
    )
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_for_store_ids(page, "events", [])
    assert batches == [100, 100, 5]


@pytest.mark.e2e
def test_shell_and_study_state_work_offline_but_audio_is_online_only(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    cache_names = wait_for_shell(page)
    assert cache_names == ["pwa-shell-v8"]
    shell_paths = page.evaluate(
        """async () => caches.open("pwa-shell-v8").then((cache) => cache.keys()).then(
          (keys) => keys.map((key) => new URL(key.url).pathname)
        )"""
    )
    assert all(not path.startswith("/api/") for path in shell_paths)

    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    page.evaluate(
        """() => {
          window.__onlineForTest = false;
          Object.defineProperty(navigator, "onLine", {
            configurable: true,
            get: () => window.__onlineForTest,
          });
          window.dispatchEvent(new Event("offline"));
        }"""
    )
    expect(page.locator("#term")).to_be_visible()
    expect(page.locator("#status")).to_contain_text("Offline")
    expect(page.locator("#audio")).to_be_hidden()
    assert page.locator("#audio").get_attribute("src") is None

    answer(page, "Unknown")
    answer(page, "Known")
    assert event_rows(database) == []
    expect(page.locator("#status")).to_contain_text("Offline · 2 answers waiting")
    page.context.set_offline(False)
    page.evaluate("window.__onlineForTest = true")
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.locator("#audio")).to_be_visible()
    assert page.locator("#audio").get_attribute("src") is not None
    assert [row["rating"] for row in wait_for_rows(database, 2)] == ["again", "good"]
    wait_for_store_ids(page, "events", [])
    expect(page.locator("#status")).to_be_hidden()


@pytest.mark.e2e
def test_production_logout_preserves_unsynced_data_until_confirmed(
    page: Page, production_auth_server: tuple[str, Path]
) -> None:
    base_url, _ = production_auth_server
    page.goto(base_url)
    page.locator("#password").fill("browser-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(f"{base_url}/")
    expect(page.locator("#card")).to_have_text("Choose Known or Unknown.")

    pending_id = "00000000-0000-0000-0000-000000000202"
    event = {
        "id": pending_id,
        "word_id": "protected-0",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "intent": "review",
    }
    page.context.set_offline(True)
    put_outbox_events(page, [event])
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page.locator("#status")).to_have_text(
        "You are offline. Sign-out was not sent and local study data was kept."
    )
    wait_for_store_ids(page, "events", [pending_id])

    page.route("**/api/v1/review-events/batch", lambda route: route.abort())
    page.context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    put_outbox_events(page, [event])
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page.locator("#status")).to_contain_text("Sign-out was cancelled")
    wait_for_store_ids(page, "events", [pending_id])

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page).to_have_url(f"{base_url}/login")
    cookie_names = [cookie["name"] for cookie in page.context.cookies()]
    assert "vocab-session" not in cookie_names and "vocab-csrf" not in cookie_names
    assert private_storage_state(page) == [[], [], []]
    assert page.evaluate("fetch('/api/v1/sets').then((response) => response.status)") == 401
