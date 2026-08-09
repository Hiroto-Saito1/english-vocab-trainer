from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.application.services import create_study_session, submit_review
from english_vocab_trainer.domain.models import Rating, ReviewAction, Word
from english_vocab_trainer.ports.audio import (
    AudioStorageError,
    AudioStore,
    InvalidRangeError,
    parse_single_range,
)
from english_vocab_trainer.ports.errors import (
    ConcurrentUpdateError,
    EventConflictError,
    MissingError,
)
from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.validation import validate_english_transcript
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
from english_vocab_trainer.web.dependencies import audio_store, csrf, identity
from english_vocab_trainer.web.dependencies import repository as repository_dependency

Identity = Annotated[str, Depends(identity)]
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


class TranscriptIn(BaseModel):
    transcript: str = Field(min_length=1, max_length=20_000)

    @field_validator("transcript")
    @classmethod
    def english_only(cls, value: str) -> str:
        return validate_english_transcript(value)


class SettingsIn(BaseModel):
    daily_target: int = Field(default=30, ge=1, le=100)


class ProgressOut(BaseModel):
    total: int
    due: int
    reviewed: int


class SettingsOut(BaseModel):
    daily_target: int


class WordOut(BaseModel):
    id: str
    term: str
    level: int | None
    transcript: str | None
    audio_url: str


class WordListOut(BaseModel):
    items: list[WordOut]


class SessionOut(BaseModel):
    id: str
    mode: str
    created_at: datetime
    items: list[WordOut]


class EventResultOut(BaseModel):
    id: UUID
    word_id: str
    status: Literal["applied", "idempotent", "voided", "conflict", "missing"]
    rating: Rating | None = None
    detail: str | None = None


class BatchOut(BaseModel):
    results: list[EventResultOut]
    acknowledged: list[UUID]


class VoidOut(BaseModel):
    id: UUID
    status: Literal["voided"]
    word_id: str
    due_at: datetime
    version: int


def word_out(word: Word) -> WordOut:
    values = asdict(word)
    return WordOut(
        id=values["id"],
        term=values["term"],
        level=values["level"],
        transcript=values["transcript"],
        audio_url=f"/api/v1/audio/{values['id']}",
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    container = request.app.state.container
    if container.environment not in {"local", "test"}:
        try:
            assert container.auth is not None
            container.auth.user_from_session(
                request.cookies.get(container.auth.session_cookie_name)
            )
        except AuthenticationError:
            return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "index.html", {"title": "English Vocab Trainer"})


@router.get("/health", include_in_schema=False)
@router.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness(request: Request) -> dict[str, str]:
    """A public readiness probe that exercises opening and migrating SQLite only."""
    container = request.app.state.container
    try:
        repository = container.repositories.for_user("healthcheck")
        try:
            repository.progress(datetime.now(UTC))
        finally:
            repository.close()
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
def logout(request: Request, _: Identity, __: Csrf) -> Response:
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


@router.get("/api/v1/sessions", response_model=SessionOut)
def sessions(
    _: Identity, repository: Repository, mode: str = "daily", count: int | None = None
) -> SessionOut:
    try:
        session = create_study_session(
            repository, mode, datetime.now(UTC), str(uuid4()), count, Random()
        )
        return SessionOut(
            id=session.id,
            mode=session.kind,
            created_at=session.created_at,
            items=[word_out(word) for word in session.words],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/v1/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, _: Identity, repository: Repository) -> SessionOut:
    session = repository.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return SessionOut(
        id=session.id,
        mode=session.kind,
        created_at=session.created_at,
        items=[word_out(word) for word in session.words],
    )


@router.post("/api/v1/review-events/batch", response_model=BatchOut)
def review_batch(events: list[EventIn], _: Identity, __: Csrf, repository: Repository) -> BatchOut:
    if len(events) > 100:
        raise HTTPException(422, "at most 100 events")
    results: list[EventResultOut] = []
    acknowledged: list[UUID] = []
    for event in events:
        try:
            result = submit_review(
                repository, event.id, event.word_id, event.action, event.reviewed_at
            )
            status: Literal["applied", "idempotent", "voided"] = (
                "voided" if result.voided else "applied" if result.created else "idempotent"
            )
            results.append(
                EventResultOut(
                    id=event.id, word_id=event.word_id, status=status, rating=result.rating
                )
            )
            acknowledged.append(event.id)
        except (EventConflictError, ConcurrentUpdateError) as exc:
            results.append(
                EventResultOut(
                    id=event.id, word_id=event.word_id, status="conflict", detail=str(exc)
                )
            )
        except (MissingError, KeyError) as exc:
            results.append(
                EventResultOut(
                    id=event.id, word_id=event.word_id, status="missing", detail=str(exc)
                )
            )
    return BatchOut(results=results, acknowledged=acknowledged)


