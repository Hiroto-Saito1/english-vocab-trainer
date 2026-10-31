"""Cloudflare Workers R2 binding adapter for private content-addressed audio."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from english_vocab_trainer.ports.audio import (
    AudioMetadata,
    AudioResult,
    AudioStorageError,
    parse_single_range,
)

_KEY = re.compile(r"audio/([0-9a-f]{64})\.mp3\Z")
MAX_AUDIO_BYTES = 1_048_576


def _identity(key: str) -> str:
    match = _KEY.fullmatch(key)
    if match is None:
        raise FileNotFoundError("invalid audio key")
    return match.group(1)


def _metadata(obj: Any, digest: str) -> AudioMetadata:
    size = obj.size
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_AUDIO_BYTES:
        raise AudioStorageError("invalid private audio object size")
    return AudioMetadata(size, digest)


def _bytes(buffer: Any) -> bytes:
    if isinstance(buffer, (bytes, bytearray, memoryview)):
        return bytes(buffer)
    # A JS ArrayBuffer is not itself iterable in Pyodide; expose it as Uint8Array.
    from js import Uint8Array

    return bytes(Uint8Array.new(buffer).to_py())


class WorkerR2AudioStore:
    def __init__(self, binding: Any) -> None:
        self._binding = binding

    async def head(self, key: str) -> AudioMetadata:
        digest = _identity(key)
        try:
            obj = await self._binding.head(key)
            if obj is None:
                raise FileNotFoundError(key)
            return _metadata(obj, digest)
        except (FileNotFoundError, AudioStorageError):
            raise
        except Exception as exc:
            raise AudioStorageError("private audio storage is unavailable") from exc

    async def get(self, key: str, range_header: str | None = None) -> AudioResult:
        digest = _identity(key)
        try:
            obj = await self._binding.get(key)
            if obj is None:
                raise FileNotFoundError(key)
            metadata = _metadata(obj, digest)
            selected = parse_single_range(range_header, metadata.size)
            data = _bytes(await obj.arrayBuffer())
            if len(data) != metadata.size or hashlib.sha256(data).hexdigest() != digest:
                raise AudioStorageError("private audio object failed integrity verification")
            start, end = selected if selected is not None else (0, metadata.size - 1)
            return AudioResult(
                data[start : end + 1], metadata.size, digest, start, end, selected is not None
            )
        except (FileNotFoundError, AudioStorageError, ValueError):
            raise
        except Exception as exc:
            raise AudioStorageError("private audio storage is unavailable") from exc
