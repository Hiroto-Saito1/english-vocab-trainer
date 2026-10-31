from __future__ import annotations

import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

MAX_AUDIO_BYTES = 1_048_576
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_R2_KEY = re.compile(r"audio/([0-9a-f]{64})\.mp3\Z")


def valid_audio_digest(value: str) -> bool:
    return _SHA256.fullmatch(value) is not None


def r2_audio_key(digest: str) -> str:
    if not valid_audio_digest(digest):
        raise ValueError("checksum must be a lowercase SHA-256 hex digest")
    return f"audio/{digest}.mp3"


def r2_audio_digest(key: str) -> str:
    match = _R2_KEY.fullmatch(key)
    if match is None:
        raise FileNotFoundError("invalid audio key")
    return match.group(1)


@dataclass(frozen=True, slots=True)
class AudioResult:
    body: bytes
    size: int
    etag: str
    start: int
    end: int
    partial: bool


@dataclass(frozen=True, slots=True)
class AudioMetadata:
    size: int
    etag: str


class InvalidRangeError(ValueError):
    """A request did not contain one satisfiable byte range."""


class AudioStorageError(RuntimeError):
    """The private audio store cannot safely serve an object."""


def parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range without accepting multipart ranges."""
    if value is None:
        return None
    if size < 0 or not value.startswith("bytes=") or "," in value:
        raise InvalidRangeError("invalid byte range")
    spec = value.removeprefix("bytes=")
    if spec.count("-") != 1:
        raise InvalidRangeError("invalid byte range")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            length = int(end_text)
            if length <= 0 or size == 0:
                raise InvalidRangeError("invalid byte range")
            return max(0, size - length), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise InvalidRangeError("invalid byte range") from exc
    if start < 0 or start >= size or end < start:
        raise InvalidRangeError("invalid byte range")
    return start, min(end, size - 1)


class AudioStore(Protocol):
    def head(self, key: str) -> AudioMetadata | Awaitable[AudioMetadata]: ...
    def get(
        self, key: str, range_header: str | None = None
    ) -> AudioResult | Awaitable[AudioResult]: ...
