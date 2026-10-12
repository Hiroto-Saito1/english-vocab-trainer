from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from english_vocab_trainer.adapters.local.database import SchemaMismatchError
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.application.services import select_set_words, submit_review
from english_vocab_trainer.domain.models import (
    LEARNING_STEP,
    ReviewAction,
    Tier,
    Word,
)
from english_vocab_trainer.ports.audio import (
    AudioMetadata,
    AudioStorageError,
    AudioStore,
    InvalidRangeError,
    parse_single_range,
)
from english_vocab_trainer.ports.errors import EventConflictError, MissingError
from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.web.audio import (
    build_audio_head_response,
    build_audio_response,
    if_none_match_matches,
)
from english_vocab_trainer.web.auth import AuthenticationError
from english_vocab_trainer.web.container import (
    AppContainer,
    ConfigurationError,
    audio_store_from_env,
    auth_from_env,
    trusted_hosts_from_env,
)
from english_vocab_trainer.web.dependencies import audio_store, csrf
from english_vocab_trainer.web.dependencies import repository as repository_dependency

Csrf = Annotated[None, Depends(csrf)]
Repository = Annotated[VocabularyRepository, Depends(repository_dependency)]
Audio = Annotated[AudioStore, Depends(audio_store)]
RangeHeader = Annotated[str | None, Header(alias="Range")]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match")]
ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
router = APIRouter()


class EventIn(BaseModel):
    id: UUID
    word_id: str
    action: ReviewAction
    reviewed_at: datetime


class WordOut(BaseModel):
    id: str
    term: str
    level: int | None
    transcript: str | None
    audio_url: str
    tier: Tier


class SetOut(BaseModel):
    items: list[WordOut]
    learning_step_seconds: int
    status: Literal["ready", "next_due", "all_suspended", "catalog_empty"]
    next_due: datetime | None = None


class ExcludedWordOut(BaseModel):
    id: str
    term: str
    level: int | None
    tier: Tier


class ExclusionsOut(BaseModel):
    items: list[ExcludedWordOut]


class EventResultOut(BaseModel):
    id: UUID
    status: Literal["applied", "idempotent", "voided", "conflict", "missing"]
    due_at: datetime | None = None


class BatchOut(BaseModel):
    results: list[EventResultOut]


def word_out(word: Word) -> WordOut:
    return WordOut(
        id=word.id,
        term=word.term,
        level=word.level,
        transcript=word.transcript,
        audio_url=f"/api/v1/audio/{word.id}",
        tier=word.tier,
    )


def _audio_metadata(store: AudioStore, key: str) -> AudioMetadata:
    try:
        return store.head(key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except AudioStorageError as exc:
        raise HTTPException(502, "audio storage is unavailable") from exc


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    container = request.app.state.container
    if container.environment not in {"local", "test"}:
        try:
            assert container.auth is not None
            container.auth.validate_session(request.cookies.get(container.auth.session_cookie_name))
        except AuthenticationError:
            return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "index.html", {"title": "English Vocab Trainer"})


