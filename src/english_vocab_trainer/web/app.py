from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from english_vocab_trainer.adapters.local import InMemoryVocabularyRepository
from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.adapters.local.sqlite import MissingError
from english_vocab_trainer.application.services import create_study_session, submit_review
from english_vocab_trainer.domain.models import Rating, ReviewAction, ReviewEvent
from english_vocab_trainer.ports.repositories import VocabularyRepository
from english_vocab_trainer.web.container import (
    AppContainer,
    ConfigurationError,
    UnavailableAudioStore,
    UnavailableRepositoryProvider,
)
from english_vocab_trainer.web.dependencies import identity
from english_vocab_trainer.web.dependencies import repository as repository_dependency

Identity = Annotated[str, Depends(identity)]
Repository = Annotated[VocabularyRepository, Depends(repository_dependency)]
ROOT = Path(__file__).parent
repo = InMemoryVocabularyRepository()
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
    audio_key: str


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
            items=[WordOut(**asdict(word)) for word in session.words],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/v1/review-events/batch")
def review_batch(
    events: list[EventIn], _: Identity, repository: Repository
) -> dict[str, list[str]]:
    if len(events) > 100:
        raise HTTPException(422, "at most 100 events")
    return {
        "acknowledged": [
            str(submit_review(repository, e.id, e.word_id, e.action, e.reviewed_at).id)
            for e in events
        ]
    }


@router.post("/api/v1/review-events/{event_id}/void")
def void_event(event_id: UUID, _: Identity) -> dict[str, str]:
    event = repo.events.get(str(event_id))
    if event is None:
        raise HTTPException(404, "event not found")
    repo.events[str(event_id)] = ReviewEvent(
        event.id, event.word_id, event.rating, event.reviewed_at, datetime.now(UTC)
    )
    return {"id": str(event_id), "status": "voided"}


@router.get("/api/v1/audio/{word_id}")
def audio(word_id: str, _: Identity) -> Response:
    if repo.get_word(word_id) is None:
        raise HTTPException(404, "word not found")
    return Response(status_code=501, headers={"Accept-Ranges": "bytes"})


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
        items=[
            WordOut(**asdict(word)) for word in repository.list_words(levels=levels, limit=limit)
        ]
    )


@router.patch("/api/v1/words/{word_id}/transcript", response_model=WordOut)
def transcript(word_id: str, body: TranscriptIn, _: Identity, repository: Repository) -> WordOut:
    try:
        return WordOut(**asdict(repository.update_transcript(word_id, body.transcript)))
    except (KeyError, MissingError) as exc:
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
