from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import UUID

import pytest

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository
from english_vocab_trainer.adapters.local.sqlite import SCHEMA
from english_vocab_trainer.domain.models import Rating, ReviewEvent
from english_vocab_trainer.ports.errors import ConcurrentUpdateError, EventConflictError
from tests.fakes.d1 import FakeD1


@pytest.mark.asyncio
async def test_d1_appends_initial_review_event() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO words VALUES(?,?,?,?,?)", ("one", "one", 9, None, "one.mp3"))
    connection.commit()
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    event = ReviewEvent(UUID(int=1), "one", Rating.EASY, datetime(2026, 1, 1, tzinfo=UTC))
    state = await repo.append_event(event, 0)
    assert state.version == 1 and state.card_json is not None and state.first_known_at is not None
    assert await repo.get_event(event.id) == event and await repo.has_active_review("one")
    assert (await repo.progress(datetime(2026, 1, 2, tzinfo=UTC)))["reviewed"] == 1
    connection.close()


@pytest.mark.asyncio
async def test_d1_event_uuid_is_idempotent_and_user_scoped() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO words VALUES(?,?,?,?,?)", ("one", "one", 9, None, "one.mp3"))
    connection.commit()
    event = ReviewEvent(UUID(int=2), "one", Rating.EASY, datetime(2026, 1, 1, tzinfo=UTC))
    alice = D1VocabularyRepository(FakeD1(connection), "alice")
    assert (await alice.append_event(event, 0)).version == 1 and (
        await alice.append_event(event, 1)
    ).version == 1
    with pytest.raises(EventConflictError):
        await alice.append_event(ReviewEvent(event.id, "one", Rating.AGAIN, event.reviewed_at), 1)
    with pytest.raises(EventConflictError):
        await D1VocabularyRepository(FakeD1(connection), "bob").append_event(event, 0)
    connection.close()


@pytest.mark.asyncio
async def test_d1_stale_cas_does_not_append() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO words VALUES(?,?,?,?,?)", ("one", "one", 9, None, "one.mp3"))
    connection.commit()
    repo = D1VocabularyRepository(FakeD1(connection), "alice")
    await repo.append_event(
        ReviewEvent(UUID(int=3), "one", Rating.EASY, datetime(2026, 1, 1, tzinfo=UTC)), 0
    )
    with pytest.raises(ConcurrentUpdateError):
        await repo.append_event(
            ReviewEvent(UUID(int=4), "one", Rating.GOOD, datetime(2026, 1, 2, tzinfo=UTC)), 0
        )
    assert (await repo.state("one")).version == 1 and (await repo.progress(datetime.now(UTC)))[
        "reviewed"
    ] == 1
    connection.close()
