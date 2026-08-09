from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from english_vocab_trainer.adapters.local.migrations import apply_migrations
from english_vocab_trainer.domain.models import (
    Rating,
    ReviewEvent,
    Settings,
    StudySession,
    Word,
    WordState,
    replay_word_state,
)

# SQL DDL and parameterized statements are intentionally kept verbatim so that
# the local adapter can be audited against the D1 migration.
from english_vocab_trainer.ports.errors import ConcurrentUpdateError, EventConflictError
from english_vocab_trainer.ports.errors import MissingError as PortMissingError

ConflictError = EventConflictError
MissingError = PortMissingError


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


class SQLiteVocabularyRepository:
    def __init__(self, path: Path, user_id: str) -> None:
        self.user_id, self.db = (
            user_id,
            sqlite3.connect(path, isolation_level=None, check_same_thread=False),
        )
        self.db.row_factory = sqlite3.Row
        apply_migrations(self.db, Path(__file__).parents[4] / "migrations")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> SQLiteVocabularyRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def for_user(self, user_id: str) -> SQLiteVocabularyRepository:
        return SQLiteVocabularyRepository(
            Path(self.db.execute("PRAGMA database_list").fetchone()[2]), user_id
        )

    def add_word(self, word: Word) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO words VALUES(?,?,?,?,?)",
            (word.id, word.term, word.level, word.transcript, word.audio_key),
        )

    def get_word(self, word_id: str) -> Word | None:
        r = self.db.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
        return Word(r["id"], r["term"], r["level"], r["transcript"], r["audio_key"]) if r else None

    def list_words(self, *, levels: list[int] | None = None, limit: int = 100) -> list[Word]:
        q = "SELECT w.* FROM words w WHERE NOT EXISTS(SELECT 1 FROM review_events e WHERE e.user_id=? AND e.word_id=w.id AND e.voided_at IS NULL)"
        args: list[object] = [self.user_id]
        if levels is not None:
            q += " AND w.level IN (" + ",".join("?" for _ in levels) + ")"
            args.extend(levels)
        q += " ORDER BY w.level IS NULL,w.level,w.id LIMIT ?"
        args.append(limit)
        return [
            Word(r["id"], r["term"], r["level"], r["transcript"], r["audio_key"])
            for r in self.db.execute(q, args)
        ]

    def due_words(self, now: datetime, limit: int) -> list[Word]:
        q = "SELECT w.* FROM user_word_state s JOIN words w ON w.id=s.word_id WHERE s.user_id=? AND s.due_at<=? ORDER BY s.due_at LIMIT ?"
        return [
            Word(r["id"], r["term"], r["level"], r["transcript"], r["audio_key"])
            for r in self.db.execute(q, (self.user_id, _iso(now), limit))
        ]

    def _events(self, word_id: str) -> list[ReviewEvent]:
        return [
            ReviewEvent(
                UUID(r["id"]),
                word_id,
                Rating(r["rating"]),
                _dt(r["reviewed_at"]) or datetime.now(UTC),
                _dt(r["voided_at"]),
            )
            for r in self.db.execute(
                "SELECT * FROM review_events WHERE user_id=? AND word_id=?", (self.user_id, word_id)
            )
        ]

    def get_event(self, event_id: UUID) -> ReviewEvent | None:
        row = self.db.execute(
            "SELECT * FROM review_events WHERE id=? AND user_id=?", (str(event_id), self.user_id)
        ).fetchone()
        if row is None:
            return None
        reviewed = _dt(row["reviewed_at"])
        assert reviewed is not None
        return ReviewEvent(
            event_id, row["word_id"], Rating(row["rating"]), reviewed, _dt(row["voided_at"])
        )

    def state(self, word_id: str) -> WordState:
        r = self.db.execute(
            "SELECT * FROM user_word_state WHERE user_id=? AND word_id=?", (self.user_id, word_id)
        ).fetchone()
        return (
            WordState(
                word_id,
                _dt(r["due_at"]) or datetime.min.replace(tzinfo=UTC),
                r["stability"],
                r["difficulty"],
                _dt(r["first_seen_at"]),
                _dt(r["first_known_at"]),
                _dt(r["last_known_at"]),
                r["version"],
                r["card_json"],
            )
            if r
            else WordState(word_id, datetime.min.replace(tzinfo=UTC))
        )

    def has_active_review(self, word_id: str) -> bool:
        row = self.db.execute(
            "SELECT EXISTS(SELECT 1 FROM review_events WHERE user_id=? AND word_id=? AND voided_at IS NULL)",
            (self.user_id, word_id),
        ).fetchone()
        return bool(int(row[0]))

    def _save(self, state: WordState) -> None:
        self.db.execute(
            "INSERT INTO user_word_state VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,word_id) DO UPDATE SET due_at=excluded.due_at,stability=excluded.stability,difficulty=excluded.difficulty,card_json=excluded.card_json,first_seen_at=excluded.first_seen_at,first_known_at=excluded.first_known_at,last_known_at=excluded.last_known_at,version=excluded.version",
            (
                self.user_id,
                state.word_id,
                _iso(state.due_at),
                state.stability,
                state.difficulty,
                state.card_json,
                _iso(state.first_seen_at),
                _iso(state.first_known_at),
                _iso(state.last_known_at),
                state.version,
            ),
        )

    def append_event(self, event: ReviewEvent, expected_version: int) -> WordState:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute(
                "SELECT user_id,payload FROM review_events WHERE id=?", (str(event.id),)
            ).fetchone()
            payload = json.dumps([event.word_id, event.rating, event.reviewed_at.isoformat()])
            if old:
                if old["user_id"] != self.user_id or old["payload"] != payload:
                    raise EventConflictError("event UUID payload conflict")
                self.db.execute("COMMIT")
                return self.state(event.word_id)
            if self.get_word(event.word_id) is None:
                raise MissingError("word not found")
            state = self.state(event.word_id)
            if state.version != expected_version:
                raise ConcurrentUpdateError("CAS conflict")
            self.db.execute(
                "INSERT INTO review_events VALUES(?,?,?,?,?,?,?)",
                (
                    str(event.id),
                    self.user_id,
                    event.word_id,
                    event.rating,
                    _iso(event.reviewed_at),
                    None,
                    payload,
                ),
            )
            state = replay_word_state(event.word_id, self._events(event.word_id), state.version + 1)
            self._save(state)
            self.db.execute("COMMIT")
            return state
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def void_event(self, event_id: UUID) -> WordState:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            r = self.db.execute(
                "SELECT word_id,voided_at FROM review_events WHERE id=? AND user_id=?",
                (str(event_id), self.user_id),
            ).fetchone()
            if not r:
                raise MissingError("event not found")
            if r["voided_at"] is not None:
                self.db.execute("COMMIT")
                return self.state(r["word_id"])
            self.db.execute(
                "UPDATE review_events SET voided_at=? WHERE id=?",
                (_iso(datetime.now(UTC)), str(event_id)),
            )
            prior = self.state(r[0])
            state = replay_word_state(r[0], self._events(r[0]), prior.version + 1)
            self._save(state)
            self.db.execute("COMMIT")
            return state
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def update_transcript(self, word_id: str, transcript: str) -> Word:
        if (
            self.db.execute(
                "UPDATE words SET transcript=? WHERE id=?", (transcript, word_id)
            ).rowcount
            != 1
        ):
            raise MissingError("word not found")
        word = self.get_word(word_id)
        assert word is not None
        return word

    def progress(self, now: datetime) -> dict[str, int]:
        return {
            "total": self.db.execute("SELECT count(*) FROM words").fetchone()[0],
            "due": len(self.due_words(now, 100000)),
            "reviewed": self.db.execute(
                "SELECT count(*) FROM review_events WHERE user_id=? AND voided_at IS NULL",
                (self.user_id,),
            ).fetchone()[0],
        }

    def get_settings(self) -> Settings:
        row = self.db.execute(
            "SELECT daily_target FROM user_settings WHERE user_id=?", (self.user_id,)
        ).fetchone()
        return Settings(row[0] if row else 30)

    def update_settings(self, daily_target: int) -> Settings:
        if not 1 <= daily_target <= 100:
            raise ValueError("daily_target must be between 1 and 100")
        self.db.execute(
            "INSERT INTO user_settings VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET daily_target=excluded.daily_target",
            (self.user_id, daily_target),
        )
        return self.get_settings()

    def create_session(
        self, session_id: str, kind: str, words: list[str], created_at: datetime
    ) -> StudySession:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO study_sessions VALUES(?,?,?,?)",
                (session_id, self.user_id, kind, _iso(created_at)),
            )
            self.db.executemany(
                "INSERT INTO session_items VALUES(?,?,?)",
                [(session_id, word, index) for index, word in enumerate(words)],
            )
            self.db.execute("COMMIT")
            session = self.get_session(session_id)
            assert session is not None
            return session
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def get_session(self, session_id: str) -> StudySession | None:
        q = "SELECT w.* FROM study_sessions s JOIN session_items i ON i.session_id=s.id JOIN words w ON w.id=i.word_id WHERE s.id=? AND s.user_id=? ORDER BY i.ordinal"
        rows = list(self.db.execute(q, (session_id, self.user_id)))
        if not rows:
            return None
        words = tuple(
            Word(r["id"], r["term"], r["level"], r["transcript"], r["audio_key"]) for r in rows
        )
        meta = self.db.execute(
            "SELECT kind,created_at FROM study_sessions WHERE id=? AND user_id=?",
            (session_id, self.user_id),
        ).fetchone()
        assert meta is not None
        return StudySession(
            session_id,
            meta["kind"],
            _dt(meta["created_at"]) or datetime.min.replace(tzinfo=UTC),
            words,
        )
