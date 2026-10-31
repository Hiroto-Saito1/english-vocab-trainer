from datetime import datetime
from typing import Protocol
from uuid import UUID

from english_vocab_trainer.domain.models import Review, Word


class MissingWordError(RuntimeError):
    pass


class ReviewConflictError(RuntimeError):
    pass


class VocabularyRepository(Protocol):
    async def get_word(self, word_id: str) -> Word | None: ...
    async def next_word(self, now: datetime) -> Word | None: ...
    async def next_due(self, now: datetime) -> datetime | None: ...
    async def save_review(self, review: Review) -> None: ...
    async def history_page(
        self, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[Review, Word]]: ...
