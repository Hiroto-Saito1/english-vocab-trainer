from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from english_vocab_trainer.domain.models import Word


def _record(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    native = value.to_py() if hasattr(value, "to_py") else value
    return cast(dict[str, object], native)


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

    async def close(self) -> None:
        return None
