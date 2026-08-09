from datetime import UTC, datetime, timedelta
from uuid import UUID

from english_vocab_trainer.domain.models import Rating, ReviewEvent, replay_word_state


def test_replay_is_order_independent_and_voidable() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    easy = ReviewEvent(UUID(int=2), "w", Rating.EASY, now)
    again = ReviewEvent(UUID(int=1), "w", Rating.AGAIN, now + timedelta(days=1))
    forward = replay_word_state("w", [easy, again])
    reverse = replay_word_state("w", [again, easy])
    assert forward.card_json == reverse.card_json
    voided = replay_word_state(
        "w", [easy, ReviewEvent(again.id, "w", Rating.AGAIN, again.reviewed_at, now)]
    )
    assert voided.last_known_at == now


def test_replay_is_byte_for_byte_deterministic_after_fuzz_was_disabled() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        ReviewEvent(UUID(int=1), "w", Rating.EASY, now),
        ReviewEvent(UUID(int=2), "w", Rating.EASY, now + timedelta(days=8)),
    ]
    states = [replay_word_state("w", events) for _ in range(100)]
    assert {(state.card_json, state.due_at) for state in states} == {
        (states[0].card_json, states[0].due_at)
    }


def test_out_of_order_and_void_replay_stay_deterministic() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = ReviewEvent(UUID(int=1), "w", Rating.EASY, now)
    second = ReviewEvent(UUID(int=2), "w", Rating.GOOD, now + timedelta(days=8))
    expected = replay_word_state("w", [first, second])
    assert replay_word_state("w", [second, first]).card_json == expected.card_json
    voided = ReviewEvent(second.id, "w", second.rating, second.reviewed_at, now)
    states = [replay_word_state("w", [first, voided]) for _ in range(100)]
    assert {(state.card_json, state.due_at) for state in states} == {
        (states[0].card_json, states[0].due_at)
    }
