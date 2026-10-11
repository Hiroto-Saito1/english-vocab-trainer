from datetime import UTC, datetime
from uuid import uuid4

from english_vocab_trainer.domain.models import (
    Rating,
    ReviewAction,
    ReviewEvent,
    rating_for_action,
    replay_word_state,
)


def test_fsrs_again_has_ten_minute_learning_step() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = replay_word_state("one", [ReviewEvent(uuid4(), "one", Rating.AGAIN, now)])
    assert 9 * 60 <= (state.due_at - now).total_seconds() <= 11 * 60


def test_fsrs_easy_schedules_a_future_review() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = replay_word_state("one", [ReviewEvent(uuid4(), "one", Rating.EASY, now)])
    assert state.due_at > now


def test_action_rating_mapping() -> None:
    assert rating_for_action(ReviewAction.UNKNOWN, False) is Rating.AGAIN
    assert rating_for_action(ReviewAction.KNOWN, False) is Rating.EASY
    assert rating_for_action(ReviewAction.KNOWN, True) is Rating.GOOD