@router.post("/api/v1/review-events/{event_id}/void", response_model=VoidOut)
def void_event(event_id: UUID, _: Identity, __: Csrf, repository: Repository) -> VoidOut:
    event = repository.get_event(event_id)
    if event is None:
        raise HTTPException(404, "event not found")
    try:
        state = repository.void_event(event_id)
    except MissingError as exc:
        raise HTTPException(404, "event not found") from exc
    except (EventConflictError, ConcurrentUpdateError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return VoidOut(
        id=event_id,
        status="voided",
        word_id=event.word_id,
        due_at=state.due_at,
        version=state.version,
    )


@router.get("/api/v1/audio/{word_id}")
def audio(
    word_id: str,
    _: Identity,
    repository: Repository,
    store: Audio,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    word = repository.get_word(word_id)
    if word is None:
        raise HTTPException(404, "word not found")
    metadata = None
    # RFC 7233 evaluates Range only after preconditions: a matching cache validator
    # wins even over an otherwise invalid range and must not fetch the object body.
    if if_none_match is not None:
        try:
            metadata = store.head(word.audio_key)
        except FileNotFoundError as exc:
            raise HTTPException(404, "audio not found") from exc
        except (AudioStorageError, ConfigurationError) as exc:
            raise HTTPException(502, "audio storage is unavailable") from exc
        if if_none_match_matches(metadata.etag, if_none_match):
            return build_audio_head_response(metadata, None, if_none_match)
    if range_header is not None:
        try:
            metadata = metadata or store.head(word.audio_key)
            assert metadata is not None
            parse_single_range(range_header, metadata.size)
        except FileNotFoundError as exc:
            raise HTTPException(404, "audio not found") from exc
        except InvalidRangeError:
            assert metadata is not None
            return Response(
                status_code=416,
                headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
            )
        except (AudioStorageError, ConfigurationError) as exc:
            raise HTTPException(502, "audio storage is unavailable") from exc
    try:
        result = store.get(word.audio_key, range_header)
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except InvalidRangeError:
        # An upstream may reject a range after local validation (for example, a race).
        try:
            metadata = store.head(word.audio_key)
        except FileNotFoundError as exc:
            raise HTTPException(404, "audio not found") from exc
        except (AudioStorageError, ConfigurationError) as exc:
            raise HTTPException(502, "audio storage is unavailable") from exc
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
        )
    except (AudioStorageError, ConfigurationError) as exc:
        raise HTTPException(502, "audio storage is unavailable") from exc
    return build_audio_response(result, False, if_none_match)


@router.head("/api/v1/audio/{word_id}")
def audio_head(
    word_id: str,
    _: Identity,
    repository: Repository,
    store: Audio,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    word = repository.get_word(word_id)
    if word is None:
        raise HTTPException(404, "word not found")
    try:
        metadata = store.head(word.audio_key)
        try:
            return build_audio_head_response(metadata, range_header, if_none_match)
        except InvalidRangeError:
            return Response(
                status_code=416,
                headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
            )
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except (AudioStorageError, ConfigurationError) as exc:
        raise HTTPException(502, "audio storage is unavailable") from exc


@router.get("/api/v1/progress", response_model=ProgressOut)
def progress(_: Identity, repository: Repository) -> ProgressOut:
    return ProgressOut(**repository.progress(datetime.now(UTC)))


@router.get("/api/v1/settings", response_model=SettingsOut)
def get_settings(_: Identity, repository: Repository) -> SettingsOut:
    return SettingsOut(daily_target=repository.get_settings().daily_target)


@router.patch("/api/v1/settings", response_model=SettingsOut)
def settings(settings: SettingsIn, _: Identity, __: Csrf, repository: Repository) -> SettingsOut:
    updated = repository.update_settings(settings.daily_target)
    return SettingsOut(daily_target=updated.daily_target)


@router.get("/api/v1/words", response_model=WordListOut)
def words(
    _: Identity,
    repository: Repository,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    levels: Annotated[list[int] | None, Query()] = None,
) -> WordListOut:
    return WordListOut(
        items=[word_out(word) for word in repository.list_words(levels=levels, limit=limit)]
    )


@router.patch("/api/v1/words/{word_id}/transcript", response_model=WordOut)
def transcript(
    word_id: str, body: TranscriptIn, _: Identity, __: Csrf, repository: Repository
) -> WordOut:
    try:
        return word_out(repository.update_transcript(word_id, body.transcript))
    except (MissingError, KeyError) as exc:
        raise HTTPException(404, "word not found") from exc


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
            SQLiteRepositoryProvider(database),
            audio_store_from_env(environ),
            environment,
            environ.get("LOCAL_USER_ID", "local-user"),
        )
    try:
        database = Path(environ["VOCAB_DB_PATH"])
        return AppContainer(
            SQLiteRepositoryProvider(database),
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
    return create_app(container_from_env())
