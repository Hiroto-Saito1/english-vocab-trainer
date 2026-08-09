from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from fsrs import Card, Scheduler
from fsrs import Rating as FsrsRating


class Rating(StrEnum):
    AGAIN = "again"
    GOOD = "good"
    EASY = "easy"


@dataclass(frozen=True, slots=True)
class Word:
    id: str
    term: str
    level: int | None
    transcript: str | None
    audio_key: str


@dataclass(slots=True)
class WordState:
    word_id: str
    due_at: datetime
    stability: float = 0.0
    difficulty: float = 5.0
    first_seen_at: datetime | None = None
    first_known_at: datetime | None = None
    last_known_at: datetime | None = None
    version: int = 0
    card_json: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    id: UUID
    word_id: str
    rating: Rating
    reviewed_at: datetime
    voided_at: datetime | None = None


def next_state(state: WordState, event: ReviewEvent) -> WordState:
    """Apply canonical Py-FSRS 6 scheduling; only the server runs this policy."""
    now = event.reviewed_at.astimezone(UTC)
    state.first_seen_at = state.first_seen_at or now
    card = Card.from_json(state.card_json) if state.card_json else Card()
    scheduler = Scheduler(
        desired_retention=0.90,
        learning_steps=(timedelta(minutes=10),),
        relearning_steps=(timedelta(minutes=10),),
        maximum_interval=365,
        enable_fuzzing=True,
    )
    rating = {
        Rating.AGAIN: FsrsRating.Again,
        Rating.GOOD: FsrsRating.Good,
        Rating.EASY: FsrsRating.Easy,
    }[event.rating]
    card, _ = scheduler.review_card(card, rating, now)
    if event.rating is not Rating.AGAIN:
        state.first_known_at = state.first_known_at or now
        state.last_known_at = now
    state.card_json = card.to_json()
    state.stability = card.stability or 0.0
    state.difficulty = card.difficulty or 5.0
    state.due_at = card.due
    state.version += 1
    return state
