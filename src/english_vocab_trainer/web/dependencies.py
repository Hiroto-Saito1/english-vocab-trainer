from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request

from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.web.auth import AuthenticationError, csrf_hash
from english_vocab_trainer.web.container import AppContainer, ConfigurationError


def identity(
    request: Request,
) -> str:
    container = get_container(request)
    if container.environment in {"local", "test"}:
        if container.local_user_id is None:
            raise HTTPException(500, "local identity is not configured")
        return container.local_user_id
    if container.auth is None:
        raise HTTPException(503, "authentication is unavailable")
    try:
        return container.auth.user_from_session(
            request.cookies.get(container.auth.session_cookie_name)
        )
    except AuthenticationError as exc:
        raise HTTPException(401, "authentication required") from exc


def csrf(
    request: Request,
    token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    container = get_container(request)
    if container.environment in {"local", "test"}:
        return
    if container.auth is None:
        raise HTTPException(503, "authentication is unavailable")
    cookie = request.cookies.get(container.auth.csrf_cookie_name)
    try:
        csrf_hash(token)
        csrf_hash(cookie)
    except AuthenticationError as exc:
        raise HTTPException(403, "csrf validation failed") from exc
    if token is None or cookie is None or not hmac.compare_digest(token, cookie):
        raise HTTPException(403, "csrf validation failed")
    try:
        container.auth.user_from_session(
            request.cookies.get(container.auth.session_cookie_name), cookie, require_csrf=True
        )
    except AuthenticationError as exc:
        raise HTTPException(403, "csrf validation failed") from exc


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


def repository(
    request: Request, user_id: Annotated[str, Depends(identity)]
) -> Iterator[VocabularyRepository]:
    try:
        user_repository = get_container(request).repositories.for_user(user_id)
    except ConfigurationError as exc:
        raise HTTPException(503, "repository is unavailable") from exc
    try:
        yield user_repository
    finally:
        user_repository.close()


def audio_store(request: Request) -> AudioStore:
    return get_container(request).audio
