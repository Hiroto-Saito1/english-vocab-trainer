from datetime import UTC, datetime
from uuid import uuid4

from english_vocab_trainer.domain.models import (
    Rating,
    ReviewAction,
    ReviewEvent,
    WordState,
    next_state,
    rating_for_action,
)


def test_fsrs_again_has_ten_minute_learning_step() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = next_state(WordState("one", now), ReviewEvent(uuid4(), "one", Rating.AGAIN, now))
    assert 9 * 60 <= (state.due_at - now).total_seconds() <= 11 * 60
    assert state.card_json is not None


def test_first_known_is_preserved() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = WordState("one", now)
    state = next_state(state, ReviewEvent(uuid4(), "one", Rating.EASY, now))
    assert state.first_seen_at == state.first_known_at == now
    assert state.stability > 0


def test_action_rating_mapping() -> None:
    assert rating_for_action(ReviewAction.UNKNOWN, False) is Rating.AGAIN
    assert rating_for_action(ReviewAction.KNOWN, False) is Rating.EASY
    assert rating_for_action(ReviewAction.KNOWN, True) is Rating.GOOD
