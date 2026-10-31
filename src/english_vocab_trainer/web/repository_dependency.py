from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository
from english_vocab_trainer.adapters.local.vocabulary import (
    AsyncSQLiteVocabularyRepository,
    SQLiteVocabularyRepository,
)
from english_vocab_trainer.ports.vocabulary import VocabularyRepository
from english_vocab_trainer.web.dependencies import get_container


async def repository(request: Request) -> AsyncIterator[VocabularyRepository]:
    env: Any = request.scope.get("env")
    db = env.get("DB") if isinstance(env, dict) else getattr(env, "DB", None)
    if db is not None:
        yield D1VocabularyRepository(db)
        return
    local = SQLiteVocabularyRepository(get_container(request).database)
    try:
        yield AsyncSQLiteVocabularyRepository(local)
    finally:
        local.close()
