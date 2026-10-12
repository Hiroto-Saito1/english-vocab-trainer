import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Review, ReviewAction, Word, replay_word_state
from english_vocab_trainer.ports.errors import MissingError, ReviewConflictError


def seed(repository: SQLiteVocabularyRepository) -> None:
    repository.bulk_upsert_words(
        [
            Word("one", "one", 1, "definition one", "one.mp3"),
            Word("two", "two", 2, "definition two", "two.mp3"),
            Word("unknown", "unknown", None, "definition unknown", "unknown.mp3"),
        ]
    )


def review(
    word_id: str,
    when: datetime,
    *,
    review_id: UUID | None = None,
    action: ReviewAction = ReviewAction.KNOWN,
) -> Review:
    return Review(review_id or uuid4(), word_id, action, when)


def test_reopen_persists_word_state_and_review(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    now = datetime.now(UTC)
    stored = review("one", now)
    with SQLiteVocabularyRepository(path) as repository:
        seed(repository)
        repository.save_review(stored)
    with SQLiteVocabularyRepository(path) as repository:
        assert repository.get_word("one") == Word("one", "one", 1, "definition one", "one.mp3")
        assert repository.get_review(stored.id) == stored
        assert repository.state("one").due_at > now


def test_next_word_prefers_due_then_lowest_level_unseen(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        assert repository.next_word(now).id == "one"  # type: ignore[union-attr]
        repository.save_review(review("one", now))
        assert repository.next_word(now).id == "two"  # type: ignore[union-attr]
        repository.save_review(review("two", now))
        assert repository.next_word(now).id == "unknown"  # type: ignore[union-attr]
        repository.save_review(review("unknown", now))
        assert repository.next_word(now) is None
        assert repository.next_due(now) is not None
        repository.db.execute(
            "UPDATE word_state SET due_at=? WHERE word_id='two'",
            ((now - timedelta(seconds=1)).isoformat(),),
        )
        assert repository.next_word(now).id == "two"  # type: ignore[union-attr]


def test_empty_catalog_has_no_next_due(tmp_path: Path) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        assert repository.next_due(datetime.now(UTC)) is None


def test_schema_rejects_missing_definition_invalid_action_and_missing_word(
    tmp_path: Path,
) -> None:
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute(
                "INSERT INTO words(id,term,level,definition,audio_key) "
                "VALUES('x','x',NULL,NULL,'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            repository.db.execute(
                "INSERT INTO reviews(id,word_id,action,reviewed_at) VALUES('x','none','bad','now')"
            )


def test_save_is_idempotent_and_correction_preserves_server_timestamp(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    review_id = uuid4()
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        repository.save_review(review("one", now, review_id=review_id))
        repository.save_review(review("one", now + timedelta(days=1), review_id=review_id))
        repository.save_review(
            review(
                "one",
                now + timedelta(days=2),
                review_id=review_id,
                action=ReviewAction.UNKNOWN,
            )
        )
        corrected = Review(review_id, "one", ReviewAction.UNKNOWN, now)
        assert repository.get_review(review_id) == corrected
        assert repository.state("one") == replay_word_state("one", [corrected])


def test_review_identity_conflict_and_missing_word_are_atomic(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    review_id = uuid4()
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        repository.save_review(review("one", now, review_id=review_id))
        with pytest.raises(ReviewConflictError):
            repository.save_review(review("two", now, review_id=review_id))
        with pytest.raises(MissingError):
            repository.save_review(review("missing", now))


def test_history_pages_are_stable_when_timestamps_match(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with SQLiteVocabularyRepository(tmp_path / "v.db") as repository:
        seed(repository)
        reviews = [
            review("one", now, review_id=UUID(int=1)),
            review("two", now, review_id=UUID(int=2)),
            review("unknown", now, review_id=UUID(int=3)),
        ]
        for stored in reviews:
            repository.save_review(stored)
        first = repository.history_page(None, 2)
        cursor = (first[-1][0].reviewed_at, first[-1][0].id)
        second = repository.history_page(cursor, 2)
    assert [stored.id for stored, _ in first] == [UUID(int=3), UUID(int=2)]
    assert [stored.id for stored, _ in second] == [UUID(int=1)]


def test_concurrent_distinct_reviews_are_serialized_and_both_persist(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    with SQLiteVocabularyRepository(path) as repository:
        seed(repository)
    start = Barrier(2, timeout=10)
    first = Review(UUID(int=1), "one", ReviewAction.KNOWN, datetime(2026, 1, 1, tzinfo=UTC))
    second = Review(UUID(int=2), "one", ReviewAction.UNKNOWN, datetime(2026, 1, 2, tzinfo=UTC))

    def save(stored: Review) -> None:
        with SQLiteVocabularyRepository(path) as repository:
            start.wait()
            repository.save_review(stored)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(save, stored) for stored in (first, second)]
        for future in futures:
            future.result(timeout=15)

    with SQLiteVocabularyRepository(path) as repository:
        assert repository.get_review(first.id) == first
        assert repository.get_review(second.id) == second
        assert repository.state("one") == replay_word_state("one", [first, second])
