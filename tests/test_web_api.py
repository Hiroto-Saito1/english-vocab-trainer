from pathlib import Path
from uuid import UUID

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


def test_words_filter_and_transcript_validation(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    for word in (
        Word("nine", "nine", 9, None, "nine.mp3"),
        Word("ten", "ten", 10, None, "ten.mp3"),
        Word("none", "none", None, None, "none.mp3"),
    ):
        repository.add_word(word)
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        assert client.get("/api/v1/words?levels=10&limit=1").json()["items"][0]["id"] == "ten"
        assert (
            client.patch(
                "/api/v1/words/nine/transcript", json={"transcript": "English only."}
            ).status_code
            == 200
        )
        assert (
            client.patch("/api/v1/words/nine/transcript", json={"transcript": "日本語"}).status_code
            == 422
        )
        assert (
            client.patch("/api/v1/words/no/transcript", json={"transcript": "English"}).status_code
            == 404
        )


def test_review_batch_known_retry_is_idempotent(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    event = {"id": str(UUID(int=9)), "word_id": "one", "action": "known", "reviewed_at": "2026-01-01T00:00:00Z"}
    with TestClient(app) as client:
        first = client.post("/api/v1/review-events/batch", json=[event]).json()
        second = client.post("/api/v1/review-events/batch", json=[event]).json()
        later = dict(event, id=str(UUID(int=10)), reviewed_at="2026-01-02T00:00:00Z")
        third = client.post("/api/v1/review-events/batch", json=[later]).json()
        progress = client.get("/api/v1/progress").json()
    assert first["results"][0]["status"] == "applied" and first["results"][0]["rating"] == "easy"
    assert second["results"][0]["status"] == "idempotent" and progress["reviewed"] == 2
    assert third["results"][0]["rating"] == "good"
