from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Review, ReviewAction, Word


def test_two_thousand_word_next_card_and_review_smoke(tmp_path: Path) -> None:
    started = monotonic()
    repository = SQLiteVocabularyRepository(tmp_path / "v.db")
    try:
        repository.bulk_upsert_words(
            [
                Word(
                    str(index),
                    f"term {index}",
                    index % 10,
                    f"definition {index}",
                    f"{index}.mp3",
                )
                for index in range(2_000)
            ]
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        selected = repository.next_word(now)
        assert selected is not None
        review_id = uuid4()
        repository.save_review(Review(review_id, selected.id, ReviewAction.KNOWN, now))
        stored = repository.db.execute(
            "SELECT id FROM reviews WHERE id=?", (str(review_id),)
        ).fetchone()
    finally:
        repository.close()
    assert selected.id == "0"
    assert stored["id"] == str(review_id)
    assert monotonic() - started < 8
