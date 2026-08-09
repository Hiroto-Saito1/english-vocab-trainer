from pathlib import Path


def test_repository_has_no_audio_or_private_catalog() -> None:
    root = Path(__file__).parents[1]
    forbidden = [*root.rglob("*.mp3"), *root.rglob("*.wav"), *root.rglob("*.m4a")]
    forbidden += [path for path in root.rglob("catalog*") if path.is_file()]
    assert forbidden == []
