from pathlib import Path

import pytest

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore, parse_single_range
from english_vocab_trainer.ingest import parse_audio_path


def test_range_parser() -> None:
    assert parse_single_range("bytes=1-3", 5) == (1, 3)
    assert parse_single_range("bytes=-2", 5) == (3, 4)
    for invalid in ("bytes=9-", "bytes=1-2,3-4", "items=1-2", "bytes=-0", "bytes=a-2"):
        with pytest.raises(ValueError):
            parse_single_range(invalid, 5)


def test_audio_store_blocks_traversal(tmp_path: Path) -> None:
    (tmp_path / "x.mp3").write_bytes(b"abcde")
    result = FilesystemAudioStore(tmp_path).get("x.mp3", "bytes=1-3")
    assert result.body == b"bcd" and result.partial
    with pytest.raises(FileNotFoundError):
        FilesystemAudioStore(tmp_path).get("../x.mp3")
    with pytest.raises(FileNotFoundError):
        FilesystemAudioStore(tmp_path).get("x.MP3")


@pytest.mark.parametrize(
    ("filename", "level", "term"),
    [
        ("123 STAGE 08　0723 mobilize.mp3", 9, "mobilize"),
        ("STAGE 02　0193 painstaking.mp3", 9, "painstaking"),
        ("STAGE 02　0193 take care of.mp3", None, "take care of"),
    ],
)
def test_parse_real_filename_shapes(
    tmp_path: Path, filename: str, level: int | None, term: str
) -> None:
    root = tmp_path
    path = root / "上級SVL" / (str(level) if level else "unknown") / filename
    path.parent.mkdir(parents=True)
    path.touch()
    assert parse_audio_path(root, path) == (level, term)
