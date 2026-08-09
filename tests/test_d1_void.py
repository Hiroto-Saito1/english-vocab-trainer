from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import UUID

import pytest

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository
from english_vocab_trainer.adapters.local.sqlite import SCHEMA
from english_vocab_trainer.domain.models import Rating, ReviewEvent
from english_vocab_trainer.ports.errors import MissingError
from tests.fakes.d1 import FakeD1


@pytest.mark.asyncio
async def test_d1_void_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO words VALUES(?,?,?,?,?)", ("one", "one", 9, None, "one.mp3"))
    connection.commit()
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    event = ReviewEvent(UUID(int=5), "one", Rating.EASY, datetime(2026, 1, 1, tzinfo=UTC))
    await repo.append_event(event, 0)
    first = await repo.void_event(event.id)
    second = await repo.void_event(event.id)
    assert first.version == second.version == 2 and not await repo.has_active_review("one")
    assert (await repo.progress(datetime.now(UTC)))["reviewed"] == 0
    with pytest.raises(MissingError):
        await repo.void_event(UUID(int=99))
    connection.close()
