from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request

from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.web.container import AppContainer, ConfigurationError


def identity(
    request: Request,
    assertion: Annotated[str | None, Header(alias="Cf-Access-Jwt-Assertion")] = None,
) -> str:
    container = get_container(request)
    if container.environment in {"local", "test"}:
        if container.local_user_id is None:
            raise HTTPException(500, "local identity is not configured")
        return container.local_user_id
    if assertion is None:
        raise HTTPException(403, "Cloudflare Access authentication required")
    raise HTTPException(403, "Access verification is not configured")


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


def _binding(request: Request, name: str) -> object | None:
    environment = request.scope.get("env")
    if isinstance(environment, dict):
        return environment.get(name)
    return getattr(environment, name, None)


def repository_for_user(request: Request, user_id: str) -> Iterator[VocabularyRepository]:
    try:
        repository = cast(
            VocabularyRepository, get_container(request).repositories.for_user(user_id)
        )
    except ConfigurationError as exc:
        raise HTTPException(503, "repository is unavailable") from exc
    try:
        yield repository
    finally:
        repository.close()


def repository(
    request: Request, user_id: Annotated[str, Depends(identity)]
) -> Iterator[VocabularyRepository]:
    yield from repository_for_user(request, user_id)


def audio_store(request: Request) -> AudioStore:
    try:
        return get_container(request).audio
    except ConfigurationError as exc:
        raise HTTPException(503, "audio store is unavailable") from exc
