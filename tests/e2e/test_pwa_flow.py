from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.web.app import create_app
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


@pytest.mark.e2e
def test_mobile_pwa_known_unknown_reload_and_undo(page: Page, pwa_server: tuple[str, Path]) -> None:
    base_url, database = pwa_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page.locator("#term")).to_be_hidden()
    expect(page.locator("#transcript")).to_be_hidden()

    page.get_by_role("button", name="Known", exact=True).click()
    expect(page.locator("#progress")).to_have_text("2 of 3")
    assert wait_for_rows(database, 1)[0]["rating"] == "easy"

    page.get_by_role("button", name="Unknown", exact=True).click()
    expect(page.locator("#term")).to_be_visible()
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
    expect(page.locator("#card")).to_have_text("Daily study complete.")
    page.get_by_role("button", name="Undo").click()
    expect(page.locator("#progress")).to_have_text("3 of 3")
    assert len(event_rows(database)) == 3
    page.context.set_offline(False)
