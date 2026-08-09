from __future__ import annotations

import pytest

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository


class Statement:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.params: tuple[object, ...] = ()

    def bind(self, *params: object) -> Statement:
        self.params = params
        return self

    async def first(self) -> dict[str, object] | None:
        return self.row


class D1:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.sql = ""
        self.statement = Statement(row)

    def prepare(self, sql: str) -> Statement:
        self.sql = sql
        return self.statement


@pytest.mark.asyncio
async def test_d1_get_word_found_and_missing() -> None:
    db = D1({"id": "w", "term": "word", "level": 9, "transcript": None, "audio_key": "w.mp3"})
    result = await D1VocabularyRepository(db, "u").get_word("w")
    assert result is not None and result.term == "word"
    assert (
        db.statement.params == ("w",)
        and await D1VocabularyRepository(D1(None), "u").get_word("x") is None
    )
