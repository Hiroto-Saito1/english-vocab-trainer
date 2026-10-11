from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from english_vocab_trainer.adapters.local.database import initialize_database
from english_vocab_trainer.domain.models import (
    Rating,
    ReviewEvent,
    Tier,
    Word,
    WordState,
    replay_word_state,
)
from english_vocab_trainer.ports.errors import EventConflictError, MissingError


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


class SQLiteVocabularyRepository:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            initialize_database(self.db)
        except Exception:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> SQLiteVocabularyRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def bulk_upsert_words(self, words: list[Word]) -> None:
        """Atomically publish a fully validated catalog without touching review history."""
        with self._transaction():
            self.db.executemany(
                "INSERT INTO words(id,term,level,transcript,audio_key,tier,excluded) "
                "VALUES(?,?,?,?,?,?,0) "
                "ON CONFLICT(id) DO UPDATE SET term=excluded.term,level=excluded.level,"
                "transcript=excluded.transcript,audio_key=excluded.audio_key,tier=excluded.tier",
                [
                    (
                        word.id,
                        word.term,
                        word.level,
                        word.transcript,
                        word.audio_key,
                        word.tier.value,
                    )
                    for word in words
                ],
            )

    def get_word(self, word_id: str) -> Word | None:
        row = self.db.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
        return self._word(row) if row else None

    @staticmethod
    def _word(row: sqlite3.Row) -> Word:
        return Word(
            row["id"],
            row["term"],
            row["level"],
            row["transcript"],
            row["audio_key"],
            Tier(row["tier"]),
        )

    def list_words(self, *, limit: int = 100) -> list[Word]:
        query = (
            "SELECT w.* FROM words w WHERE w.excluded=0 AND NOT EXISTS("
            "SELECT 1 FROM review_events e "
            "WHERE e.word_id=w.id AND e.voided_at IS NULL) "
            "ORDER BY w.level IS NULL,w.level,w.id LIMIT ?"
        )
        return [self._word(row) for row in self.db.execute(query, (limit,))]

    def due_words(self, now: datetime, limit: int) -> list[Word]:
        query = (
            "SELECT w.* FROM word_state s JOIN words w ON w.id=s.word_id "
            "WHERE w.excluded=0 AND s.due_at<=? ORDER BY s.due_at LIMIT ?"
        )
        return [self._word(row) for row in self.db.execute(query, (_iso(now), limit))]

    def _events(self, word_id: str) -> list[ReviewEvent]:
        return [
            ReviewEvent(
                UUID(row["id"]),
                word_id,
                Rating(row["rating"]),
                datetime.fromisoformat(row["reviewed_at"]),
                _dt(row["voided_at"]),
            )
            for row in self.db.execute("SELECT * FROM review_events WHERE word_id=?", (word_id,))
        ]

    def get_event(self, event_id: UUID) -> ReviewEvent | None:
        row = self.db.execute("SELECT * FROM review_events WHERE id=?", (str(event_id),)).fetchone()
        if row is None:
            return None
        return ReviewEvent(
            event_id,
            row["word_id"],
            Rating(row["rating"]),
            datetime.fromisoformat(row["reviewed_at"]),
            _dt(row["voided_at"]),
        )

    def state(self, word_id: str) -> WordState:
        row = self.db.execute("SELECT * FROM word_state WHERE word_id=?", (word_id,)).fetchone()
        if row is None:
            return WordState(word_id, datetime.min.replace(tzinfo=UTC))
        return WordState(word_id, datetime.fromisoformat(row["due_at"]))

    def list_excluded_words(self) -> list[Word]:
        return [
            self._word(row)
            for row in self.db.execute(
                "SELECT * FROM words WHERE excluded=1 ORDER BY level IS NULL,level,term,id"
            )
        ]

    def set_excluded(self, word_id: str, excluded: bool) -> None:
        with self._transaction():
            cursor = self.db.execute(
                "UPDATE words SET excluded=? WHERE id=?", (int(excluded), word_id)
            )
            if cursor.rowcount != 1:
                raise MissingError("word not found")

    def _save(self, state: WordState) -> None:
        self.db.execute(
            "INSERT INTO word_state VALUES(?,?) "
            "ON CONFLICT(word_id) DO UPDATE SET due_at=excluded.due_at",
            (state.word_id, _iso(state.due_at)),
        )

    def append_event(self, event: ReviewEvent) -> WordState:
        with self._transaction():
            old = self.db.execute(
                "SELECT word_id,rating,reviewed_at FROM review_events WHERE id=?",
                (str(event.id),),
            ).fetchone()
            reviewed_at = _iso(event.reviewed_at)
            if old:
                if (
                    old["word_id"] != event.word_id
                    or old["rating"] != event.rating.value
                    or old["reviewed_at"] != reviewed_at
                ):
                    raise EventConflictError("event UUID payload conflict")
                return self.state(event.word_id)
            if self.get_word(event.word_id) is None:
                raise MissingError("word not found")
            self.db.execute(
                "INSERT INTO review_events VALUES(?,?,?,?,?)",
                (
                    str(event.id),
                    event.word_id,
                    event.rating.value,
                    reviewed_at,
                    None,
                ),
            )
            state = replay_word_state(event.word_id, self._events(event.word_id))
            self._save(state)
            return state

    def void_event(self, event_id: UUID) -> WordState:
        with self._transaction():
            row = self.db.execute(
                "SELECT word_id,voided_at FROM review_events WHERE id=?", (str(event_id),)
            ).fetchone()
            if not row:
                raise MissingError("event not found")
            word_id = row["word_id"]
            if row["voided_at"] is not None:
                return self.state(word_id)
            self.db.execute(
                "UPDATE review_events SET voided_at=? WHERE id=?",
                (_iso(datetime.now(UTC)), str(event_id)),
            )
            state = replay_word_state(word_id, self._events(word_id))
            self._save(state)
            return state
