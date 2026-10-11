from __future__ import annotations

from datetime import UTC, datetime
from random import Random
from typing import cast

from english_vocab_trainer.application.services import select_set_words
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.ports.repositories import VocabularyRepository


def words(count: int) -> list[Word]:
    return [
        Word(
            str(index),
            f"term {index}",
            index % 4,
            None,
            f"{index}.mp3",
            Tier.UPPER if index % 2 == 0 else Tier.ULTRA,
        )
        for index in range(count)
    ]


class SetRepository:
    def __init__(self, candidates: list[Word], due: list[Word]) -> None:
        self.candidates = candidates
        self.due = due

    def list_words(self, *, limit: int = 100) -> list[Word]:
        return self.candidates[:limit]

    def due_words(self, now: datetime, limit: int) -> list[Word]:
        assert now.tzinfo is UTC
        return self.due[:limit]


def select(repository: SetRepository, *, seed: int = 1) -> list[Word]:
    return select_set_words(
        cast(VocabularyRepository, repository),
        datetime(2026, 1, 1, tzinfo=UTC),
        Random(seed),
    )


def test_set_uses_thirty_as_total_cap_and_admits_due_first() -> None:
    candidates = words(200)
    due = candidates[:12]
    result = select(SetRepository(candidates, due), seed=7)
    assert len(result) == 30
    assert {word.id for word in due}.issubset({word.id for word in result})
    assert len({word.id for word in result}) == len(result)


def test_set_shortage_and_due_backlog_never_duplicate_or_exceed_target() -> None:
    shortage = select(SetRepository(words(3), []))
    backlog = select(SetRepository(words(200), words(150)))
    assert len(shortage) == 3 and len({word.id for word in shortage}) == 3
    assert {word.id for word in backlog} == {str(index) for index in range(30)}


def test_presentation_order_is_random_and_seedable_after_admission() -> None:
    candidates = words(100)
    first = select(SetRepository(candidates, []), seed=1)
    same_seed = select(SetRepository(candidates, []), seed=1)
    other_seed = select(SetRepository(candidates, []), seed=2)
    assert [word.id for word in first] == [word.id for word in same_seed]
    assert [word.id for word in first] != [word.id for word in other_seed]
    assert first != sorted(first, key=lambda word: (word.level is None, word.level or 0, word.id))


def test_new_admission_uses_lowest_non_null_levels_and_null_last() -> None:
    candidates = [
        Word(str(index), f"term {index}", level, None, f"{index}.mp3", Tier.UPPER)
        for index, level in enumerate([None, 8, 2, 5, None, 1] * 10)
    ]
    selected = select(SetRepository(list(reversed(candidates)), []), seed=11)
    admitted = {word.id for word in selected}
    expected = {
        word.id
        for word in sorted(
            candidates,
            key=lambda word: (
                word.level is None,
                word.level if word.level is not None else 0,
                word.id,
            ),
        )[:30]
    }
    assert admitted == expected
    assert all(word.level is not None for word in selected)
