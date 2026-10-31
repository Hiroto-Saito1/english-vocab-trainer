from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import Request

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository
from english_vocab_trainer.adapters.local.vocabulary import (
    AsyncSQLiteVocabularyRepository,
    SQLiteVocabularyRepository,
)
from english_vocab_trainer.adapters.worker_r2 import WorkerR2AudioStore
from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.ports.vocabulary import VocabularyRepository
from english_vocab_trainer.web.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


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


def audio_store(request: Request) -> AudioStore:
    binding = getattr(request.scope.get("env"), "AUDIO", None)
    if binding is not None:
        return WorkerR2AudioStore(binding)
    return get_container(request).audio
