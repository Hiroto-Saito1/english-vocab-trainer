from datetime import UTC, datetime
from typing import cast

from english_vocab_trainer.application.services import select_next_word
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.ports.repositories import VocabularyRepository


class CardRepository:
    def __init__(self, selected: Word | None) -> None:
        self.selected = selected
        self.received: datetime | None = None

    def next_word(self, now: datetime) -> Word | None:
        self.received = now
        return self.selected


def test_next_word_delegates_one_card_selection_to_repository() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    word = Word("one", "one", 1, "definition", "one.mp3", Tier.UPPER)
    repository = CardRepository(word)
    assert select_next_word(cast(VocabularyRepository, repository), now) is word
    assert repository.received == now
