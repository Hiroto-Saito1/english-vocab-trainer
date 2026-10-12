from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from fsrs import Card, Scheduler
from fsrs import Rating as FsrsRating


class ReviewAction(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


# This interval belongs only to the server-side FSRS scheduler.  The client
# never schedules or repeats cards on its own.
LEARNING_STEP = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Word:
    id: str
    term: str
    level: int | None
    definition: str
    audio_key: str


@dataclass(frozen=True, slots=True)
class WordState:
    word_id: str
    due_at: datetime


@dataclass(frozen=True, slots=True)
class Review:
    id: UUID
    word_id: str
    action: ReviewAction
    reviewed_at: datetime


def replay_word_state(word_id: str, reviews: list[Review]) -> WordState:
    """Rebuild state from genesis in stable review order."""
    ordered = sorted(reviews, key=lambda review: (review.reviewed_at, review.id.int))
    # Py-FSRS creates a time-based card id by default; seed it from word_id so
    # rebuilding the same server history produces the same due date.
    seed = int.from_bytes(word_id.encode("utf-8"), "big") % 2_000_000_000
    card = Card(card_id=seed)
    scheduler = Scheduler(
        desired_retention=0.90,
        learning_steps=(LEARNING_STEP,),
        relearning_steps=(LEARNING_STEP,),
        maximum_interval=365,
        # Random interval fuzz would make the same review history produce a
        # different card when a historical answer is corrected.
        enable_fuzzing=False,
    )
    for review in ordered:
        now = review.reviewed_at.astimezone(UTC)
        fsrs_rating = FsrsRating.Good if review.action is ReviewAction.KNOWN else FsrsRating.Again
        card, _ = scheduler.review_card(card, fsrs_rating, now)
    return WordState(word_id, card.due if ordered else datetime.min.replace(tzinfo=UTC))
