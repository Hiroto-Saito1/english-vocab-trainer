from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def _client(tmp_path: Path, words: tuple[Word, ...] = ()) -> TestClient:
    database = tmp_path / "v.db"
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(list(words))
    return TestClient(create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test")))


def word() -> Word:
    return Word("one", "one", 9, None, "one.mp3", Tier.UPPER)


def test_service_worker_is_root_scoped(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/sw.js")
    assert (
        response.status_code == 200
        and response.headers["service-worker-allowed"] == "/"
        and "cachedAudioRange" in response.text
    )


def test_review_batch_known_retry_is_idempotent(tmp_path: Path) -> None:
    event = {
        "id": str(UUID(int=9)),
        "word_id": "one",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    database = tmp_path / "v.db"
    with _client(tmp_path, (word(),)) as client:
        first = client.post("/api/v1/review-events/batch", json=[event]).json()
        second = client.post("/api/v1/review-events/batch", json=[event]).json()
        later = dict(event, id=str(UUID(int=10)), reviewed_at="2026-01-02T00:00:00Z")
        third = client.post("/api/v1/review-events/batch", json=[later]).json()
        unknown = dict(
            later, id=str(UUID(int=11)), action="unknown", reviewed_at="2026-01-03T00:00:00Z"
        )
        fourth = client.post("/api/v1/review-events/batch", json=[unknown]).json()
    with SQLiteVocabularyRepository(database) as repository:
        assert sum(repository.get_event(UUID(int=value)) is not None for value in (9, 10, 11)) == 3
    assert (
        first["results"][0]["status"] == "applied"
        and first["results"][0]["rating"] == "easy"
        and first["results"][0]["due_at"] is not None
        and first["results"][0]["version"] == 1
    )
    assert second["results"][0]["status"] == "idempotent"
    assert third["results"][0]["rating"] == "good"
    assert fourth["results"][0]["rating"] == "again"


def test_review_batch_reports_missing_and_conflict(tmp_path: Path) -> None:
    event = {
        "id": str(UUID(int=12)),
        "word_id": "one",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    with _client(tmp_path, (word(),)) as client:
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
    event_id = UUID(int=13)
    event = {
        "id": str(event_id),
        "word_id": "one",
        "action": "known",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    database = tmp_path / "v.db"
    with _client(tmp_path, (word(),)) as client:
        client.post("/api/v1/review-events/batch", json=[event])
        first = client.post(f"/api/v1/review-events/{event_id}/void").json()
        second = client.post(f"/api/v1/review-events/{event_id}/void").json()
        missing = client.post(f"/api/v1/review-events/{UUID(int=99)}/void")
    with SQLiteVocabularyRepository(database) as repository:
        stored = repository.get_event(event_id)
    assert first["version"] == second["version"] and stored is not None
    assert stored.voided_at is not None and missing.status_code == 404
