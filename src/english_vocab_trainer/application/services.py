from __future__ import annotations

from datetime import UTC, datetime

from english_vocab_trainer.domain.models import ReviewEvent, Word
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
