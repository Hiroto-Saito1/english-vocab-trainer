import subprocess
from pathlib import Path


def test_repository_has_no_audio_or_private_catalog() -> None:
    root = Path(__file__).parents[1]
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    forbidden = [
        path
        for path in tracked
        if path.endswith((".mp3", ".wav", ".m4a")) or Path(path).name.startswith("catalog")
    ]
    assert forbidden == []
