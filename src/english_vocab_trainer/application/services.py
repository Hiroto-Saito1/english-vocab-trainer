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
    StudySession,
    Tier,
    Word,
    WordState,
    rating_for_action,
)
from english_vocab_trainer.ports.errors import ConcurrentUpdateError, EventConflictError
from english_vocab_trainer.ports.repositories import VocabularyRepository


def balanced_new_words(words: list[Word], limit: int, rng: Random) -> list[Word]:
    """Take new words without replacement, balancing the two published tiers."""
    pools: dict[Tier, list[Word]] = {}
    for tier in Tier:
        tier_words = [word for word in words if word.tier is tier]
        pools[tier] = rng.sample(tier_words, k=len(tier_words))
    # For an odd quota either tier may receive the extra slot; this avoids a
    # persistent upper-first presentation bias while preserving a <= 1 split.
    upper_quota = limit // 2 + rng.randrange(2 if limit % 2 else 1)
    ultra_quota = limit - upper_quota
    selected = pools[Tier.UPPER][:upper_quota] + pools[Tier.ULTRA][:ultra_quota]
    remainder = pools[Tier.UPPER][upper_quota:] + pools[Tier.ULTRA][ultra_quota:]
    return (selected + remainder)[:limit]


def select_session_words(repo: VocabularyRepository, now: datetime, rng: Random) -> list[Word]:
    unseen = repo.list_words(limit=10_000)
    target = 30
    due = repo.due_words(now, target)
    due_ids = {word.id for word in due}
    quota = max(0, target - len(due))
    selected = due + balanced_new_words(
        [word for word in unseen if word.id not in due_ids], quota, rng
    )
    # Due cards win admission to the daily quota; their presentation order, and
    # the new cards' order, are still random so a level band cannot dominate.
    return rng.sample(selected, k=len(selected))


def create_study_session(
    repo: VocabularyRepository,
    now: datetime,
    session_id: str,
    rng: Random,
) -> StudySession:
    words = select_session_words(repo, now, rng)
    return StudySession(session_id, tuple(words))


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
            ReviewAction.KNOWN: {Rating.EASY, Rating.GOOD},
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
    rating = rating_for_action(action, repo.has_active_review(word_id))
    event = ReviewEvent(event_id, word_id, rating, reviewed_at)
    for _ in range(3):
        try:
            state = repo.append_event(event, repo.state(word_id).version)
            return ReviewResult(
                rating=rating,
                state=state,
                status="applied",
            )
        except ConcurrentUpdateError:
            continue
    raise ConcurrentUpdateError("CAS retry exhausted")
