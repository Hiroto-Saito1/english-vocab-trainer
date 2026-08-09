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
from english_vocab_trainer.ports.repositories import VocabularyRepository


def daily_study(repo: VocabularyRepository, now: datetime, size: int = 30) -> list[Word]:
    due = repo.due_words(now, size)
    if len(due) >= size:
        return due
    return due + repo.list_words(limit=min(20, size - len(due)))


def screen_new_words(repo: VocabularyRepository, count: int) -> list[Word]:
    if count not in {20, 50, 100}:
        raise ValueError("count must be 20, 50, or 100")
    return repo.list_words(limit=count)


def apply_events(repo: VocabularyRepository, events: list[ReviewEvent]) -> list[str]:
    applied: list[str] = []
    for event in events:
        for _ in range(3):
            try:
                repo.append_event(event, repo.state(event.word_id).version)
                applied.append(str(event.id))
                break
            except RuntimeError:
                continue
    return applied


def utcnow() -> datetime:
    return datetime.now(UTC)


def shuffle_within_level_bands(words: list[Word], rng: Random) -> list[Word]:
    grouped: dict[int | None, list[Word]] = {}
    for word in words:
        grouped.setdefault(word.level, []).append(word)
    result: list[Word] = []
    for level in sorted(level for level in grouped if level is not None) + [None]:
        band = list(grouped.get(level, []))
        rng.shuffle(band)
        result.extend(band)
    return result


def select_session_words(
    repo: VocabularyRepository, mode: str, now: datetime, count: int | None, rng: Random
) -> list[Word]:
    new = shuffle_within_level_bands(repo.list_words(limit=10_000), rng)
    if mode == "screen":
        if count not in {20, 50, 100}:
            raise ValueError("screen count must be 20, 50, or 100")
        return new[:count]
    if mode != "daily":
        raise ValueError("mode must be daily or screen")
    due = repo.due_words(now, min(repo.get_settings().daily_target, 30))
    due_ids = {word.id for word in due}
    return due + [word for word in new if word.id not in due_ids][:20]


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
            raise RuntimeError("review event conflict")
        return ReviewResult(
            event_id,
            word_id,
            action,
            existing.rating,
            repo.state(word_id),
            existing.voided_at is not None,
        )
    rating = rating_for_action(action, repo.has_active_review(word_id))
    event = ReviewEvent(event_id, word_id, rating, reviewed_at)
    for _ in range(3):
        try:
            state = repo.append_event(event, repo.state(word_id).version)
            return ReviewResult(event_id, word_id, action, rating, state)
        except RuntimeError:
            continue
    raise RuntimeError("CAS retry exhausted")
