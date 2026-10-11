from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from random import Random
from time import monotonic
from uuid import uuid4

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.application.services import create_study_session, submit_review
from english_vocab_trainer.domain.models import ReviewAction, Tier, Word


def test_two_thousand_word_session_progress_and_review_smoke(tmp_path: Path) -> None:
    started = monotonic()
    repository = SQLiteVocabularyRepository(tmp_path / "v.db")
    try:
        repository.bulk_upsert_words(
            [
                Word(
                    str(index),
                    f"term {index}",
                    index % 10,
                    None,
                    f"{index}.mp3",
                    Tier.UPPER if index % 2 == 0 else Tier.ULTRA,
                )
                for index in range(2_000)
            ]
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        session = create_study_session(repository, now, "scale", Random(7))
        event_id = uuid4()
        review = submit_review(repository, event_id, session.words[0].id, ReviewAction.KNOWN, now)
        stored = repository.get_event(event_id)
    finally:
        repository.close()
    assert len(session.words) == 30 and len({word.id for word in session.words}) == 30
    assert review.status == "applied" and stored is not None
    assert monotonic() - started < 8
