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
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.application.services import create_study_session, submit_review
from english_vocab_trainer.domain.models import Rating, ReviewAction, Word
from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.ports.errors import (
    ConcurrentUpdateError,
    EventConflictError,
    MissingError,
)
from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.web.audio import build_audio_response
from english_vocab_trainer.web.container import (
    AppContainer,
    ConfigurationError,
    UnavailableAudioStore,
    UnavailableRepositoryProvider,
)
from english_vocab_trainer.web.dependencies import audio_store, identity
from english_vocab_trainer.web.dependencies import repository as repository_dependency

Identity = Annotated[str, Depends(identity)]
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
        if any("\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff" for char in value):
            raise ValueError("transcript must be English only")
        return value


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
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"title": "English Vocab Trainer"})


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
def review_batch(events: list[EventIn], _: Identity, repository: Repository) -> BatchOut:
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
def void_event(event_id: UUID, _: Identity, repository: Repository) -> VoidOut:
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
    try:
        result = store.get(word.audio_key, range_header)
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except ValueError:
        full = store.get(word.audio_key, None)
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{full.size}"},
        )
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
    response = audio(word_id, _, repository, store, range_header, if_none_match)
    response.body = b""
    return response


@router.get("/api/v1/progress", response_model=ProgressOut)
def progress(_: Identity, repository: Repository) -> ProgressOut:
    return ProgressOut(**repository.progress(datetime.now(UTC)))


@router.get("/api/v1/settings", response_model=SettingsOut)
def get_settings(_: Identity, repository: Repository) -> SettingsOut:
    return SettingsOut(daily_target=repository.get_settings().daily_target)


@router.patch("/api/v1/settings", response_model=SettingsOut)
def settings(settings: SettingsIn, _: Identity, repository: Repository) -> SettingsOut:
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
def transcript(word_id: str, body: TranscriptIn, _: Identity, repository: Repository) -> WordOut:
    try:
        return word_out(repository.update_transcript(word_id, body.transcript))
    except (MissingError, KeyError) as exc:
        raise HTTPException(404, "word not found") from exc


def create_app(container: AppContainer) -> FastAPI:
    new_app = FastAPI(title="English Vocab Trainer", version="1.0.0")
    new_app.state.container = container
    new_app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    new_app.include_router(router)
    return new_app


def container_from_env(environ: Mapping[str, str] = os.environ) -> AppContainer:
    environment = environ.get("APP_ENV", "production")
    if environment in {"local", "test"}:
        try:
            database = Path(environ["VOCAB_DB_PATH"])
            audio = Path(environ["AUDIO_ROOT"])
        except KeyError as exc:
            raise ConfigurationError("VOCAB_DB_PATH and AUDIO_ROOT are required") from exc
        return AppContainer(
            SQLiteRepositoryProvider(database),
            FilesystemAudioStore(audio),
            environment,
            environ.get("LOCAL_USER_ID", "local-user"),
        )
    return AppContainer(UnavailableRepositoryProvider(), UnavailableAudioStore(), "production")


app = create_app(container_from_env())
