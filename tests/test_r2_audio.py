from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from english_vocab_trainer.adapters.r2 import Boto3R2AudioStore
from english_vocab_trainer.ports.audio import AudioStorageError, InvalidRangeError

SHA = "a" * 64
KEY = f"audio/{SHA}.mp3"


class Body:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        return self.value if amt is None else self.value[:amt]

    def close(self) -> None:
        self.closed = True


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response: dict[str, object] = {"Error": {"Code": code}}


class FakeR2:
    def __init__(self, value: bytes = b"abcde") -> None:
        self.value = value
        self.head_calls = 0
        self.get_calls: list[dict[str, str]] = []
        self.body: Body | None = None
        self.error: Exception | None = None
        self.get_error: Exception | None = None

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
        self.head_calls += 1
        if self.error:
            raise self.error
        return {"ContentLength": len(self.value), "Metadata": {"sha256": SHA}}

    def get_object(self, **kwargs: str) -> Mapping[str, Any]:
        self.get_calls.append(kwargs)
        if self.get_error:
            raise self.get_error
        requested = kwargs.get("Range")
        if requested is None:
            start, end = 0, len(self.value) - 1
            response: dict[str, Any] = {}
        else:
            start_text, end_text = requested.removeprefix("bytes=").split("-", 1)
            start, end = int(start_text), int(end_text)
            response = {"ContentRange": f"bytes {start}-{end}/{len(self.value)}"}
        self.body = Body(self.value[start : end + 1])
        return {
            **response,
            "Body": self.body,
            "ContentLength": len(self.body.value),
            "Metadata": {"sha256": SHA},
        }


def test_r2_head_never_gets_and_get_closes_body() -> None:
    client = FakeR2()
    store = Boto3R2AudioStore(client, "private-audio")

    assert store.head(KEY).size == 5
    assert client.head_calls == 1 and client.get_calls == []

    result = store.get(KEY, "bytes=-2")
    assert result.body == b"de" and result.start == 3 and result.end == 4
    assert client.get_calls[-1]["Range"] == "bytes=3-4"
    assert client.body is not None and client.body.closed


def test_r2_rejects_bad_keys_and_preserves_private_errors() -> None:
    client = FakeR2()
    store = Boto3R2AudioStore(client, "private-audio")
    with pytest.raises(FileNotFoundError):
        store.head("audio/UPPER.mp3")

    client.error = ClientError("NoSuchKey")
    with pytest.raises(FileNotFoundError):
        store.head(KEY)

    client.error = ClientError("AccessDenied")
    with pytest.raises(AudioStorageError, match="private audio storage is unavailable"):
        store.head(KEY)


def test_r2_range_and_corrupt_response_are_rejected() -> None:
    client = FakeR2()
    store = Boto3R2AudioStore(client, "private-audio")
    with pytest.raises(InvalidRangeError):
        store.get(KEY, "bytes=99-")

    class CorruptR2(FakeR2):
        def get_object(self, **kwargs: str) -> Mapping[str, Any]:
            response = dict(super().get_object(**kwargs))
            response["Metadata"] = {"sha256": "b" * 64}
            return response

    corrupt = CorruptR2()
    with pytest.raises(AudioStorageError):
        Boto3R2AudioStore(corrupt, "private-audio").get(KEY)
    assert corrupt.body is not None and corrupt.body.closed


def test_r2_maps_upstream_range_codes_and_checks_response_shape() -> None:
    client = FakeR2()
    store = Boto3R2AudioStore(client, "private-audio")
    client.get_error = ClientError("InvalidRange")
    with pytest.raises(InvalidRangeError):
        store.get(KEY, "bytes=0-1")

    class BadRangeR2(FakeR2):
        def get_object(self, **kwargs: str) -> Mapping[str, Any]:
            response = dict(super().get_object(**kwargs))
            response["ContentRange"] = "bytes 0-2/5"
            return response

    bad_range = BadRangeR2()
    with pytest.raises(AudioStorageError):
        Boto3R2AudioStore(bad_range, "private-audio").get(KEY, "bytes=0-1")
    assert bad_range.body is not None and bad_range.body.closed

    class StatusError(Exception):
        response = {"ResponseMetadata": {"HTTPStatusCode": 404}}

    missing = FakeR2()
    missing.error = StatusError()
    with pytest.raises(FileNotFoundError):
        Boto3R2AudioStore(missing, "private-audio").head(KEY)
