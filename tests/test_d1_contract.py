from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository
from english_vocab_trainer.adapters.local.sqlite import SCHEMA
from english_vocab_trainer.ports.errors import MissingError
from tests.fakes.d1 import FakeD1


@pytest.mark.asyncio
async def test_d1_reads_new_and_due_words() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO words VALUES(?,?,?,?,?)",
        [("nine", "nine", 9, None, "nine.mp3"), ("none", "none", None, None, "none.mp3")],
    )
    connection.execute(
        "INSERT INTO user_word_state VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("alice", "nine", "2000-01-01T00:00:00+00:00", 1, 5, None, None, None, None, 1),
    )
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    assert [word.id for word in await repo.list_words()] == ["nine", "none"]
    assert [word.id for word in await repo.list_words(levels=[9])] == ["nine"]
    assert [word.id for word in await repo.due_words(datetime.now(UTC), 10)] == ["nine"]
    connection.close()


@pytest.mark.asyncio
async def test_d1_settings_default_update_and_validation() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    assert (await repo.get_settings()).daily_target == 30
    assert (await repo.update_settings(42)).daily_target == 42
    assert (await repo.get_settings()).daily_target == 42
    with pytest.raises(ValueError):
        await repo.update_settings(0)
    connection.close()


@pytest.mark.asyncio
async def test_d1_transcript_update_and_missing() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO words VALUES(?,?,?,?,?)", ("one", "one", 9, None, "one.mp3"))
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    assert (await repo.update_transcript("one", "English text")).transcript == "English text"
    assert (await repo.get_word("one")).transcript == "English text"
    with pytest.raises(MissingError):
        await repo.update_transcript("missing", "English")
    connection.close()


@pytest.mark.asyncio
async def test_d1_session_create_and_ordered_read() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.executemany("INSERT INTO words VALUES(?,?,?,?,?)", [("a", "a", 9, None, "a.mp3"), ("b", "b", 9, None, "b.mp3")])
    connection.commit()
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    session = await repo.create_session("s", "screen", ["b", "a"], datetime(2026, 1, 1, tzinfo=UTC))
    assert [word.id for word in session.words] == ["b", "a"]
    assert [word.id for word in (await repo.get_session("s")).words] == ["b", "a"]
    connection.close()
