from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from english_vocab_trainer.adapters.cloudflare.auth import verify_access_jwt
from english_vocab_trainer.adapters.local import InMemoryVocabularyRepository
from english_vocab_trainer.application.services import apply_events, daily_study, screen_new_words
from english_vocab_trainer.domain.models import Rating, ReviewEvent, Word

ROOT = Path(__file__).parent
repo = InMemoryVocabularyRepository()
templates = Jinja2Templates(directory=str(ROOT / "templates"))
app = FastAPI(title="English Vocab Trainer", version="1.0.0")
router = APIRouter()
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


class EventIn(BaseModel):
    id: UUID
    word_id: str
    rating: Rating
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


def identity(assertion: str | None = Header(None, alias="Cf-Access-Jwt-Assertion")) -> str:
    if os.getenv("APP_ENV") in {"local", "test"}:
        return "local-user"
    if not assertion:
        raise HTTPException(403, "Cloudflare Access authentication required")
    try:
        claims = verify_access_jwt(
            assertion,
            os.environ["CF_ACCESS_PUBLIC_KEY"],
            os.environ["CF_ACCESS_ISSUER"],
            os.environ["CF_ACCESS_AUDIENCE"],
        )
        return str(claims["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(403, "invalid Cloudflare Access assertion") from exc


def wire_demo_data() -> None:
    if not repo.words:
        repo.words["demo-1"] = Word("demo-1", "example", 1, "An example sentence.", "demo-1.mp3")


@router.on_event("startup")
def startup() -> None:
    wire_demo_data()


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"title": "English Vocab Trainer"})


@router.get("/api/v1/sessions")
def sessions(_: str = identity()) -> dict[str, object]:
    return {"items": [asdict(word) for word in daily_study(repo, datetime.now(UTC))]}


@router.get("/api/v1/sessions/new/{count}")
def new_session(count: int, _: str = identity()) -> dict[str, object]:
    try:
        return {"items": [asdict(word) for word in screen_new_words(repo, count)]}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/v1/review-events/batch")
def review_batch(events: list[EventIn], _: str = identity()) -> dict[str, list[str]]:
    domain_events = [ReviewEvent(e.id, e.word_id, e.rating, e.reviewed_at) for e in events]
    return {"applied": apply_events(repo, domain_events)}


@router.post("/api/v1/review-events/{event_id}/void")
def void_event(event_id: UUID, _: str = identity()) -> dict[str, str]:
    event = repo.events.get(str(event_id))
    if event is None:
        raise HTTPException(404, "event not found")
    repo.events[str(event_id)] = ReviewEvent(
        event.id, event.word_id, event.rating, event.reviewed_at, datetime.now(UTC)
    )
    return {"id": str(event_id), "status": "voided"}


@router.get("/api/v1/audio/{word_id}")
def audio(word_id: str, _: str = identity()) -> Response:
    if repo.get_word(word_id) is None:
        raise HTTPException(404, "word not found")
    return Response(status_code=501, headers={"Accept-Ranges": "bytes"})


@router.get("/api/v1/progress")
def progress(_: str = identity()) -> dict[str, int]:
    return repo.progress(datetime.now(UTC))


@router.get("/api/v1/settings")
def get_settings(_: str = identity()) -> SettingsIn:
    return SettingsIn()


@router.patch("/api/v1/settings")
def settings(settings: SettingsIn, _: str = identity()) -> SettingsIn:
    return settings


@router.get("/api/v1/words")
def words(limit: int = 100, _: str = identity()) -> dict[str, object]:
    return {"items": [asdict(word) for word in repo.list_words(limit=limit)]}


@router.patch("/api/v1/words/{word_id}/transcript")
def transcript(word_id: str, body: TranscriptIn, _: str = identity()) -> dict[str, object]:
    try:
        return asdict(repo.update_transcript(word_id, body.transcript))
    except KeyError as exc:
        raise HTTPException(404, "word not found") from exc


app.include_router(router)
