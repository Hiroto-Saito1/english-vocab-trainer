"""Study card, review, and server-backed history routes."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from english_vocab_trainer.adapters.local.vocabulary import (
    MissingWordError,
    ReviewConflictError,
    SQLiteVocabularyRepository,
)
from english_vocab_trainer.domain.models import Review, ReviewAction, Word
from english_vocab_trainer.web.dependencies import csrf
from english_vocab_trainer.web.dependencies import repository as repository_dependency

Csrf = Annotated[None, Depends(csrf)]
Repository = Annotated[SQLiteVocabularyRepository, Depends(repository_dependency)]
HISTORY_PAGE_SIZE = 50
router = APIRouter()


class ReviewIn(BaseModel):
    id: UUID
    word_id: str
    action: ReviewAction


class WordOut(BaseModel):
    word_id: str
    term: str
    definition: str
    audio_url: str


class CardOut(BaseModel):
    item: WordOut | None
    next_due: datetime | None = None


class HistoryItemOut(BaseModel):
    review_id: UUID
    word_id: str
    action: ReviewAction
    reviewed_at: datetime
    term: str
    definition: str
    audio_url: str


class HistoryOut(BaseModel):
    items: list[HistoryItemOut]
    next_cursor: str | None


def word_out(word: Word) -> WordOut:
    return WordOut(
        word_id=word.id,
        term=word.term,
        definition=word.definition,
        audio_url=f"/api/v1/audio/{word.id}",
    )


def _history_cursor(reviewed_at: datetime, review_id: UUID) -> str:
    value = f"{reviewed_at.astimezone(UTC).isoformat()}|{review_id}"
    return urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def _parse_history_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        reviewed_at, review_id = decoded.rsplit("|", 1)
        return datetime.fromisoformat(reviewed_at), UUID(review_id)
    except (Base64Error, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "invalid history cursor") from exc


@router.get("/api/v1/card", response_model=CardOut)
def next_card(repository: Repository) -> CardOut:
    now = datetime.now(UTC)
    word = repository.next_word(now)
    if word is not None:
        return CardOut(item=word_out(word))
    return CardOut(item=None, next_due=repository.next_due(now))


@router.post("/api/v1/reviews", status_code=204)
def review(review_input: ReviewIn, repository: Repository, _: Csrf) -> Response:
    try:
        repository.save_review(
            Review(
                review_input.id,
                review_input.word_id,
                review_input.action,
                datetime.now(UTC),
            )
        )
    except ReviewConflictError as exc:
        raise HTTPException(409, "review conflict") from exc
    except MissingWordError as exc:
        raise HTTPException(404, "word not found") from exc
    return Response(status_code=204)


@router.get("/api/v1/history", response_model=HistoryOut)
def history(repository: Repository, cursor: str | None = None) -> HistoryOut:
    parsed = _parse_history_cursor(cursor) if cursor else None
    rows = repository.history_page(parsed, HISTORY_PAGE_SIZE + 1)
    has_more = len(rows) > HISTORY_PAGE_SIZE
    rows = rows[:HISTORY_PAGE_SIZE]
    items = [
        HistoryItemOut(
            review_id=review.id,
            word_id=review.word_id,
            action=review.action,
            reviewed_at=review.reviewed_at,
            term=word.term,
            definition=word.definition,
            audio_url=f"/api/v1/audio/{word.id}",
        )
        for review, word in rows
    ]
    next_cursor = None
    if has_more and rows:
        review = rows[-1][0]
        next_cursor = _history_cursor(review.reviewed_at, review.id)
    return HistoryOut(items=items, next_cursor=next_cursor)
