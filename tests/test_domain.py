from datetime import UTC, datetime
from uuid import uuid4

from english_vocab_trainer.domain.models import Review, ReviewAction, replay_word_state


def test_fsrs_again_has_ten_minute_learning_step() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = replay_word_state("one", [Review(uuid4(), "one", ReviewAction.UNKNOWN, now)])
    assert 9 * 60 <= (state.due_at - now).total_seconds() <= 11 * 60


def test_fsrs_good_schedules_a_future_review() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = replay_word_state("one", [Review(uuid4(), "one", ReviewAction.KNOWN, now)])
    assert state.due_at > now
