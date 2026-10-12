from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from argon2 import PasswordHasher
from fastapi import FastAPI
from playwright.sync_api import Page, Route, expect

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Review, ReviewAction, Word
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


def _seed(database: Path, audio_root: Path, prefix: str, count: int) -> list[Word]:
    audio_root.mkdir()
    words = [
        Word(
            f"{prefix}-{index}",
            f"{prefix} word {index}",
            index + 1,
            f"An English definition for {prefix} word {index}.",
            f"{prefix}-{index}.mp3",
        )
        for index in range(count)
    ]
    for word in words:
        (audio_root / word.audio_key).write_bytes(b"ID3\x04\x00\x00audio")
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(words)
    return words


@pytest.fixture
def pwa_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    database = tmp_path / "vocab.db"
    audio_root = tmp_path / "audio"
    _seed(database, audio_root, "word", 4)
    app = create_app(AppContainer(database, FilesystemAudioStore(audio_root), "test"))
    with _serve(app) as base_url:
        yield base_url, database


@pytest.fixture
def paginated_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    database = tmp_path / "paginated.db"
    audio_root = tmp_path / "paginated-audio"
    words = _seed(database, audio_root, "history", 52)
    reviewed_at = datetime.now(UTC)
    with SQLiteVocabularyRepository(database) as repository:
        for index, word in enumerate(words[:51], start=1):
            repository.save_review(
                Review(UUID(int=index), word.id, ReviewAction.KNOWN, reviewed_at)
            )
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


def review_rows(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                "SELECT id,word_id,action,reviewed_at FROM reviews ORDER BY reviewed_at,id"
            )
        )
    finally:
        connection.close()


def wait_for_rows(database: Path, count: int) -> list[sqlite3.Row]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = review_rows(database)
        if len(rows) == count:
            return rows
        time.sleep(0.03)
    raise AssertionError(f"expected {count} reviews, got {review_rows(database)}")


def answer(page: Page, action: str) -> None:
    expect(page.locator("#term")).to_be_visible()
    expect(page.get_by_role("button", name=action, exact=True)).to_be_enabled()
    previous_term = page.locator("#term").text_content()
    page.get_by_role("button", name=action, exact=True).click()
    page.wait_for_function(
        """(term) => !document.querySelector('#term').hidden
          && document.querySelector('#term').textContent !== term""",
        arg=previous_term,
    )


@pytest.mark.e2e
def test_initial_card_and_known_unknown_advance(page: Page, pwa_server: tuple[str, Path]) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page.locator("#term")).to_have_text("word word 0")
    expect(page.locator("#definition")).to_contain_text("English definition")
    assert page.locator("#audio").get_attribute("controls") == ""
    assert page.locator("#audio").get_attribute("preload") == "metadata"
    assert page.locator("#audio").get_attribute("src") == "/api/v1/audio/word-0"
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Unknown", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Known", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Logout", exact=True)).to_have_count(0)
    assert page.locator("text=Ready to answer").count() == 0
    assert page.locator("text=Choose Known or Unknown.").count() == 0

    answer(page, "Known")
    expect(page.locator("#term")).to_have_text("word word 1")
    answer(page, "Unknown")
    expect(page.locator("#term")).to_have_text("word word 2")
    rows = wait_for_rows(database, 2)
    assert [row["action"] for row in rows] == ["known", "unknown"]
    with SQLiteVocabularyRepository(database) as repository:
        due = repository.state("word-1").due_at
    reviewed = datetime.fromisoformat(rows[1]["reviewed_at"])
    assert reviewed + timedelta(minutes=9, seconds=59) <= due


