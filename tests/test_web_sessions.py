from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def test_screen_session_is_persisted(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions?mode=screen&count=20")
        assert client.get("/api/v1/sessions?mode=screen").status_code == 422
        assert client.get("/api/v1/sessions?mode=screen&count=19").status_code == 422
        assert client.get("/api/v1/sessions?mode=other").status_code == 422
    payload = response.json()
    stored_repository = provider.for_user("local-user")
    stored = stored_repository.get_session(payload["id"])
    stored_repository.close()
    assert response.status_code == 200 and len(payload["items"]) == 1 and stored is not None
