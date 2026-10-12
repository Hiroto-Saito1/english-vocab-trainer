from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from random import Random
from typing import Literal
from uuid import UUID

from english_vocab_trainer.domain.models import (
    Rating,
    ReviewAction,
    ReviewEvent,
    Word,
    WordState,
    rating_for_action,
)
from english_vocab_trainer.ports.errors import EventConflictError
from english_vocab_trainer.ports.repositories import VocabularyRepository


def select_set_words(repo: VocabularyRepository, now: datetime, rng: Random) -> list[Word]:
    """Admit due cards first, then the lowest-level unseen words, up to 30."""
    target = 30
    due = repo.due_words(now, target)
    selected = due + repo.new_words_by_level(limit=target - len(due))
    # Admission is deterministic by due time and difficulty; presentation is not.
    return rng.sample(selected, k=len(selected))


@dataclass(frozen=True, slots=True)
class ReviewResult:
    rating: Rating
    state: WordState
    status: Literal["applied", "idempotent", "voided"]


def submit_review(
    repo: VocabularyRepository,
    event_id: UUID,
    word_id: str,
    action: ReviewAction,
    reviewed_at: datetime,
) -> ReviewResult:
    existing = repo.get_event(event_id)
    if existing is not None:
        allowed_ratings = {
            ReviewAction.UNKNOWN: {Rating.AGAIN},
            ReviewAction.KNOWN: {Rating.GOOD},
        }[action]
        if (
            existing.word_id != word_id
            or existing.reviewed_at != reviewed_at
            or existing.rating not in allowed_ratings
        ):
            raise EventConflictError("review event conflict")
        return ReviewResult(
            rating=existing.rating,
            state=repo.state(word_id),
            status="voided" if existing.voided_at is not None else "idempotent",
        )
    rating = rating_for_action(action)
    event = ReviewEvent(event_id, word_id, rating, reviewed_at)
    return ReviewResult(
        rating=rating,
        state=repo.append_event(event),
        status="applied",
    )
