from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AudioResult:
    body: bytes
    size: int
    etag: str
    start: int
    end: int
    partial: bool


class AudioStore(Protocol):
    def get(self, key: str, range_header: str | None = None) -> AudioResult: ...
