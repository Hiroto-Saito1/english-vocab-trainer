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


def test_service_worker_is_root_scoped(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    with TestClient(app) as client:
        response = client.get("/sw.js")
    assert (
        response.status_code == 200
        and response.headers["service-worker-allowed"] == "/"
        and "cachedAudioRange" in response.text
    )


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
                "/api/v1/words/nine/transcript",
                json={"transcript": "An English definition example."},
            ).status_code
            == 200
        )
        assert (
            client.patch("/api/v1/words/nine/transcript", json={"transcript": "日本語"}).status_code
            == 422
        )
        assert (
            client.patch(
                "/api/v1/words/no/transcript",
                json={"transcript": "An English definition example."},
            ).status_code
            == 404
        )


def test_review_batch_known_retry_is_idempotent(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    event = {
        "id": str(UUID(int=9)),
        "word_id": "one",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    with TestClient(app) as client:
        first = client.post("/api/v1/review-events/batch", json=[event]).json()
        second = client.post("/api/v1/review-events/batch", json=[event]).json()
        later = dict(event, id=str(UUID(int=10)), reviewed_at="2026-01-02T00:00:00Z")
        third = client.post("/api/v1/review-events/batch", json=[later]).json()
        unknown = dict(
            later, id=str(UUID(int=11)), action="unknown", reviewed_at="2026-01-03T00:00:00Z"
        )
        fourth = client.post("/api/v1/review-events/batch", json=[unknown]).json()
        progress = client.get("/api/v1/progress").json()
    assert first["results"][0]["status"] == "applied" and first["results"][0]["rating"] == "easy"
    assert second["results"][0]["status"] == "idempotent" and progress["reviewed"] == 3
    assert third["results"][0]["rating"] == "good"
    assert fourth["results"][0]["rating"] == "again"


def test_review_batch_reports_missing_and_conflict(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    event = {
        "id": str(UUID(int=12)),
        "word_id": "one",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    with TestClient(app) as client:
        missing = client.post(
            "/api/v1/review-events/batch", json=[dict(event, word_id="missing")]
        ).json()
        client.post("/api/v1/review-events/batch", json=[event])
        conflict = client.post(
            "/api/v1/review-events/batch", json=[dict(event, action="unknown")]
        ).json()
    assert missing["results"][0]["status"] == "missing" and missing["acknowledged"] == []
    assert conflict["results"][0]["status"] == "conflict" and conflict["acknowledged"] == []


def test_void_review_is_idempotent_and_missing_is_404(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "one.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
    event_id = UUID(int=13)
    event = {
        "id": str(event_id),
        "word_id": "one",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    with TestClient(app) as client:
        client.post("/api/v1/review-events/batch", json=[event])
        first = client.post(f"/api/v1/review-events/{event_id}/void").json()
        second = client.post(f"/api/v1/review-events/{event_id}/void").json()
        progress = client.get("/api/v1/progress").json()
        missing = client.post(f"/api/v1/review-events/{UUID(int=99)}/void")
    assert (
        first["version"] == second["version"]
        and progress["reviewed"] == 0
        and missing.status_code == 404
    )