@pytest.mark.e2e
def test_history_navigation_is_unlimited_and_does_not_write(
    page: Page, paginated_server: tuple[str, Path]
) -> None:
    base_url, database = paginated_server
    page.goto(base_url)
    expect(page.locator("#term")).to_have_text("history word 51")
    before = [tuple(row) for row in review_rows(database)]
    for _ in range(51):
        page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#term")).to_have_text("history word 0")
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    for _ in range(51):
        page.get_by_role("button", name="Next", exact=True).click()
    expect(page.locator("#term")).to_have_text("history word 51")
    expect(page.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    assert [tuple(row) for row in review_rows(database)] == before


@pytest.mark.e2e
def test_historical_same_answer_moves_forward_and_opposite_updates_review(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    page.goto(base_url)
    answer(page, "Known")
    current = page.locator("#term").text_content()
    original = wait_for_rows(database, 1)[0]

    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#saved-answer")).to_have_text("Saved answer: Known")
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#term")).to_have_text(current or "")
    assert [tuple(row) for row in review_rows(database)] == [tuple(original)]

    page.get_by_role("button", name="Previous", exact=True).click()
    page.get_by_role("button", name="Unknown", exact=True).click()
    expect(page.locator("#term")).to_have_text(current or "")
    corrected = wait_for_rows(database, 1)[0]
    assert corrected["id"] == original["id"]
    assert corrected["reviewed_at"] == original["reviewed_at"]
    assert corrected["action"] == "unknown"
    assert page.locator("#audio").get_attribute("src") == "/api/v1/audio/word-1"
    page.reload()
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#saved-answer")).to_have_text("Saved answer: Unknown")


@pytest.mark.e2e
def test_correcting_current_due_word_refreshes_server_selection(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    reviewed_at = datetime.now(UTC) - timedelta(minutes=20)
    with SQLiteVocabularyRepository(database) as repository:
        repository.save_review(Review(UUID(int=91), "word-0", ReviewAction.UNKNOWN, reviewed_at))

    page.goto(base_url)
    expect(page.locator("#term")).to_have_text("word word 0")
    page.get_by_role("button", name="Previous", exact=True).click()
    expect(page.locator("#saved-answer")).to_have_text("Saved answer: Unknown")
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#term")).to_have_text("word word 1")

    rows = wait_for_rows(database, 1)
    assert rows[0]["id"] == str(UUID(int=91))
    assert rows[0]["action"] == "known"
    assert page.locator("#audio").get_attribute("src") == "/api/v1/audio/word-1"


@pytest.mark.e2e
def test_response_loss_retains_card_and_retries_same_uuid(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    lost: list[str] = []

    def lose_response(route: Route) -> None:
        body = route.request.post_data_json
        assert isinstance(body, dict)
        lost.append(str(body["id"]))
        route.fetch()
        route.abort()

    page.route("**/api/v1/reviews", lose_response, times=1)
    page.goto(base_url)
    expect(page.locator("#term")).to_have_text("word word 0")
    initial = page.locator("#term").text_content()
    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#term")).to_have_text(initial or "")
    expect(page.get_by_role("button", name="Retry answer", exact=True)).to_be_visible()
    for action in ("Previous", "Next", "Unknown", "Known"):
        expect(page.get_by_role("button", name=action, exact=True)).to_be_disabled()
    rows = wait_for_rows(database, 1)
    assert rows[0]["id"] == lost[0]

    page.get_by_role("button", name="Retry answer", exact=True).click()
    expect(page.locator("#term")).not_to_have_text(initial or "")
    expect(page.get_by_role("button", name="Previous", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Unknown", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Known", exact=True)).to_be_enabled()
    rows = wait_for_rows(database, 1)
    assert rows[0]["id"] == lost[0]


@pytest.mark.e2e
def test_due_card_failure_waits_for_explicit_retry(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, database = pwa_server
    now = datetime.now(UTC)
    with SQLiteVocabularyRepository(database) as repository:
        for index in range(4):
            repository.save_review(
                Review(UUID(int=index + 1), f"word-{index}", ReviewAction.KNOWN, now)
            )
        repository.db.execute(
            "UPDATE word_state SET due_at=?",
            ((now + timedelta(days=1)).isoformat(),),
        )
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id='word-0'",
            ((datetime.now(UTC) + timedelta(seconds=1)).isoformat(),),
        )

    requests = 0

    def fail_automatic_retry(route: Route) -> None:
        nonlocal requests
        requests += 1
        if requests == 2:
            route.abort()
        else:
            route.continue_()

    page.route("**/api/v1/card", fail_automatic_retry)
    page.goto(base_url)
    expect(page.locator("#card")).to_contain_text("next review is due")
    expect(page.get_by_role("button", name="Retry next card", exact=True)).to_be_visible()
    page.wait_for_timeout(500)
    assert requests == 2
    expect(page.locator("#term")).to_be_hidden()

    page.get_by_role("button", name="Retry next card", exact=True).click()
    expect(page.locator("#term")).to_have_text("word word 0")
    assert requests == 3


@pytest.mark.e2e
def test_initial_card_failure_is_not_reported_as_an_empty_catalog(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    page.route("**/api/v1/card", lambda route: route.abort(), times=1)
    page.goto(base_url)
    expect(page.get_by_role("button", name="Retry next card", exact=True)).to_be_visible()
    expect(page.locator("#card")).to_have_text("Loading a card…")
    expect(page.locator("#card")).not_to_contain_text("catalog is empty")

    page.get_by_role("button", name="Retry next card", exact=True).click()
    expect(page.locator("#term")).to_have_text("word word 0")
    assert page.locator("#audio").get_attribute("src") == "/api/v1/audio/word-0"


@pytest.mark.e2e
def test_second_tab_is_read_only_until_writer_closes(
    page: Page, pwa_server: tuple[str, Path]
) -> None:
    base_url, _ = pwa_server
    page.goto(base_url)
    expect(page.get_by_role("button", name="Known", exact=True)).to_be_enabled()
    follower = page.context.new_page()
    follower.goto(base_url)
    expect(follower.locator("#status")).to_contain_text("another tab")
    expect(follower.get_by_role("button", name="Known", exact=True)).to_be_disabled()
    expect(follower.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    page.close()
    follower.reload()
    expect(follower.get_by_role("button", name="Known", exact=True)).to_be_enabled()
    expect(follower.locator("#status")).to_be_hidden()


@pytest.mark.e2e
def test_production_logout_ends_session_only(
    page: Page, production_auth_server: tuple[str, Path]
) -> None:
    base_url, database = production_auth_server
    page.goto(f"{base_url}/login")
    page.get_by_label("Password").fill("browser-password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("button", name="Logout", exact=True)).to_be_visible()
    answer(page, "Known")
    wait_for_rows(database, 1)
    page.get_by_role("button", name="Logout", exact=True).click()
    page.wait_for_url(f"{base_url}/login")
    assert len(review_rows(database)) == 1
    assert page.evaluate("fetch('/api/v1/card').then((response) => response.status)") == 401
