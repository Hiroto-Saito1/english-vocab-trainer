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
