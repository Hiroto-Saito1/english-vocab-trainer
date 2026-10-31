from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from english_vocab_trainer.domain.models import Review, ReviewAction, replay_card


def test_fsrs_again_has_ten_minute_learning_step() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    due_at = replay_card("one", [Review(uuid4(), "one", ReviewAction.UNKNOWN, now)]).due
    assert 9 * 60 <= (due_at - now).total_seconds() <= 11 * 60


def test_fsrs_good_schedules_a_future_review() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    due_at = replay_card("one", [Review(uuid4(), "one", ReviewAction.KNOWN, now)]).due
    assert due_at > now


def test_replay_is_order_independent() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    good = Review(UUID(int=2), "w", ReviewAction.KNOWN, now)
    again = Review(UUID(int=1), "w", ReviewAction.UNKNOWN, now + timedelta(days=1))
    forward = replay_card("w", [good, again]).due
    reverse = replay_card("w", [again, good]).due
    assert forward == reverse


def test_replay_due_date_is_deterministic_after_fuzz_was_disabled() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reviews = [
        Review(UUID(int=1), "w", ReviewAction.KNOWN, now),
        Review(UUID(int=2), "w", ReviewAction.KNOWN, now + timedelta(days=8)),
    ]
    due_dates = [replay_card("w", reviews).due for _ in range(100)]
    assert set(due_dates) == {due_dates[0]}