@router.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness(request: Request) -> dict[str, str]:
    """A public readiness probe that opens and initializes the current SQLite schema."""
    container = request.app.state.container
    try:
        with SQLiteVocabularyRepository(container.database) as repository:
            repository.db.execute("SELECT 1 FROM words LIMIT 1").fetchone()
    except SchemaMismatchError as exc:
        raise HTTPException(503, "database schema is incompatible") from exc
    except Exception as exc:
        raise HTTPException(503, "database is not ready") from exc
    return {"status": "ready"}


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Sign in", "error": False},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    container = request.app.state.container
    if container.environment in {"local", "test"} or container.auth is None:
        raise HTTPException(404, "not found")
    origin = request.headers.get("Origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "invalid origin")
    # Reject a surprisingly large form before multipart parsing.  We still call
    # authenticate with a harmless value so the generic response consumes a
    # limiter reservation; an attacker cannot bypass the global limiter by
    # switching to an oversized request.
    try:
        oversized = int(request.headers.get("Content-Length", "0")) > 4_096
    except ValueError:
        oversized = True
    password: object
    if oversized:
        password = ""
    else:
        form = await request.form()
        password = form.get("password")
    cookies = container.auth.authenticate(password if isinstance(password, str) else "")
    if cookies is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Sign in", "error": True},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
    common = {
        "max_age": 30 * 24 * 60 * 60,
        "secure": container.auth.secure_cookies,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(
        container.auth.session_cookie_name, cookies.session, httponly=True, **common
    )
    response.set_cookie(container.auth.csrf_cookie_name, cookies.csrf, httponly=False, **common)
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request, _: Csrf) -> Response:
    container = request.app.state.container
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if container.auth is not None:
        for name in (container.auth.session_cookie_name, container.auth.csrf_cookie_name):
            response.delete_cookie(
                name, path="/", secure=container.auth.secure_cookies, samesite="strict"
            )
    return response


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Serve the worker at the application root so it controls offline navigations."""
    return FileResponse(
        ROOT / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@router.get("/api/v1/sets", response_model=SetOut)
def study_set(repository: Repository) -> SetOut:
    now = datetime.now(UTC)
    words = select_set_words(repository, now, Random())
    status: Literal["ready", "next_due", "all_suspended", "catalog_empty"] = "ready"
    next_due = None
    if not words:
        status, next_due = repository.empty_set_status(now)
    return SetOut(
        items=[word_out(word) for word in words],
        learning_step_seconds=int(LEARNING_STEP.total_seconds()),
        status=status,
        next_due=next_due,
    )


@router.get("/api/v1/exclusions", response_model=ExclusionsOut)
def exclusions(repository: Repository) -> ExclusionsOut:
    return ExclusionsOut(
        items=[
            ExcludedWordOut(id=word.id, term=word.term, level=word.level, tier=word.tier)
            for word in repository.list_excluded_words()
        ]
    )


@router.post("/api/v1/exclusions/{word_id}", status_code=204)
def exclude_word(word_id: str, repository: Repository, _: Csrf) -> Response:
    try:
        repository.set_excluded(word_id, True)
    except MissingError as exc:
        raise HTTPException(404, "word not found") from exc
    return Response(status_code=204)


@router.delete("/api/v1/exclusions/{word_id}", status_code=204)
def restore_word(word_id: str, repository: Repository, _: Csrf) -> Response:
    try:
        repository.set_excluded(word_id, False)
    except MissingError as exc:
        raise HTTPException(404, "word not found") from exc
    return Response(status_code=204)


@router.post("/api/v1/review-events/batch", response_model=BatchOut)
def review_batch(events: list[EventIn], repository: Repository, _: Csrf) -> BatchOut:
    if len(events) > 100:
        raise HTTPException(422, "at most 100 events")
    results: list[EventResultOut] = []
    for event in events:
        try:
            result = submit_review(
                repository, event.id, event.word_id, event.action, event.reviewed_at
            )
            results.append(
                EventResultOut(
                    id=event.id,
                    status=result.status,
                    due_at=result.state.due_at,
                )
            )
        except EventConflictError:
            results.append(EventResultOut(id=event.id, status="conflict"))
        except MissingError:
            results.append(EventResultOut(id=event.id, status="missing"))
    return BatchOut(results=results)


@router.post("/api/v1/review-events/{event_id}/void", status_code=204)
def void_event(event_id: UUID, repository: Repository, _: Csrf) -> Response:
    try:
        repository.void_event(event_id)
    except MissingError as exc:
        raise HTTPException(404, "event not found") from exc
    return Response(status_code=204)


@router.get("/api/v1/audio/{word_id}")
def audio(
    word_id: str,
    repository: Repository,
    store: Audio,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    word = repository.get_word(word_id)
    if word is None:
        raise HTTPException(404, "word not found")
    # RFC 7233 evaluates Range only after preconditions: a matching cache validator
    # wins even over an otherwise invalid range and must not fetch the object body.
    if if_none_match is not None or range_header is not None:
        metadata = _audio_metadata(store, word.audio_key)
        if if_none_match is not None and if_none_match_matches(metadata.etag, if_none_match):
            return build_audio_head_response(metadata, None, if_none_match)
        if range_header is not None:
            try:
                parse_single_range(range_header, metadata.size)
            except InvalidRangeError:
                return Response(
                    status_code=416,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{metadata.size}",
                    },
                )
    try:
        result = store.get(word.audio_key, range_header)
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except InvalidRangeError:
        # An upstream may reject a range after local validation (for example, a race).
        metadata = _audio_metadata(store, word.audio_key)
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
        )
    except AudioStorageError as exc:
        raise HTTPException(502, "audio storage is unavailable") from exc
    return build_audio_response(result, if_none_match)


@router.head("/api/v1/audio/{word_id}")
def audio_head(
    word_id: str,
    repository: Repository,
    store: Audio,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    word = repository.get_word(word_id)
    if word is None:
        raise HTTPException(404, "word not found")
    metadata = _audio_metadata(store, word.audio_key)
    try:
        return build_audio_head_response(metadata, range_header, if_none_match)
    except InvalidRangeError:
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
        )


def create_app(container: AppContainer) -> FastAPI:
    new_app = FastAPI(title="English Vocab Trainer", version="1.0.0")
    new_app.state.container = container
    if container.environment == "production":
        # A manually assembled production container is useful in tests; the
        # env factory never permits this fallback for an internet-facing app.
        allowed_hosts = list(container.trusted_hosts or ("localhost", "127.0.0.1", "testserver"))
        new_app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @new_app.middleware("http")
    async def private_response_cache_control(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if (
            (path.startswith("/api/") and not path.startswith("/api/v1/audio/"))
            or path.startswith("/auth/")
            or path == "/login"
        ):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self'; media-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'"
        )
        if container.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    new_app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    new_app.include_router(router)
    return new_app


def container_from_env(environ: Mapping[str, str] = os.environ) -> AppContainer:
    environment = environ.get("APP_ENV", "production")
    if environment in {"local", "test"}:
        try:
            database = Path(environ["VOCAB_DB_PATH"])
        except KeyError as exc:
            raise ConfigurationError("VOCAB_DB_PATH is required") from exc
        return AppContainer(
            database,
            audio_store_from_env(environ),
            environment,
        )
    try:
        database = Path(environ["VOCAB_DB_PATH"])
        return AppContainer(
            database,
            audio_store_from_env(environ),
            "production",
            auth=auth_from_env(environ, database, secure_cookies=True),
            trusted_hosts=trusted_hosts_from_env(environ),
        )
    except (KeyError, ConfigurationError) as exc:
        raise ConfigurationError(
            "production storage or authentication configuration is incomplete"
        ) from exc


def create_app_from_env() -> FastAPI:
    container = container_from_env()
    with SQLiteVocabularyRepository(container.database):
        pass
    return create_app(container)
