from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from english_vocab_trainer.adapters.r2 import Boto3R2AudioStore, Boto3R2AudioUploader
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

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: BinaryIO,
        ContentType: str,
        Metadata: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.value = Body.read()
        return {}


class UploadR2:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.head_calls = 0
        self.put_calls = 0
        self.body: BinaryIO | None = None
        self.error: Exception | None = None
        self.bad_verify = False

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
        self.head_calls += 1
        if self.error is not None:
            raise self.error
        try:
            value, digest = self.objects[Key]
        except KeyError as exc:
            raise ClientError("NoSuchKey") from exc
        return {
            "ContentLength": len(value),
            "Metadata": {"sha256": "b" * 64 if self.bad_verify else digest},
        }

    def get_object(self, **kwargs: str) -> Mapping[str, Any]:
        raise AssertionError("not used")

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: BinaryIO,
        ContentType: str,
        Metadata: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.put_calls += 1
        self.body = Body
        self.objects[Key] = (Body.read(), Metadata["sha256"])
        return {}


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


def test_r2_uploader_uploads_verifies_skips_and_closes_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.mp3"
    path.write_bytes(b"audio")
    digest = "c" * 64
    key = f"audio/{digest}.mp3"
    client = UploadR2()
    uploader = Boto3R2AudioUploader(client, "private-audio")

    assert uploader.upload(key, path, digest)
    assert client.put_calls == 1 and client.body is not None and client.body.closed
    assert not uploader.upload(key, path, digest)
    assert client.put_calls == 1


def test_r2_uploader_fails_closed_for_conflicts_errors_and_bad_verification(tmp_path: Path) -> None:
    path = tmp_path / "sample.mp3"
    path.write_bytes(b"audio")
    digest = "d" * 64
    key = f"audio/{digest}.mp3"
    client = UploadR2()
    client.objects[key] = (b"other", "e" * 64)
    uploader = Boto3R2AudioUploader(client, "private-audio")
    with pytest.raises(AudioStorageError, match="conflicts"):
        uploader.upload(key, path, digest)
    assert uploader.upload(key, path, digest, force=True)

    broken = UploadR2()
    broken.bad_verify = True
    with pytest.raises(AudioStorageError, match="verification"):
        Boto3R2AudioUploader(broken, "private-audio").upload(key, path, digest)
    unavailable = UploadR2()
    unavailable.error = ClientError("AccessDenied")
    with pytest.raises(AudioStorageError, match="unavailable"):
        Boto3R2AudioUploader(unavailable, "private-audio").upload(key, path, digest)

    invalid = UploadR2()
    invalid_uploader = Boto3R2AudioUploader(invalid, "private-audio")
    with pytest.raises(ValueError, match="invalid audio object identity"):
        invalid_uploader.upload("bad-key", path, digest)
    with pytest.raises(ValueError, match="invalid audio object identity"):
        invalid_uploader.upload(f"audio/{digest}.mp3", path, "e" * 64)
    assert invalid.head_calls == 0 and invalid.put_calls == 0

    class PutFailure(UploadR2):
        def put_object(
            self,
            *,
            Bucket: str,
            Key: str,
            Body: BinaryIO,
            ContentType: str,
            Metadata: Mapping[str, str],
        ) -> Mapping[str, Any]:
            raise ClientError("AccessDenied")

    with pytest.raises(AudioStorageError, match="unavailable"):
        Boto3R2AudioUploader(PutFailure(), "private-audio").upload(key, path, digest)
