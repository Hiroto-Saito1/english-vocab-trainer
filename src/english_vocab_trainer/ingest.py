from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

_TERM = re.compile(r"(?:^|[ \u3000])\d{4}[ \u3000]+(.+?)\.mp3$", re.IGNORECASE)


def parse_audio_path(root: Path, path: Path) -> tuple[int | None, str]:
    """Derive level and term from approved filenames, never CSV metadata."""
    relative = path.relative_to(root)
    level_part = relative.parts[1]  # approved tree / level / file
    level = int(level_part) if level_part.isdecimal() else None
    match = _TERM.search(path.name)
    if not match:
        raise ValueError(f"unrecognised audio filename: {path.name}")
    return level, match.group(1).strip()


def eligible_audio(root: Path) -> list[Path]:
    """Only approved SVL trees; duplicate folders and CSV data are never read."""
    return sorted(
        path for name in ("上級SVL", "超上級SVL") for path in (root / name).rglob("*.mp3")
    )


def checksum(path: Path) -> str:
    with path.open("rb") as audio:
        return hashlib.file_digest(audio, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["scan", "transcribe", "validate", "upload-audio", "emit-d1"]
    )
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    files = eligible_audio(args.source)
    if args.command == "scan":
        print(f"{len(files)} eligible MP3 files")
    elif args.command == "validate":
        if len(files) != 2000 or len({checksum(path) for path in files}) != len(files):
            raise SystemExit("expected 2,000 unique audio files")
        print("valid: 2,000 unique audio files")
    else:
        print(f"{args.command}: private manifest output only; {len(files)} candidates")
