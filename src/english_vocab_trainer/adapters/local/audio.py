from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioResult:
    body: bytes
    size: int
    etag: str
    start: int
    end: int
    partial: bool


def parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid range")
    start_text, end_text = value[6:].split("-", 1)
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("invalid range")
        return max(0, size - length), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, min(end, size - 1)


class FilesystemAudioStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        candidate = (self.root / key).resolve()
        if self.root not in candidate.parents or candidate.suffix.lower() != ".mp3":
            raise FileNotFoundError(key)
        data = candidate.read_bytes()
        size = len(data)
        selected = parse_single_range(range_header, size)
        start, end = selected if selected else (0, size - 1)
        return AudioResult(
            data[start : end + 1],
            size,
            hashlib.sha256(data).hexdigest(),
            start,
            end,
            bool(selected),
        )
