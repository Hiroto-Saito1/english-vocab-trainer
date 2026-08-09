from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from random import Random
from uuid import UUID

from english_vocab_trainer.domain.models import (
    Rating,
    ReviewAction,
    ReviewEvent,
    StudySession,
    Word,
    WordState,
    rating_for_action,
)
from english_vocab_trainer.ports.errors import ConcurrentUpdateError, EventConflictError
from english_vocab_trainer.ports.repositories import VocabularyRepository


def utcnow() -> datetime:
    return datetime.now(UTC)


def shuffled(words: list[Word], rng: Random) -> list[Word]:
    """Return a random, without-replacement presentation order."""
    result = list(words)
    rng.shuffle(result)
    return result


def select_session_words(
    repo: VocabularyRepository, mode: str, now: datetime, count: int | None, rng: Random
) -> list[Word]:
    new = shuffled(repo.list_words(limit=10_000), rng)
    if mode == "screen":
        if count not in {20, 50, 100}:
            raise ValueError("screen count must be 20, 50, or 100")
        return new[:count]
    if mode != "daily":
        raise ValueError("mode must be daily or screen")
    target = repo.get_settings().daily_target
    due = repo.due_words(now, target)
    due_ids = {word.id for word in due}
    selected = due + [word for word in new if word.id not in due_ids][: max(0, target - len(due))]
    # Due cards win admission to the daily quota; their presentation order, and
    # the new cards' order, are still random so a level band cannot dominate.
    return shuffled(selected, rng)


def create_study_session(
    repo: VocabularyRepository,
    mode: str,
    now: datetime,
    session_id: str,
    count: int | None,
    rng: Random,
) -> StudySession:
    words = select_session_words(repo, mode, now, count, rng)
    return repo.create_session(session_id, mode, [word.id for word in words], now)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    id: UUID
    word_id: str
    action: ReviewAction
    rating: Rating
    state: WordState
    created: bool
    voided: bool = False


def submit_review(
    repo: VocabularyRepository,
    event_id: UUID,
    word_id: str,
    action: ReviewAction,
    reviewed_at: datetime,
) -> ReviewResult:
    existing = repo.get_event(event_id)
    if existing is not None:
        compatible = existing.word_id == word_id and existing.reviewed_at == reviewed_at
        compatible = compatible and (
            action is ReviewAction.UNKNOWN
            and existing.rating is Rating.AGAIN
            or action is ReviewAction.KNOWN
            and existing.rating in {Rating.EASY, Rating.GOOD}
        )
        if not compatible:
            raise EventConflictError("review event conflict")
        return ReviewResult(
            id=event_id,
            word_id=word_id,
            action=action,
            rating=existing.rating,
            state=repo.state(word_id),
            created=False,
            voided=existing.voided_at is not None,
        )
    rating = rating_for_action(action, repo.has_active_review(word_id))
    event = ReviewEvent(event_id, word_id, rating, reviewed_at)
    for _ in range(3):
        try:
            state = repo.append_event(event, repo.state(word_id).version)
            return ReviewResult(
                id=event_id,
                word_id=word_id,
                action=action,
                rating=rating,
                state=state,
                created=True,
            )
        except ConcurrentUpdateError:
            continue
    raise ConcurrentUpdateError("CAS retry exhausted")
