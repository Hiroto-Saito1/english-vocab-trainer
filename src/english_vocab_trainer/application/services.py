from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from english_vocab_trainer.ports.repositories import VocabularyRepository


def select_next_word(repo: VocabularyRepository, now: datetime) -> Word | None:
    """Return one due word, or the lowest-level word not reviewed before."""
    return repo.next_word(now)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    rating: Rating
    state: WordState
    status: Literal["applied", "idempotent", "updated"]


def submit_review(
    repo: VocabularyRepository,
    event_id: UUID,
    word_id: str,
    action: ReviewAction,
    reviewed_at: datetime,
) -> ReviewResult:
    rating = rating_for_action(action)
    event = ReviewEvent(event_id, word_id, rating, reviewed_at)
    state, status = repo.save_event(event)
    return ReviewResult(
        rating=rating,
        state=state,
        status=status,
    )
