from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Rating, ReviewEvent, Tier, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def test_set_admits_thirty_lowest_level_words_without_persisting_selection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v.db"
    words = [
        Word(
            str(index),
            f"term {index}",
            None if index < 10 else index // 10,
            None,
            f"{index}.mp3",
            Tier.UPPER if index % 2 == 0 else Tier.ULTRA,
        )
        for index in range(100)
    ]
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(words)
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sets")
    payload = response.json()
    selected = payload["items"]
    assert response.status_code == 200 and len(selected) == 30
    assert len({item["id"] for item in selected}) == 30
    assert {item["id"] for item in selected} == {str(index) for index in range(10, 40)}
    assert "id" not in payload and "mode" not in payload and "created_at" not in payload
    assert "learning_step_seconds" not in payload
    assert payload["status"] == "ready" and payload["next_due"] is None


def test_empty_set_is_returned(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sets")
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "status": "catalog_empty",
        "next_due": None,
    }


def test_empty_set_reports_the_next_future_review(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    word = Word("word", "word", 1, "definition", "word.mp3", Tier.UPPER)
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words([word])
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        with SQLiteVocabularyRepository(database) as repository:
            repository.append_event(
                ReviewEvent(
                    UUID("00000000-0000-0000-0000-000000000001"),
                    word.id,
                    Rating.GOOD,
                    datetime.now(UTC),
                )
            )
        future = client.get("/api/v1/sets").json()
    assert future["status"] == "next_due"
    assert future["items"] == [] and future["next_due"] is not None
