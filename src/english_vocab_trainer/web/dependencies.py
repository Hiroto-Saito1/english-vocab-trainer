from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from fastapi import HTTPException, Request

from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.web.container import AppContainer, ConfigurationError


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


def repository_for_user(request: Request, user_id: str) -> Iterator[VocabularyRepository]:
    try:
        repository = get_container(request).repositories.for_user(user_id)
    except ConfigurationError as exc:
        raise HTTPException(503, "repository is unavailable") from exc
    try:
        yield repository
    finally:
        repository.close()
