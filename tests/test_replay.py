from datetime import UTC, datetime, timedelta
from uuid import UUID

from english_vocab_trainer.domain.models import Rating, ReviewEvent, replay_word_state


def test_replay_is_order_independent_and_voidable() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    easy = ReviewEvent(UUID(int=2), "w", Rating.EASY, now)
    again = ReviewEvent(UUID(int=1), "w", Rating.AGAIN, now + timedelta(days=1))
    forward = replay_word_state("w", [easy, again])
    reverse = replay_word_state("w", [again, easy])
    assert forward.due_at == reverse.due_at
    voided = replay_word_state(
        "w", [easy, ReviewEvent(again.id, "w", Rating.AGAIN, again.reviewed_at, now)]
    )
    assert voided.due_at == replay_word_state("w", [easy]).due_at


def test_replay_due_date_is_deterministic_after_fuzz_was_disabled() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        ReviewEvent(UUID(int=1), "w", Rating.EASY, now),
        ReviewEvent(UUID(int=2), "w", Rating.GOOD, now + timedelta(days=8)),
    ]
    states = [replay_word_state("w", events) for _ in range(100)]
    assert {state.due_at for state in states} == {states[0].due_at}
