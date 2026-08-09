from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def test_progress_uses_test_container(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        response = client.get("/api/v1/progress")
    assert response.status_code == 200 and response.json()["total"] == 1


def test_settings_patch_persists_and_validates(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        assert client.get("/api/v1/settings").json()["daily_target"] == 30
        assert (
            client.patch("/api/v1/settings", json={"daily_target": 42}).json()["daily_target"] == 42
        )
        assert client.get("/api/v1/settings").json()["daily_target"] == 42
        assert client.patch("/api/v1/settings", json={"daily_target": 0}).status_code == 422
        assert client.patch("/api/v1/settings", json={"daily_target": 101}).status_code == 422
