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


class ReviewAction(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class Tier(StrEnum):
    """Public, English-only source tier for a published word."""

    UPPER = "upper"
    ULTRA = "ultra"


# This interval belongs only to the server-side FSRS scheduler.  The client
# never schedules or repeats cards on its own.
LEARNING_STEP = timedelta(minutes=10)


def rating_for_action(action: ReviewAction) -> Rating:
    return Rating.AGAIN if action is ReviewAction.UNKNOWN else Rating.GOOD


@dataclass(frozen=True, slots=True)
class Word:
    id: str
    term: str
    level: int | None
    transcript: str | None
    audio_key: str
    tier: Tier


@dataclass(frozen=True, slots=True)
class WordState:
    word_id: str
    due_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    id: UUID
    word_id: str
    rating: Rating
    reviewed_at: datetime
    voided_at: datetime | None = None


def replay_word_state(word_id: str, events: list[ReviewEvent]) -> WordState:
    """Rebuild state from genesis. Ordering is stable even for offline arrivals."""
    active = sorted(
        (event for event in events if event.voided_at is None),
        key=lambda event: (event.reviewed_at, event.id.int),
    )
    # Py-FSRS creates a time-based card id by default; seed it from word_id so
    # offline replays produce the same due date from the same event history.
    seed = int.from_bytes(word_id.encode("utf-8"), "big") % 2_000_000_000
    card = Card(card_id=seed)
    scheduler = Scheduler(
        desired_retention=0.90,
        learning_steps=(LEARNING_STEP,),
        relearning_steps=(LEARNING_STEP,),
        maximum_interval=365,
        # States are rebuilt for offline arrival and undo.  Random interval
        # fuzz would make the same event history produce a different card.
        enable_fuzzing=False,
    )
    for event in active:
        now = event.reviewed_at.astimezone(UTC)
        rating = {
            Rating.AGAIN: FsrsRating.Again,
            Rating.GOOD: FsrsRating.Good,
        }[event.rating]
        card, _ = scheduler.review_card(card, rating, now)
    return WordState(word_id, card.due if active else datetime.min.replace(tzinfo=UTC))
