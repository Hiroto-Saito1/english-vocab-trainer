import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.application.services import submit_review
from english_vocab_trainer.domain.models import Rating, ReviewAction, ReviewEvent, Tier, Word
from english_vocab_trainer.ports.errors import (
    ConcurrentUpdateError,
    EventConflictError,
    MissingError,
)


def words(repository: SQLiteVocabularyRepository) -> None:
    repository.bulk_upsert_words(
        [
            Word("nine", "nine", 9, None, "nine.mp3", Tier.UPPER),
            Word("ten", "ten", 10, None, "ten.mp3", Tier.ULTRA),
            Word("extra", "extra", None, None, "extra.mp3", Tier.UPPER),
        ]
    )


def event(word: str, when: datetime) -> ReviewEvent:
    return ReviewEvent(uuid4(), word, Rating.EASY, when)


def test_reopen_persists_words_state_and_events(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    now = datetime.now(UTC)
    with SQLiteVocabularyRepository(path) as repository:
        words(repository)
        repository.append_event(event("nine", now), 0)
    with SQLiteVocabularyRepository(path) as repository:
        stored = repository.get_word("nine")
        assert stored is not None and stored.tier is Tier.UPPER
        assert repository.state("nine").version == 1


def test_list_excludes_active_review_and_void_reincludes_word(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        review = event("nine", datetime.now(UTC))
        repository.append_event(review, 0)
        assert "nine" not in [word.id for word in repository.list_words()]
        repository.void_event(review.id)
        assert "nine" in [word.id for word in repository.list_words()]
        assert not repository.has_active_review("nine")


def test_due_returns_past_cards_only(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        now = datetime.now(UTC)
        repository.append_event(event("nine", now), 0)
        assert not repository.due_words(now, 10)
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id=?",
            ((now - timedelta(minutes=1)).isoformat(), "nine"),
        )
        assert [word.id for word in repository.due_words(now, 10)] == ["nine"]


def test_schema_rejects_invalid_tier_rating_and_missing_word(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute("INSERT INTO words VALUES('x','x',NULL,NULL,'x','unknown')")
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute("INSERT INTO review_events VALUES('x','none','bad','now',NULL)")


def test_submit_review_retries_are_idempotent(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        now = datetime.now(UTC)
        known_id = uuid4()
        first = submit_review(repository, known_id, "nine", ReviewAction.KNOWN, now)
        second = submit_review(repository, known_id, "nine", ReviewAction.KNOWN, now)
        unknown_id = uuid4()
        unknown = submit_review(repository, unknown_id, "ten", ReviewAction.UNKNOWN, now)
        unknown_retry = submit_review(repository, unknown_id, "ten", ReviewAction.UNKNOWN, now)
        assert first.rating is second.rating is Rating.EASY
        assert first.state.version == second.state.version == 1
        assert unknown.rating is unknown_retry.rating is Rating.AGAIN
        assert unknown_retry.status == "idempotent"


def test_missing_word_and_stale_cas_leave_no_partial_event(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        with pytest.raises(MissingError):
            repository.append_event(event("missing", datetime.now(UTC)), 0)
        words(repository)
        repository.append_event(event("nine", datetime.now(UTC)), 0)
        stale = event("nine", datetime.now(UTC))
        with pytest.raises(ConcurrentUpdateError):
            repository.append_event(stale, 0)
        assert repository.get_event(stale.id) is None


def test_uuid_idempotency_compares_structured_event_fields(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        words(repository)
        review = event("nine", datetime.now(UTC))
        first = repository.append_event(review, 0)
        same_instant = ReviewEvent(
            review.id,
            review.word_id,
            review.rating,
            review.reviewed_at.astimezone(timezone(timedelta(hours=9))),
        )
        second = repository.append_event(same_instant, 99)
        assert first.version == second.version
        for changed in (
            ReviewEvent(review.id, "ten", review.rating, review.reviewed_at),
            ReviewEvent(review.id, review.word_id, Rating.AGAIN, review.reviewed_at),
            ReviewEvent(
                review.id, review.word_id, review.rating, review.reviewed_at + timedelta(1)
            ),
        ):
            with pytest.raises(EventConflictError):
                repository.append_event(changed, 0)


def test_out_of_order_replay_and_double_void_are_deterministic(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    first = event("nine", now)
    second = ReviewEvent(uuid4(), "nine", Rating.AGAIN, now + timedelta(days=1))
    with SQLiteVocabularyRepository(tmp_path / "one.db") as repository:
        words(repository)
        repository.append_event(second, 0)
        actual = repository.append_event(first, 1)
        voided = repository.void_event(second.id)
        repeated = repository.void_event(second.id)
    with SQLiteVocabularyRepository(tmp_path / "two.db") as repository:
        words(repository)
        repository.append_event(first, 0)
        expected = repository.append_event(second, 1)
    assert actual.card_json == expected.card_json and actual.due_at == expected.due_at
    assert voided.card_json == repeated.card_json and voided.version == repeated.version
