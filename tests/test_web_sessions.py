from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def test_screen_session_is_persisted(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3", Tier.UPPER))
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
    assert (
        response.status_code == 200
        and len(payload["items"]) == 1
        and payload["items"][0]["audio_url"] == "/api/v1/audio/one"
        and payload["items"][0]["tier"] == "upper"
        and payload["learning_step_seconds"] == 600
        and stored is not None
    )


@pytest.mark.parametrize("target", [1, 30, 100])
def test_daily_session_honours_configured_target(tmp_path: Path, target: int) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    for index in range(100):
        repository.add_word(Word(str(index), f"term {index}", index % 4, None, f"{index}.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        assert client.patch("/api/v1/settings", json={"daily_target": target}).status_code == 200
        response = client.get("/api/v1/sessions?mode=daily")
    assert response.status_code == 200 and len(response.json()["items"]) == target


def test_empty_daily_session_is_persisted_and_returned(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions?mode=daily")
    session_id = response.json()["id"]
    repository = provider.for_user("local-user")
    try:
        assert repository.get_session("missing") is None
        stored = repository.get_session(session_id)
    finally:
        repository.close()
    assert response.status_code == 200 and response.json()["items"] == []
    assert stored is not None and stored.words == ()
