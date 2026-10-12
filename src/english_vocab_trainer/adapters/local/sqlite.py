from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
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
                "INSERT INTO words(id,term,level,definition,audio_key,tier) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET term=excluded.term,level=excluded.level,"
                "definition=excluded.definition,audio_key=excluded.audio_key,tier=excluded.tier",
                [
                    (
                        word.id,
                        word.term,
                        word.level,
                        word.definition,
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
            row["definition"],
            row["audio_key"],
            Tier(row["tier"]),
        )

    def next_word(self, now: datetime) -> Word | None:
        due = self.db.execute(
            "SELECT w.* FROM word_state s JOIN words w ON w.id=s.word_id "
            "WHERE s.due_at<=? ORDER BY s.due_at,w.id LIMIT 1",
            (_iso(now),),
        ).fetchone()
        if due is not None:
            return self._word(due)
        unseen = self.db.execute(
            "SELECT w.* FROM words w WHERE NOT EXISTS("
            "SELECT 1 FROM review_events e WHERE e.word_id=w.id) "
            "ORDER BY w.level IS NULL,w.level,w.id LIMIT 1"
        ).fetchone()
        return self._word(unseen) if unseen is not None else None

    def empty_card_status(
        self, now: datetime
    ) -> tuple[Literal["next_due", "catalog_empty"], datetime | None]:
        if self.db.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 0:
            return "catalog_empty", None
        row = self.db.execute(
            "SELECT MIN(due_at) FROM word_state WHERE due_at>?",
            (_iso(now),),
        ).fetchone()
        if row[0] is None:
            raise RuntimeError("an empty card with available words must have a future due date")
        return "next_due", datetime.fromisoformat(row[0])

    def _events(self, word_id: str) -> list[ReviewEvent]:
        return [
            ReviewEvent(
                UUID(row["id"]),
                word_id,
                Rating(row["rating"]),
                datetime.fromisoformat(row["reviewed_at"]),
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
        )

    def state(self, word_id: str) -> WordState:
        row = self.db.execute("SELECT * FROM word_state WHERE word_id=?", (word_id,)).fetchone()
        if row is None:
            return WordState(word_id, datetime.min.replace(tzinfo=UTC))
        return WordState(word_id, datetime.fromisoformat(row["due_at"]))

    def _save(self, state: WordState) -> None:
        self.db.execute(
            "INSERT INTO word_state VALUES(?,?) "
            "ON CONFLICT(word_id) DO UPDATE SET due_at=excluded.due_at",
            (state.word_id, _iso(state.due_at)),
        )

    def save_event(
        self, event: ReviewEvent
    ) -> tuple[WordState, Literal["applied", "idempotent", "updated"]]:
        with self._transaction():
            old = self.db.execute(
                "SELECT word_id,rating,reviewed_at FROM review_events WHERE id=?",
                (str(event.id),),
            ).fetchone()
            reviewed_at = _iso(event.reviewed_at)
            if old:
                if old["word_id"] != event.word_id or old["reviewed_at"] != reviewed_at:
                    raise EventConflictError("event UUID payload conflict")
                if old["rating"] == event.rating.value:
                    return self.state(event.word_id), "idempotent"
                self.db.execute(
                    "UPDATE review_events SET rating=? WHERE id=?",
                    (event.rating.value, str(event.id)),
                )
                status: Literal["applied", "idempotent", "updated"] = "updated"
            else:
                if self.get_word(event.word_id) is None:
                    raise MissingError("word not found")
                self.db.execute(
                    "INSERT INTO review_events VALUES(?,?,?,?)",
                    (
                        str(event.id),
                        event.word_id,
                        event.rating.value,
                        reviewed_at,
                    ),
                )
                status = "applied"
            state = replay_word_state(event.word_id, self._events(event.word_id))
            self._save(state)
            return state, status

    def history_page(
        self, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[ReviewEvent, Word]]:
        where = ""
        values: tuple[object, ...] = (limit,)
        if cursor is not None:
            reviewed_at, event_id = cursor
            where = "WHERE (e.reviewed_at<? OR (e.reviewed_at=? AND e.id<?)) "
            value = _iso(reviewed_at)
            values = (value, value, str(event_id), limit)
        rows = self.db.execute(
            "SELECT e.id event_id,e.word_id,e.rating,e.reviewed_at,w.* "
            "FROM review_events e JOIN words w ON w.id=e.word_id "
            f"{where}ORDER BY e.reviewed_at DESC,e.id DESC LIMIT ?",
            values,
        )
        return [
            (
                ReviewEvent(
                    UUID(row["event_id"]),
                    row["word_id"],
                    Rating(row["rating"]),
                    datetime.fromisoformat(row["reviewed_at"]),
                ),
                self._word(row),
            )
            for row in rows
        ]
