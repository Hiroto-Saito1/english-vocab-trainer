from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def test_daily_session_returns_thirty_balanced_unpersisted_words(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(
            [
                Word(
                    str(index),
                    f"term {index}",
                    index % 4,
                    None,
                    f"{index}.mp3",
                    Tier.UPPER if index % 2 == 0 else Tier.ULTRA,
                )
                for index in range(100)
            ]
        )
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions")
    payload = response.json()
    tiers = [item["tier"] for item in payload["items"]]
    assert response.status_code == 200 and len(payload["items"]) == 30
    assert len({item["id"] for item in payload["items"]}) == 30
    assert abs(tiers.count("upper") - tiers.count("ultra")) <= 1
    assert "id" not in payload and "mode" not in payload and "created_at" not in payload
    assert payload["learning_step_seconds"] == 600


def test_empty_daily_session_is_returned(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions")
    assert response.status_code == 200 and response.json()["items"] == []
