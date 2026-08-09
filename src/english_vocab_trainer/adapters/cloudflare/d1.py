from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from english_vocab_trainer.domain.models import Rating, ReviewEvent, Settings, Word, WordState


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


class D1VocabularyRepository:
    """Async D1 boundary; all Worker FFI is isolated in this adapter."""

    def __init__(self, db: object, user_id: str) -> None:
        self.db, self.user_id = db, user_id

    async def _first(self, sql: str, *params: object) -> dict[str, object] | None:
        statement: Any = cast(Any, self.db).prepare(sql).bind(*params)
        return _record(await statement.first())

    async def _all(self, sql: str, *params: object) -> list[dict[str, object]]:
        statement: Any = cast(Any, self.db).prepare(sql).bind(*params)
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
        statement: Any = cast(Any, self.db).prepare(sql).bind(*params)
        return await statement.run()

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

    async def close(self) -> None:
        return None
