from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Header, HTTPException, Request

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.web.auth import AuthenticationError
from english_vocab_trainer.web.container import AppContainer


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
    if token is None or cookie is None:
        raise HTTPException(403, "csrf validation failed")
    try:
        container.auth.validate_session(
            request.cookies.get(container.auth.session_cookie_name), token, require_csrf=True
        )
    except AuthenticationError as exc:
        raise HTTPException(403, "csrf validation failed") from exc
    if not hmac.compare_digest(token.encode("utf-8"), cookie.encode("utf-8")):
        raise HTTPException(403, "csrf validation failed")


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


def repository(request: Request) -> Iterator[SQLiteVocabularyRepository]:
    container = get_container(request)
    if container.environment not in {"local", "test"}:
        if container.auth is None:
            raise HTTPException(503, "authentication is unavailable")
        try:
            container.auth.validate_session(request.cookies.get(container.auth.session_cookie_name))
        except AuthenticationError as exc:
            raise HTTPException(401, "authentication required") from exc
    try:
        user_repository = SQLiteVocabularyRepository(container.database)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "repository is unavailable") from exc
    try:
        yield user_repository
    finally:
        user_repository.close()


def audio_store(request: Request) -> AudioStore:
    return get_container(request).audio
