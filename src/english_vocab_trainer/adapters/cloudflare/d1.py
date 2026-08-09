from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from english_vocab_trainer.domain.models import (
    Rating,
    ReviewEvent,
    Settings,
    StudySession,
    Word,
    WordState,
)
from english_vocab_trainer.ports.errors import MissingError


def _record(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    native = value.to_py() if hasattr(value, "to_py") else value
    return cast(dict[str, object], native)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _records(value: object) -> list[dict[str, object]]:
    native = value.to_py() if hasattr(value, "to_py") else value
    rows = cast(list[object], native)
    return [cast(dict[str, object], row.to_py() if hasattr(row, "to_py") else row) for row in rows]


def _changes(result: object) -> int:
    native = result.to_py() if hasattr(result, "to_py") else result
    if isinstance(native, dict):
        meta = cast(dict[str, object], native.get("meta", {}))
        return int(cast(int | str, meta.get("changes", 0)))
    return int(cast(Any, native).meta.changes)


def _event_payload(event: ReviewEvent) -> str:
    return json.dumps([event.word_id, event.rating, event.reviewed_at.isoformat()])


class D1VocabularyRepository:
    """Async D1 boundary; all Worker FFI is isolated in this adapter."""

    def __init__(self, db: object, user_id: str) -> None:
        self.db, self.user_id = db, user_id

    def _prepare(self, sql: str, *params: object) -> object:
        return cast(Any, self.db).prepare(sql).bind(*params)

    def _state_upsert(self, state: WordState, expected_version: int) -> object:
        sql = """
            INSERT INTO user_word_state(
                user_id,word_id,due_at,stability,difficulty,card_json,
                first_seen_at,first_known_at,last_known_at,version
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,word_id) DO UPDATE SET
                due_at=excluded.due_at,
                stability=excluded.stability,
                difficulty=excluded.difficulty,
                card_json=excluded.card_json,
                first_seen_at=excluded.first_seen_at,
                first_known_at=excluded.first_known_at,
                last_known_at=excluded.last_known_at,
                version=excluded.version
            WHERE user_word_state.version=?
        """
        return self._prepare(
            sql,
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
            expected_version,
        )

    async def _first(self, sql: str, *params: object) -> dict[str, object] | None:
        statement: Any = self._prepare(sql, *params)
        return _record(await statement.first())

    async def _all(self, sql: str, *params: object) -> list[dict[str, object]]:
        statement: Any = self._prepare(sql, *params)
        result: object = await statement.run()
        rows = (
            result.get("results", [])
            if isinstance(result, dict)
            else getattr(result, "results", [])
        )
        return _records(rows)

    async def _count(self, sql: str, *params: object) -> int:
        row = await self._first(sql, *params)
        return int(cast(int | str, row["value"])) if row else 0

    async def _run(self, sql: str, *params: object) -> object:
        statement: Any = self._prepare(sql, *params)
        return await statement.run()

    async def _batch(self, statements: list[object]) -> list[object]:
        result: object = await cast(Any, self.db).batch(statements)
        native = result.to_py() if hasattr(result, "to_py") else result
        return cast(list[object], native)

    async def get_word(self, word_id: str) -> Word | None:
        row = await self._first(
            "SELECT id,term,level,transcript,audio_key FROM words WHERE id=?", word_id
        )
        if row is None:
            return None
        return Word(
            str(row["id"]),
            str(row["term"]),
            cast(int | None, row["level"]),
            cast(str | None, row["transcript"]),
            str(row["audio_key"]),
        )

    async def list_words(self, *, levels: list[int] | None = None, limit: int = 100) -> list[Word]:
        query = (
            "SELECT w.* FROM words w WHERE NOT EXISTS(SELECT 1 FROM review_events e "
            "WHERE e.user_id=? AND e.word_id=w.id AND e.voided_at IS NULL)"
        )
        args: list[object] = [self.user_id]
        if levels is not None:
            query += " AND w.level IN (" + ",".join("?" for _ in levels) + ")"
            args.extend(levels)
        query += " ORDER BY w.level IS NULL,w.level,w.id LIMIT ?"
        args.append(limit)
        rows = await self._all(query, *args)

        return [
            Word(
                str(row["id"]),
                str(row["term"]),
                cast(int | None, row["level"]),
                cast(str | None, row["transcript"]),
                str(row["audio_key"]),
            )
            for row in rows
        ]

    async def due_words(self, now: datetime, limit: int) -> list[Word]:
        rows = await self._all(
            "SELECT w.* FROM user_word_state s JOIN words w ON w.id=s.word_id "
            "WHERE s.user_id=? AND s.due_at<=? ORDER BY s.due_at LIMIT ?",
            self.user_id,
            now.isoformat(),
            limit,
        )
        return [
            Word(
                str(row["id"]),
                str(row["term"]),
                cast(int | None, row["level"]),
                cast(str | None, row["transcript"]),
                str(row["audio_key"]),
            )
            for row in rows
        ]

    async def state(self, word_id: str) -> WordState:
        row = await self._first(
            "SELECT * FROM user_word_state WHERE user_id=? AND word_id=?", self.user_id, word_id
        )
        if row is None:
            return WordState(word_id, datetime.min.replace(tzinfo=UTC))
        return WordState(
            word_id,
            _dt(cast(str | None, row["due_at"])) or datetime.min.replace(tzinfo=UTC),
            float(cast(float | str, row["stability"])),
            float(cast(float | str, row["difficulty"])),
            _dt(cast(str | None, row["first_seen_at"])),
            _dt(cast(str | None, row["first_known_at"])),
            _dt(cast(str | None, row["last_known_at"])),
            int(cast(int | str, row["version"])),
            cast(str | None, row["card_json"]),
        )

    async def has_active_review(self, word_id: str) -> bool:
        row = await self._first(
            "SELECT EXISTS(SELECT 1 FROM review_events WHERE user_id=? AND word_id=? "
            "AND voided_at IS NULL) AS active",
            self.user_id,
            word_id,
        )
        return bool(int(cast(int | str, row["active"]))) if row else False

    async def get_event(self, event_id: UUID) -> ReviewEvent | None:
        row = await self._first(
            "SELECT * FROM review_events WHERE id=? AND user_id=?", str(event_id), self.user_id
        )
        if row is None:
            return None
        reviewed = _dt(cast(str | None, row["reviewed_at"]))
        assert reviewed is not None
        return ReviewEvent(
            event_id,
            str(row["word_id"]),
            Rating(str(row["rating"])),
            reviewed,
            _dt(cast(str | None, row["voided_at"])),
        )

    async def _events(self, word_id: str) -> list[ReviewEvent]:
        rows = await self._all(
            "SELECT * FROM review_events WHERE user_id=? AND word_id=? ORDER BY reviewed_at,id",
            self.user_id,
            word_id,
        )
        events: list[ReviewEvent] = []
        for row in rows:
            reviewed = _dt(cast(str | None, row["reviewed_at"]))
            assert reviewed is not None
            events.append(
                ReviewEvent(
                    UUID(str(row["id"])),
                    word_id,
                    Rating(str(row["rating"])),
                    reviewed,
                    _dt(cast(str | None, row["voided_at"])),
                )
            )
        return events

    async def progress(self, now: datetime) -> dict[str, int]:
        total = await self._count("SELECT count(*) AS value FROM words")
        due = await self._count(
            "SELECT count(*) AS value FROM user_word_state WHERE user_id=? AND due_at<=?",
            self.user_id,
            _iso(now),
        )
        reviewed = await self._count(
            "SELECT count(*) AS value FROM review_events WHERE user_id=? AND voided_at IS NULL",
            self.user_id,
        )
        return {"total": total, "due": due, "reviewed": reviewed}

    async def get_settings(self) -> Settings:
        row = await self._first(
            "SELECT daily_target FROM user_settings WHERE user_id=?", self.user_id
        )
        return Settings(int(cast(int | str, row["daily_target"]))) if row else Settings()

    async def update_settings(self, daily_target: int) -> Settings:
        if not 1 <= daily_target <= 100:
            raise ValueError("daily_target must be between 1 and 100")
        await self._run(
            "INSERT INTO user_settings(user_id,daily_target) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET daily_target=excluded.daily_target",
            self.user_id,
            daily_target,
        )
        return Settings(daily_target)

    async def update_transcript(self, word_id: str, transcript: str) -> Word:
        word = await self.get_word(word_id)
        if word is None:
            raise MissingError("word not found")
        await self._run("UPDATE words SET transcript=? WHERE id=?", transcript, word_id)
        return Word(word.id, word.term, word.level, transcript, word.audio_key)

    async def get_session(self, session_id: str) -> StudySession | None:
        meta = await self._first(
            "SELECT kind,created_at FROM study_sessions WHERE id=? AND user_id=?",
            session_id,
            self.user_id,
        )
        if meta is None:
            return None
        rows = await self._all(
            "SELECT w.* FROM session_items i JOIN words w ON w.id=i.word_id "
            "WHERE i.session_id=? ORDER BY i.ordinal",
            session_id,
        )
        words = tuple(
            Word(
                str(row["id"]),
                str(row["term"]),
                cast(int | None, row["level"]),
                cast(str | None, row["transcript"]),
                str(row["audio_key"]),
            )
            for row in rows
        )
        created = _dt(cast(str | None, meta["created_at"]))
        assert created is not None
        return StudySession(session_id, str(meta["kind"]), created, words)

    async def create_session(
        self, session_id: str, kind: str, words: list[str], created_at: datetime
    ) -> StudySession:
        db: Any = cast(Any, self.db)
        statements = [
            db.prepare(
                "INSERT INTO study_sessions(id,user_id,kind,created_at) VALUES(?,?,?,?)"
            ).bind(session_id, self.user_id, kind, _iso(created_at))
        ]
        statements.extend(
            db.prepare("INSERT INTO session_items(session_id,word_id,ordinal) VALUES(?,?,?)").bind(
                session_id, word, index
            )
            for index, word in enumerate(words)
        )
        await db.batch(statements)
        session = await self.get_session(session_id)
        assert session is not None
        return session

    async def close(self) -> None:
        return None
