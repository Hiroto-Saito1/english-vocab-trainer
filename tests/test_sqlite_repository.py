from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Rating, ReviewEvent, Word


def repo(path: Path, user: str = "alice") -> SQLiteVocabularyRepository:
    return SQLiteVocabularyRepository(path, user)


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
