from __future__ import annotations

import hashlib
from pathlib import Path

from english_vocab_trainer.ports.audio import AudioMetadata, AudioResult, parse_single_range

__all__ = ["FilesystemAudioStore", "parse_single_range"]


class FilesystemAudioStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        # Only canonical lower-case MP3 paths below the configured root are public.
        if candidate.suffix != ".mp3" or not candidate.is_relative_to(self.root):
            raise FileNotFoundError(key)
        return candidate

    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        candidate = self._path(key)
        metadata = self.head(key)
        selected = parse_single_range(range_header, metadata.size)
        start, end = selected if selected is not None else (0, metadata.size - 1)
        length = max(0, end - start + 1)
        try:
            with candidate.open("rb") as source:
                source.seek(start)
                data = source.read(length)
        except OSError as exc:
            raise FileNotFoundError(key) from exc
        if len(data) != length:
            raise FileNotFoundError(key)
        return AudioResult(
            data,
            metadata.size,
            metadata.etag,
            start,
            end,
            selected is not None,
        )

    def head(self, key: str) -> AudioMetadata:
        candidate = self._path(key)
        try:
            size = candidate.stat().st_size
            with candidate.open("rb") as source:
                etag = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as exc:
            raise FileNotFoundError(key) from exc
        return AudioMetadata(size, etag)
