from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from random import Random
from time import monotonic
from uuid import uuid4

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.application.services import create_study_session, submit_review
from english_vocab_trainer.domain.models import ReviewAction, Word


def test_two_thousand_word_session_progress_and_review_smoke(tmp_path: Path) -> None:
    started = monotonic()
    repository = SQLiteVocabularyRepository(tmp_path / "v.db", "student")
    try:
        repository.bulk_upsert_words(
            [
                Word(str(index), f"term {index}", index % 10, None, f"{index}.mp3")
                for index in range(2_000)
            ]
        )
        repository.update_settings(100)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        session = create_study_session(repository, "daily", now, "scale", None, Random(7))
        review = submit_review(repository, uuid4(), session.words[0].id, ReviewAction.KNOWN, now)
        progress = repository.progress(now)
    finally:
        repository.close()
    assert len(session.words) == 100 and len({word.id for word in session.words}) == 100
    assert review.created and progress == {"total": 2_000, "due": 0, "reviewed": 1}
    assert monotonic() - started < 8
