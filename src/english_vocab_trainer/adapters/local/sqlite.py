from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from english_vocab_trainer.adapters.local.database import initialize_database
from english_vocab_trainer.domain.models import (
    Review,
    ReviewAction,
    Word,
    WordState,
    replay_word_state,
)
from english_vocab_trainer.ports.errors import MissingError, ReviewConflictError


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
                "INSERT INTO words(id,term,level,definition,audio_key) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET term=excluded.term,level=excluded.level,"
                "definition=excluded.definition,audio_key=excluded.audio_key",
                [
                    (
                        word.id,
                        word.term,
                        word.level,
                        word.definition,
                        word.audio_key,
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
            "SELECT 1 FROM reviews r WHERE r.word_id=w.id) "
            "ORDER BY w.level IS NULL,w.level,w.id LIMIT 1"
        ).fetchone()
        return self._word(unseen) if unseen is not None else None

    def next_due(self, now: datetime) -> datetime | None:
        row = self.db.execute(
            "SELECT MIN(due_at) FROM word_state WHERE due_at>?",
            (_iso(now),),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] is not None else None

    def _reviews(self, word_id: str) -> list[Review]:
        return [
            Review(
                UUID(row["id"]),
                word_id,
                ReviewAction(row["action"]),
                datetime.fromisoformat(row["reviewed_at"]),
            )
            for row in self.db.execute("SELECT * FROM reviews WHERE word_id=?", (word_id,))
        ]

    def get_review(self, review_id: UUID) -> Review | None:
        row = self.db.execute("SELECT * FROM reviews WHERE id=?", (str(review_id),)).fetchone()
        if row is None:
            return None
        return Review(
            review_id,
            row["word_id"],
            ReviewAction(row["action"]),
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

    def save_review(self, review: Review) -> WordState:
        with self._transaction():
            old = self.db.execute(
                "SELECT word_id,action FROM reviews WHERE id=?",
                (str(review.id),),
            ).fetchone()
            if old:
                if old["word_id"] != review.word_id:
                    raise ReviewConflictError("review UUID word conflict")
                if old["action"] == review.action.value:
                    return self.state(review.word_id)
                self.db.execute(
                    "UPDATE reviews SET action=? WHERE id=?",
                    (review.action.value, str(review.id)),
                )
            else:
                if self.get_word(review.word_id) is None:
                    raise MissingError("word not found")
                self.db.execute(
                    "INSERT INTO reviews VALUES(?,?,?,?)",
                    (
                        str(review.id),
                        review.word_id,
                        review.action.value,
                        _iso(review.reviewed_at),
                    ),
                )
            state = replay_word_state(review.word_id, self._reviews(review.word_id))
            self._save(state)
            return state

    def history_page(
        self, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[Review, Word]]:
        where = ""
        values: tuple[object, ...] = (limit,)
        if cursor is not None:
            reviewed_at, review_id = cursor
            where = "WHERE (r.reviewed_at<? OR (r.reviewed_at=? AND r.id<?)) "
            value = _iso(reviewed_at)
            values = (value, value, str(review_id), limit)
        rows = self.db.execute(
            "SELECT r.id review_id,r.word_id,r.action,r.reviewed_at,w.* "
            "FROM reviews r JOIN words w ON w.id=r.word_id "
            f"{where}ORDER BY r.reviewed_at DESC,r.id DESC LIMIT ?",
            values,
        )
        return [
            (
                Review(
                    UUID(row["review_id"]),
                    row["word_id"],
                    ReviewAction(row["action"]),
                    datetime.fromisoformat(row["reviewed_at"]),
                ),
                self._word(row),
            )
            for row in rows
        ]
