from __future__ import annotations

from datetime import UTC, datetime

from english_vocab_trainer.domain.models import ReviewEvent, Word, WordState, next_state


class InMemoryVocabularyRepository:
    def __init__(self, words: list[Word] | None = None) -> None:
        self.words = {word.id: word for word in words or []}
        self.states: dict[str, WordState] = {}
        self.events: dict[str, ReviewEvent] = {}

    def list_words(self, *, levels: list[int] | None = None, limit: int = 100) -> list[Word]:
        selected = [w for w in self.words.values() if levels is None or w.level in levels]
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

    def append_event(self, event: ReviewEvent, expected_version: int) -> WordState:
        if str(event.id) in self.events:
            return self.state(event.word_id)
        state = self.state(event.word_id)
        if state.version != expected_version:
            raise RuntimeError("CAS conflict")
        self.events[str(event.id)] = event
        return next_state(state, event)

    def progress(self, now: datetime) -> dict[str, int]:
        return {
            "total": len(self.words),
            "due": len(self.due_words(now, 10_000)),
            "reviewed": len(self.events),
        }
