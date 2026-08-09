from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from english_vocab_trainer.domain.models import (
    ReviewEvent,
    Settings,
    StudySession,
    Word,
    WordState,
    next_state,
    replay_word_state,
)


class InMemoryVocabularyRepository:
    def __init__(self, words: list[Word] | None = None) -> None:
        self.words = {word.id: word for word in words or []}
        self.states: dict[str, WordState] = {}
        self.events: dict[str, ReviewEvent] = {}
        self.settings = Settings()
        self.sessions: dict[str, StudySession] = {}

    def for_user(self, user_id: str) -> InMemoryVocabularyRepository:
        return self

    def list_words(self, *, levels: list[int] | None = None, limit: int = 100) -> list[Word]:
        selected = [
            w
            for w in self.words.values()
            if not self.has_active_review(w.id) and (levels is None or w.level in levels)
        ]
        return sorted(selected, key=lambda w: (w.level is None, w.level or 99, w.id))[:limit]

    def due_words(self, now: datetime, limit: int) -> list[Word]:
        return [
            self.words[s.word_id]
            for s in sorted(self.states.values(), key=lambda state: state.due_at)
            if s.due_at <= now and s.word_id in self.words
        ][:limit]

    def get_word(self, word_id: str) -> Word | None:
        return self.words.get(word_id)

    def update_transcript(self, word_id: str, transcript: str) -> Word:
        word = self.words[word_id]
        replacement = Word(word.id, word.term, word.level, transcript, word.audio_key)
        self.words[word_id] = replacement
        return replacement

    def state(self, word_id: str) -> WordState:
        return self.states.setdefault(word_id, WordState(word_id, datetime.min.replace(tzinfo=UTC)))

    def has_active_review(self, word_id: str) -> bool:
        return any(
            event.word_id == word_id and event.voided_at is None for event in self.events.values()
        )

    def append_event(self, event: ReviewEvent, expected_version: int) -> WordState:
        if str(event.id) in self.events:
            return self.state(event.word_id)
        state = self.state(event.word_id)
        if state.version != expected_version:
            raise RuntimeError("CAS conflict")
        self.events[str(event.id)] = event
        return next_state(state, event)

    def void_event(self, event_id: UUID) -> WordState:
        event = self.events[str(event_id)]
        self.events[str(event_id)] = ReviewEvent(
            event.id, event.word_id, event.rating, event.reviewed_at, datetime.now(UTC)
        )
        state = replay_word_state(
            event.word_id,
            [x for x in self.events.values() if x.word_id == event.word_id],
            self.state(event.word_id).version + 1,
        )
        self.states[event.word_id] = state
        return state

    def get_settings(self) -> Settings:
        return self.settings

    def update_settings(self, daily_target: int) -> Settings:
        if not 1 <= daily_target <= 100:
            raise ValueError("daily_target must be between 1 and 100")
        self.settings = Settings(daily_target)
        return self.settings

    def create_session(
        self, session_id: str, kind: str, words: list[str], created_at: datetime
    ) -> StudySession:
        session = StudySession(
            session_id, kind, created_at, tuple(self.words[word] for word in words)
        )
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> StudySession | None:
        return self.sessions.get(session_id)

    def progress(self, now: datetime) -> dict[str, int]:
        return {
            "total": len(self.words),
            "due": len(self.due_words(now, 10_000)),
            "reviewed": len(self.events),
        }
