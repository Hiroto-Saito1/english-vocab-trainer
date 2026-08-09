from random import Random

from english_vocab_trainer.application.services import shuffle_within_level_bands
from english_vocab_trainer.domain.models import Word


def test_shuffle_keeps_level_bands_and_none_last() -> None:
    words = [
        Word("a", "a", 10, None, "a"),
        Word("b", "b", 9, None, "b"),
        Word("c", "c", 9, None, "c"),
        Word("d", "d", None, None, "d"),
    ]
    result = shuffle_within_level_bands(words, Random(7))
    assert [word.level for word in result] == [9, 9, 10, None]
    assert words[0].id == "a" and {word.id for word in result} == {word.id for word in words}


def test_shuffle_is_seed_deterministic() -> None:
    words = [Word(str(i), str(i), 9, None, str(i)) for i in range(5)]
    assert [w.id for w in shuffle_within_level_bands(words, Random(3))] == [
        w.id for w in shuffle_within_level_bands(words, Random(3))
    ]
