from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request

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


def repository_for_user(request: Request, user_id: str) -> Iterator[VocabularyRepository]:
    try:
        repository = get_container(request).repositories.for_user(user_id)
    except ConfigurationError as exc:
        raise HTTPException(503, "repository is unavailable") from exc
    try:
        yield repository
    finally:
        repository.close()
