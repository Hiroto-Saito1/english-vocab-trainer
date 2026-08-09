import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from english_vocab_trainer.adapters.local.sqlite import (
    ConflictError,
    MissingError,
    SQLiteVocabularyRepository,
)
from english_vocab_trainer.application.services import submit_review
from english_vocab_trainer.domain.models import Rating, ReviewAction, ReviewEvent, Word

_OPEN: list[SQLiteVocabularyRepository] = []


@pytest.fixture(autouse=True)
def close_repositories() -> object:
    yield
    for repository in _OPEN:
        repository.close()
    _OPEN.clear()


def repo(path: Path, user: str = "alice") -> SQLiteVocabularyRepository:
    repository = SQLiteVocabularyRepository(path, user)
    _OPEN.append(repository)
    return repository


def words(r: SQLiteVocabularyRepository) -> None:
    r.add_word(Word("nine", "nine", 9, None, "nine.mp3"))
    r.add_word(Word("ten", "ten", 10, None, "ten.mp3"))
    r.add_word(Word("unknown", "unknown", None, None, "unknown.mp3"))


def event(word: str, when: datetime) -> ReviewEvent:
    return ReviewEvent(uuid4(), word, Rating.EASY, when)


def test_reopen_persists_word_state_and_event(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    now = datetime.now(UTC)
    with repo(path) as r:
        words(r)
        r.append_event(event("nine", now), 0)
    with repo(path) as r:
        assert r.get_word("nine") is not None and r.state("nine").version == 1


def test_two_users_state_and_progress_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    a = repo(path)
    words(a)
    a.append_event(event("nine", datetime.now(UTC)), 0)
    b = repo(path, "bob")
    assert (
        a.progress(datetime.now(UTC))["reviewed"] == 1
        and b.progress(datetime.now(UTC))["reviewed"] == 0
    )


def test_levels_filter_and_null_last(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    words(r)
    assert [w.id for w in r.list_words(levels=[10])] == ["ten"]
    assert [w.id for w in r.list_words()] == ["nine", "ten", "unknown"]


def test_active_review_excludes_and_void_reincludes_new(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    words(r)
    e = event("nine", datetime.now(UTC))
    r.append_event(e, 0)
    assert "nine" not in [w.id for w in r.list_words()]
    r.void_event(e.id)
    assert "nine" in [w.id for w in r.list_words()]
    assert not r.has_active_review("nine")


def test_active_review_is_user_scoped(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    alice = repo(path)
    words(alice)
    assert not alice.has_active_review("nine")
    alice.append_event(event("nine", datetime.now(UTC)), 0)
    assert alice.has_active_review("nine") and not repo(path, "bob").has_active_review("nine")


def test_due_is_current_user_and_past_only(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    a = repo(path)
    words(a)
    now = datetime.now(UTC)
    a.append_event(event("nine", now), 0)
    assert not a.due_words(now, 10)
    a.db.execute(
        "UPDATE user_word_state SET due_at=? WHERE user_id=?",
        ((now - timedelta(minutes=1)).isoformat(), "alice"),
    )
    assert [w.id for w in a.due_words(now, 10)] == ["nine"] and not repo(path, "bob").due_words(
        now, 10
    )


def test_settings_default_validation_persistence_and_isolation(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    alice = repo(path)
    bob = repo(path, "bob")
    assert alice.get_settings().daily_target == 30
    assert alice.update_settings(17).daily_target == 17
    assert bob.get_settings().daily_target == 30
    with pytest.raises(ValueError):
        alice.update_settings(0)
    assert repo(path).get_settings().daily_target == 17


def test_transcript_update_persists_and_missing_raises(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    r = repo(path)
    words(r)
    assert r.update_transcript("nine", "An English sentence.").transcript == "An English sentence."
    stored = repo(path).get_word("nine")
    assert stored is not None and stored.transcript == "An English sentence."
    with pytest.raises(MissingError):
        r.update_transcript("absent", "Nope")


def test_progress_counts_active_reviews_due_and_user_scope(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    a = repo(path)
    words(a)
    now = datetime.now(UTC)
    e = event("nine", now)
    a.append_event(e, 0)
    a.db.execute(
        "UPDATE user_word_state SET due_at=? WHERE user_id=?",
        ((now - timedelta(seconds=1)).isoformat(), "alice"),
    )
    assert a.progress(now) == {"total": 3, "due": 1, "reviewed": 1}
    assert repo(path, "bob").progress(now) == {"total": 3, "due": 0, "reviewed": 0}
    a.void_event(e.id)
    assert a.progress(now)["reviewed"] == 0 and a.progress(now)["due"] == 1


def test_schema_rejects_invalid_checks(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO user_settings VALUES('x',101)")
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO study_sessions VALUES('x','x','bad','now')")
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO review_events VALUES('x','x','no','bad','now',NULL,'x')")


def test_schema_rejects_missing_foreign_key(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO session_items VALUES('none','none',0)")


def test_schema_rejects_duplicate_session_ordinal(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    words(r)
    r.db.execute("INSERT INTO study_sessions VALUES('s','alice','daily','now')")
    r.db.execute("INSERT INTO session_items VALUES('s','nine',0)")
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO session_items VALUES('s','ten',0)")


def test_submit_known_retry_is_easy_and_idempotent(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    words(r)
    event_id, now = uuid4(), datetime.now(UTC)
    first = submit_review(r, event_id, "nine", ReviewAction.KNOWN, now)
    second = submit_review(r, event_id, "nine", ReviewAction.KNOWN, now)
    assert first.rating is second.rating is Rating.EASY
    assert first.state.version == second.state.version == 1
    assert r.progress(now)["reviewed"] == 1


def test_missing_word_keeps_transaction_usable(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    with pytest.raises(MissingError):
        r.append_event(event("missing", datetime.now(UTC)), 0)
    words(r)
    r.append_event(event("nine", datetime.now(UTC)), 0)
    r.close()


def test_stale_cas_has_no_partial_event(tmp_path: Path) -> None:
    r = repo(tmp_path / "v.db")
    words(r)
    r.append_event(event("nine", datetime.now(UTC)), 0)
    with pytest.raises(ConflictError):
        r.append_event(event("nine", datetime.now(UTC)), 0)
    assert r.progress(datetime.now(UTC))["reviewed"] == 1
    r.close()


def test_uuid_idempotency_and_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    r = repo(path)
    words(r)
    e = event("nine", datetime.now(UTC))
    one = r.append_event(e, 0)
    two = r.append_event(e, 99)
    assert one.version == two.version
    with pytest.raises(ConflictError):
        r.append_event(ReviewEvent(e.id, "ten", Rating.EASY, e.reviewed_at), 0)
    with pytest.raises(ConflictError):
        repo(path, "bob").append_event(e, 0)
    r.close()


def test_out_of_order_replay_and_double_void(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    now = datetime.now(UTC)
    first = event("nine", now)
    second = ReviewEvent(uuid4(), "nine", Rating.AGAIN, now + timedelta(days=1))
    a = repo(path)
    words(a)
    a.append_event(second, 0)
    state = a.append_event(first, 1)
    b = repo(tmp_path / "other.db")
    words(b)
    b.append_event(first, 0)
    expected = b.append_event(second, 1)
    assert state.card_json == expected.card_json and state.due_at == expected.due_at
    voided = a.void_event(second.id)
    repeated = a.void_event(second.id)
    assert voided.card_json == repeated.card_json and voided.version == repeated.version
