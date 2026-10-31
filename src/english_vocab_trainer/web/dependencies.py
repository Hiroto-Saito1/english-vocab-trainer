from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.adapters.worker_r2 import WorkerR2AudioStore
from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.web.container import AppContainer


def csrf(
    request: Request,
    token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Worker mutations are checked by middleware; local tests are trusted."""
    if getattr(request.scope.get("env"), "DB", None) is not None:
        return
    if get_container(request).environment not in {"local", "test"}:
        raise HTTPException(503, "Cloudflare authentication is required")


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


def repository(request: Request) -> Iterator[SQLiteVocabularyRepository]:
    try:
        user_repository = SQLiteVocabularyRepository(get_container(request).database)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "repository is unavailable") from exc
    try:
        yield user_repository
    finally:
        user_repository.close()


def audio_store(request: Request) -> AudioStore:
    binding = getattr(request.scope.get("env"), "AUDIO", None)
    if binding is not None:
        return WorkerR2AudioStore(binding)
    return get_container(request).audio
