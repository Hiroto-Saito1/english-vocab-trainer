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


def words(repository: SQLiteVocabularyRepository) -> None:
    repository.bulk_upsert_words(
        [
            Word("nine", "nine", 9, None, "nine.mp3", Tier.UPPER),
            Word("ten", "ten", 10, None, "ten.mp3", Tier.ULTRA),
            Word("extra", "extra", None, None, "extra.mp3", Tier.UPPER),
        ]
    )


def event(word: str, when: datetime) -> ReviewEvent:
    return ReviewEvent(uuid4(), word, Rating.GOOD, when)


def test_reopen_persists_words_state_and_events(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    now = datetime.now(UTC)
    with SQLiteVocabularyRepository(path) as repository:
        words(repository)
        repository.append_event(event("nine", now))
    with SQLiteVocabularyRepository(path) as repository:
        stored = repository.get_word("nine")
        assert stored is not None and stored.tier is Tier.UPPER
        assert repository.state("nine").due_at > now


def test_list_excludes_active_review_and_void_reincludes_word(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        review = event("nine", datetime.now(UTC))
        repository.append_event(review)
        assert "nine" not in [word.id for word in repository.list_words()]
        repository.void_event(review.id)
        assert "nine" in [word.id for word in repository.list_words()]


def test_due_returns_past_cards_only(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        now = datetime.now(UTC)
        repository.append_event(event("nine", now))
        assert not repository.due_words(now, 10)
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id=?",
            ((now - timedelta(minutes=1)).isoformat(), "nine"),
        )
        assert [word.id for word in repository.due_words(now, 10)] == ["nine"]


def test_schema_rejects_invalid_tier_rating_and_missing_word(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute(
                "INSERT INTO words(id,term,level,transcript,audio_key,tier) "
                "VALUES('x','x',NULL,NULL,'x','unknown')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute(
                "INSERT INTO review_events(id,word_id,rating,reviewed_at,voided_at) "
                "VALUES('x','none','bad','now',NULL)"
            )


def test_submit_review_is_idempotent_for_known_and_unknown(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        now = datetime.now(UTC)
        known_id = uuid4()
        first = submit_review(repository, known_id, "nine", ReviewAction.KNOWN, now)
        second = submit_review(repository, known_id, "nine", ReviewAction.KNOWN, now)
        unknown_id = uuid4()
        unknown = submit_review(repository, unknown_id, "ten", ReviewAction.UNKNOWN, now)
        unknown_retry = submit_review(repository, unknown_id, "ten", ReviewAction.UNKNOWN, now)
        assert first.rating is second.rating is Rating.GOOD
        assert first.state.due_at == second.state.due_at
        assert unknown.rating is unknown_retry.rating is Rating.AGAIN
        assert unknown_retry.status == "idempotent"


def test_missing_word_leaves_no_partial_event(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        with pytest.raises(MissingError):
            repository.append_event(event("missing", datetime.now(UTC)))
        words(repository)
        stored = event("nine", datetime.now(UTC))
        repository.append_event(stored)
        assert repository.get_event(stored.id) is not None


def test_uuid_idempotency_compares_structured_event_fields(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        review = event("nine", datetime.now(UTC))
        first = repository.append_event(review)
        same_instant = ReviewEvent(
            review.id,
            review.word_id,
            review.rating,
            review.reviewed_at.astimezone(timezone(timedelta(hours=9))),
        )
        second = repository.append_event(same_instant)
        assert first.due_at == second.due_at
        for changed in (
            ReviewEvent(review.id, "ten", review.rating, review.reviewed_at),
            ReviewEvent(review.id, review.word_id, Rating.AGAIN, review.reviewed_at),
            ReviewEvent(
                review.id, review.word_id, review.rating, review.reviewed_at + timedelta(1)
            ),
        ):
            with pytest.raises(EventConflictError):
                repository.append_event(changed)


def test_out_of_order_replay_and_double_void_are_deterministic(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    first = event("nine", now)
    second = ReviewEvent(uuid4(), "nine", Rating.AGAIN, now + timedelta(days=1))
    with SQLiteVocabularyRepository(tmp_path / "one.db") as repository:
        words(repository)
        repository.append_event(second)
        actual = repository.append_event(first)
        voided = repository.void_event(second.id)
        repeated = repository.void_event(second.id)
    with SQLiteVocabularyRepository(tmp_path / "two.db") as repository:
        words(repository)
        repository.append_event(first)
        expected = repository.append_event(second)
    assert actual.due_at == expected.due_at
    assert voided.due_at == repeated.due_at


def test_concurrent_distinct_events_are_serialized_and_both_persist(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    with SQLiteVocabularyRepository(path) as repository:
        words(repository)
    start = Barrier(2, timeout=10)
    first = ReviewEvent(UUID(int=1), "nine", Rating.GOOD, datetime(2026, 1, 1, tzinfo=UTC))
    second = ReviewEvent(UUID(int=2), "nine", Rating.AGAIN, datetime(2026, 1, 2, tzinfo=UTC))

    def append(review: ReviewEvent) -> None:
        with SQLiteVocabularyRepository(path) as repository:
            start.wait()
            repository.append_event(review)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append, review) for review in (first, second)]
        for future in futures:
            future.result(timeout=15)

    with SQLiteVocabularyRepository(path) as repository:
        assert repository.get_event(first.id) == first
        assert repository.get_event(second.id) == second
        assert repository.state("nine").due_at == replay_word_state("nine", [first, second]).due_at


def test_offline_unknown_then_known_has_same_state_when_synced_in_reverse(tmp_path: Path) -> None:
    reviewed_at = datetime(2026, 1, 1, tzinfo=UTC)
    unknown = (UUID(int=21), ReviewAction.UNKNOWN, reviewed_at)
    known = (UUID(int=22), ReviewAction.KNOWN, reviewed_at + timedelta(minutes=1))
    due_dates = []
    for path, order in (
        (tmp_path / "forward.db", (unknown, known)),
        (tmp_path / "reverse.db", (known, unknown)),
    ):
        with SQLiteVocabularyRepository(path) as repository:
            words(repository)
            for event_id, action, when in order:
                submit_review(repository, event_id, "nine", action, when)
            due_dates.append(repository.state("nine").due_at)
            stored = [repository.get_event(event_id) for event_id, _, _ in order]
            assert {event.rating for event in stored if event is not None} == {
                Rating.AGAIN,
                Rating.GOOD,
            }
    assert due_dates[0] == due_dates[1]
