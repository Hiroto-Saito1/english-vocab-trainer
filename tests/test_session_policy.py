from __future__ import annotations

from datetime import UTC, datetime
from random import Random
from typing import cast
from uuid import uuid4

import pytest

from english_vocab_trainer.application.services import (
    select_session_words,
    shuffled,
    submit_review,
    utcnow,
)
from english_vocab_trainer.domain.models import ReviewAction, ReviewEvent, Settings, Word, WordState
from english_vocab_trainer.ports.errors import ConcurrentUpdateError
from english_vocab_trainer.ports.repositories import VocabularyRepository


def words(count: int) -> list[Word]:
    return [
        Word(str(index), f"term {index}", index % 4, None, f"{index}.mp3") for index in range(count)
    ]


class SessionRepository:
    def __init__(self, candidates: list[Word], due: list[Word], target: int) -> None:
        self.candidates = candidates
        self.due = due
        self.target = target

    def list_words(self, *, levels: list[int] | None = None, limit: int = 100) -> list[Word]:
        assert levels is None
        return self.candidates[:limit]

    def due_words(self, now: datetime, limit: int) -> list[Word]:
        assert now.tzinfo is UTC
        return self.due[:limit]

    def get_settings(self) -> Settings:
        return Settings(self.target)


def select(
    repository: SessionRepository, *, mode: str = "daily", count: int | None = None, seed: int = 1
) -> list[Word]:
    return select_session_words(
        cast(VocabularyRepository, repository),
        mode,
        datetime(2026, 1, 1, tzinfo=UTC),
        count,
        Random(seed),
    )


def test_daily_uses_target_as_total_cap_and_admits_due_first() -> None:
    candidates = words(200)
    due = candidates[:12]
    result = select(SessionRepository(candidates, due, 30), seed=7)
    assert len(result) == 30
    assert {word.id for word in due}.issubset({word.id for word in result})
    assert len({word.id for word in result}) == len(result)


@pytest.mark.parametrize("target", [1, 30, 100])
def test_daily_honours_every_configured_target(target: int) -> None:
    result = select(SessionRepository(words(200), [], target), seed=7)
    assert len(result) == target and len({word.id for word in result}) == target


def test_daily_shortage_and_due_backlog_never_duplicate_or_exceed_target() -> None:
    shortage = select(SessionRepository(words(3), [], 100))
    backlog = select(SessionRepository(words(200), words(150), 30))
    assert len(shortage) == 3 and len({word.id for word in shortage}) == 3
    assert {word.id for word in backlog} == {str(index) for index in range(30)}


def test_presentation_order_is_random_without_level_bands_and_seedable() -> None:
    candidates = words(100)
    first = select(SessionRepository(candidates, [], 100), seed=1)
    same_seed = select(SessionRepository(candidates, [], 100), seed=1)
    other_seed = select(SessionRepository(candidates, [], 100), seed=2)
    assert [word.id for word in first] == [word.id for word in same_seed]
    assert [word.id for word in first] != [word.id for word in other_seed]
    assert first != sorted(first, key=lambda word: (word.level is None, word.level or 0, word.id))
    assert [word.id for word in shuffled(candidates, Random(3))] != [word.id for word in candidates]


def test_screen_mode_keeps_its_existing_counts() -> None:
    repository = SessionRepository(words(120), [], 30)
    assert len(select(repository, mode="screen", count=20)) == 20
    assert len(select(repository, mode="screen", count=50)) == 50
    assert len(select(repository, mode="screen", count=100)) == 100
    with pytest.raises(ValueError):
        select(repository, mode="screen", count=19)


class RetryingReviewRepository:
    def __init__(self) -> None:
        self.attempts = 0

    def get_event(self, event_id: object) -> None:
        return None

    def has_active_review(self, word_id: str) -> bool:
        return False

    def state(self, word_id: str) -> WordState:
        return WordState(word_id, datetime(2026, 1, 1, tzinfo=UTC), version=0)

    def append_event(self, event: ReviewEvent, expected_version: int) -> WordState:
        self.attempts += 1
        if self.attempts == 1:
            raise ConcurrentUpdateError("retry")
        return WordState(event.word_id, event.reviewed_at, version=1)


def test_submit_review_retries_once_after_a_cas_conflict() -> None:
    repository = RetryingReviewRepository()
    now = utcnow()
    result = submit_review(
        cast(VocabularyRepository, repository),
        uuid4(),
        "word",
        ReviewAction.KNOWN,
        now,
    )
    assert result.created and repository.attempts == 2 and result.state.version == 1
