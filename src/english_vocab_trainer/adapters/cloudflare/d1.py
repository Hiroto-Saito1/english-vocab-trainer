from __future__ import annotations

from typing import Any, cast

from english_vocab_trainer.domain.models import Word


def _record(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    native = value.to_py() if hasattr(value, "to_py") else value
    return cast(dict[str, object], native)


class D1VocabularyRepository:
    """Async D1 boundary; all Worker FFI is isolated in this adapter."""

    def __init__(self, db: object, user_id: str) -> None:
        self.db, self.user_id = db, user_id

    async def _first(self, sql: str, *params: object) -> dict[str, object] | None:
        statement: Any = cast(Any, self.db).prepare(sql).bind(*params)
        return _record(await statement.first())

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

    async def close(self) -> None:
        return None
