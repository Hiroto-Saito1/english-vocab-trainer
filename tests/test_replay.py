from datetime import UTC, datetime, timedelta
from uuid import UUID

from english_vocab_trainer.domain.models import Review, ReviewAction, replay_word_state


def test_replay_is_order_independent() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    good = Review(UUID(int=2), "w", ReviewAction.KNOWN, now)
    again = Review(UUID(int=1), "w", ReviewAction.UNKNOWN, now + timedelta(days=1))
    forward = replay_word_state("w", [good, again])
    reverse = replay_word_state("w", [again, good])
    assert forward.due_at == reverse.due_at


def test_replay_due_date_is_deterministic_after_fuzz_was_disabled() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reviews = [
        Review(UUID(int=1), "w", ReviewAction.KNOWN, now),
        Review(UUID(int=2), "w", ReviewAction.KNOWN, now + timedelta(days=8)),
    ]
    states = [replay_word_state("w", reviews) for _ in range(100)]
    assert {state.due_at for state in states} == {states[0].due_at}
