import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.application.services import submit_review
from english_vocab_trainer.domain.models import (
    Rating,
    ReviewAction,
    ReviewEvent,
    Tier,
    Word,
    replay_word_state,
)
from english_vocab_trainer.ports.errors import EventConflictError, MissingError


def seed(repository: SQLiteVocabularyRepository) -> None:
    repository.bulk_upsert_words(
        [
            Word("one", "one", 1, "definition one", "one.mp3", Tier.UPPER),
            Word("two", "two", 2, "definition two", "two.mp3", Tier.ULTRA),
            Word("unknown", "unknown", None, None, "unknown.mp3", Tier.UPPER),
        ]
    )


def review(word_id: str, when: datetime, *, event_id: UUID | None = None) -> ReviewEvent:
    return ReviewEvent(event_id or uuid4(), word_id, Rating.GOOD, when)


def test_reopen_persists_word_state_and_event(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    now = datetime.now(UTC)
    stored = review("one", now)
    with SQLiteVocabularyRepository(path) as repository:
        seed(repository)
        repository.save_event(stored)
    with SQLiteVocabularyRepository(path) as repository:
        assert repository.get_word("one") == Word(
            "one", "one", 1, "definition one", "one.mp3", Tier.UPPER
        )
        assert repository.get_event(stored.id) == stored
        assert repository.state("one").due_at > now


def test_next_word_prefers_due_then_lowest_level_unseen(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        assert repository.next_word(now).id == "one"  # type: ignore[union-attr]
        repository.save_event(review("one", now))
        assert repository.next_word(now).id == "two"  # type: ignore[union-attr]
        repository.save_event(review("two", now))
        assert repository.next_word(now).id == "unknown"  # type: ignore[union-attr]
        repository.save_event(review("unknown", now))
        assert repository.next_word(now) is None
        status, next_due = repository.empty_card_status(now)
        assert status == "next_due" and next_due is not None
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id='two'",
            ((now - timedelta(seconds=1)).isoformat(),),
        )
        assert repository.next_word(now).id == "two"  # type: ignore[union-attr]


def test_empty_catalog_status(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        assert repository.empty_card_status(datetime.now(UTC)) == ("catalog_empty", None)


def test_schema_rejects_invalid_tier_rating_and_missing_word(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute(
                "INSERT INTO words(id,term,level,definition,audio_key,tier) "
                "VALUES('x','x',NULL,NULL,'x','unknown')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute(
                "INSERT INTO review_events(id,word_id,rating,reviewed_at) "
                "VALUES('x','none','bad','now')"
            )


def test_save_is_idempotent_and_updates_rating_for_same_identity(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    event_id = uuid4()
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        applied = submit_review(repository, event_id, "one", ReviewAction.KNOWN, now)
        retry = submit_review(repository, event_id, "one", ReviewAction.KNOWN, now)
        corrected = submit_review(repository, event_id, "one", ReviewAction.UNKNOWN, now)
        assert applied.status == "applied"
        assert retry.status == "idempotent"
        assert corrected.status == "updated" and corrected.rating is Rating.AGAIN
        assert repository.get_event(event_id) == ReviewEvent(event_id, "one", Rating.AGAIN, now)
        assert corrected.state == replay_word_state(
            "one", [ReviewEvent(event_id, "one", Rating.AGAIN, now)]
        )


def test_event_identity_conflict_and_missing_word_are_atomic(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    event_id = uuid4()
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        repository.save_event(review("one", now, event_id=event_id))
        same_instant = ReviewEvent(
            event_id,
            "one",
            Rating.GOOD,
            now.astimezone(timezone(timedelta(hours=9))),
        )
        assert repository.save_event(same_instant)[1] == "idempotent"
        for changed in (
            review("two", now, event_id=event_id),
            review("one", now + timedelta(seconds=1), event_id=event_id),
        ):
            with pytest.raises(EventConflictError):
                repository.save_event(changed)
        with pytest.raises(MissingError):
            repository.save_event(review("missing", now))


def test_history_pages_are_stable_when_timestamps_match(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        events = [
            review("one", now, event_id=UUID(int=1)),
            review("two", now, event_id=UUID(int=2)),
            review("unknown", now, event_id=UUID(int=3)),
        ]
        for event in events:
            repository.save_event(event)
        first = repository.history_page(None, 2)
        cursor = (first[-1][0].reviewed_at, first[-1][0].id)
        second = repository.history_page(cursor, 2)
    assert [event.id for event, _ in first] == [UUID(int=3), UUID(int=2)]
    assert [event.id for event, _ in second] == [UUID(int=1)]


def test_concurrent_distinct_events_are_serialized_and_both_persist(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    with SQLiteVocabularyRepository(path) as repository:
        seed(repository)
    start = Barrier(2, timeout=10)
    first = ReviewEvent(UUID(int=1), "one", Rating.GOOD, datetime(2026, 1, 1, tzinfo=UTC))
    second = ReviewEvent(UUID(int=2), "one", Rating.AGAIN, datetime(2026, 1, 2, tzinfo=UTC))

    def save(event: ReviewEvent) -> None:
        with SQLiteVocabularyRepository(path) as repository:
            start.wait()
            repository.save_event(event)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(save, event) for event in (first, second)]
        for future in futures:
            future.result(timeout=15)

    with SQLiteVocabularyRepository(path) as repository:
        assert repository.get_event(first.id) == first
        assert repository.get_event(second.id) == second
        assert repository.state("one") == replay_word_state("one", [first, second])
