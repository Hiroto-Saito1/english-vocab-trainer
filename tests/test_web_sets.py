from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Tier, Word
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
    assert payload["learning_step_seconds"] == 600


def test_empty_set_is_returned(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sets")
    assert response.status_code == 200 and response.json()["items"] == []
