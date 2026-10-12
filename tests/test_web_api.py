from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Review, ReviewAction, Word
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def word(identifier: str = "one", level: int | None = 1) -> Word:
    return Word(
        identifier,
        identifier,
        level,
        f"Definition for {identifier}.",
        f"{identifier}.mp3",
    )


def client(tmp_path: Path, words: list[Word] | None = None) -> TestClient:
    database = tmp_path / "v.db"
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(words or [])
    return TestClient(create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test")))


def payload(review_id: int, action: str = "known", word_id: str = "one") -> dict[str, str]:
    return {
        "id": str(UUID(int=review_id)),
        "word_id": word_id,
        "action": action,
    }


def test_card_returns_one_due_or_lowest_level_unseen(tmp_path: Path) -> None:
    words = [word("high", 9), word("low", 1)]
    with client(tmp_path, words) as app:
        first = app.get("/api/v1/card")
        app.post("/api/v1/reviews", json=payload(1, word_id="low"))
        second = app.get("/api/v1/card")
    assert first.json() == {
        "item": {
            "word_id": "low",
            "term": "low",
            "definition": "Definition for low.",
            "audio_url": "/api/v1/audio/low",
        },
        "next_due": None,
    }
    assert second.json()["item"]["word_id"] == "high"


def test_empty_card_returns_only_optional_item_and_next_due(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        assert app.get("/api/v1/card").json() == {"item": None, "next_due": None}
    with client(tmp_path, [word()]) as app:
        assert app.post("/api/v1/reviews", json=payload(2)).status_code == 204
        future = app.get("/api/v1/card").json()
    assert future["item"] is None and future["next_due"] is not None


def test_review_retry_and_correction_keep_server_timestamp(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    submitted = payload(9)
    before = datetime.now(UTC)
    with client(tmp_path, [word()]) as app:
        first = app.post("/api/v1/reviews", json=submitted)
        retry = app.post("/api/v1/reviews", json=submitted)
        corrected = app.post("/api/v1/reviews", json={**submitted, "action": "unknown"})
    after = datetime.now(UTC)
    with SQLiteVocabularyRepository(database) as repository:
        rows = repository.db.execute("SELECT id,action,reviewed_at FROM reviews").fetchall()
    assert first.status_code == retry.status_code == corrected.status_code == 204
    assert len(rows) == 1
    assert tuple(rows[0][:2]) == (str(UUID(int=9)), "unknown")
    assert before <= datetime.fromisoformat(rows[0]["reviewed_at"]) <= after


def test_review_rejects_missing_word_and_identity_conflict(tmp_path: Path) -> None:
    with client(tmp_path, [word(), word("two")]) as app:
        assert app.post("/api/v1/reviews", json=payload(12, word_id="missing")).status_code == 404
        assert app.post("/api/v1/reviews", json=payload(12)).status_code == 204
        assert app.post("/api/v1/reviews", json=payload(12, word_id="two")).status_code == 409


def test_history_uses_stable_cursor_and_returns_word_display(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    words = [word(str(index), index) for index in range(3)]
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(words)
        for index in range(3):
            repository.save_review(
                Review(UUID(int=index + 1), str(index), ReviewAction.KNOWN, instant)
            )
    with client(tmp_path, words) as app:
        first = app.get("/api/v1/history", params={"limit": 2}).json()
        second = app.get(
            "/api/v1/history", params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        invalid = app.get("/api/v1/history", params={"cursor": "*"})
    assert [item["review_id"] for item in first["items"]] == [
        str(UUID(int=3)),
        str(UUID(int=2)),
    ]
    assert first["items"][0]["word_id"] == "2"
    assert first["items"][0]["definition"] == "Definition for 2."
    assert [item["review_id"] for item in second["items"]] == [str(UUID(int=1))]
    assert second["next_cursor"] is None and invalid.status_code == 422


def test_due_card_is_selected_before_unseen(tmp_path: Path) -> None:
    database = tmp_path / "v.db"
    with client(tmp_path, [word("reviewed", 9), word("new", 1)]) as app:
        app.post("/api/v1/reviews", json=payload(22, word_id="reviewed"))
        with SQLiteVocabularyRepository(database) as repository:
            repository.db.execute(
                "UPDATE word_state SET due_at=? WHERE word_id='reviewed'",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        selected = app.get("/api/v1/card").json()["item"]
    assert selected["word_id"] == "reviewed"
