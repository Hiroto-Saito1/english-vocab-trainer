from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from english_vocab_trainer.adapters.r2 import Boto3R2AudioUploader
from english_vocab_trainer.ports.audio import AudioStorageError


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response: dict[str, object] = {"Error": {"Code": code}}


class UploadR2:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.head_calls = 0
        self.put_calls = 0
        self.body: BinaryIO | None = None
        self.error: Exception | None = None
        self.bad_verify = False
        self.malformed_metadata = False

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
            "Metadata": {"sha256": "not-a-checksum"}
            if self.malformed_metadata
            else {"sha256": "b" * 64 if self.bad_verify else digest},
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


def test_r2_uploader_rejects_missing_source_and_malformed_existing_metadata(tmp_path: Path) -> None:
    digest = "f" * 64
    key = f"audio/{digest}.mp3"
    client = UploadR2()
    uploader = Boto3R2AudioUploader(client, "private-audio")
    with pytest.raises(AudioStorageError, match="source is unavailable"):
        uploader.upload(key, tmp_path / "gone.mp3", digest)
    assert client.head_calls == 0 and client.put_calls == 0

    path = tmp_path / "sample.mp3"
    path.write_bytes(b"audio")
    client.objects[key] = (b"audio", digest)
    client.malformed_metadata = True
    with pytest.raises(AudioStorageError, match="conflicts"):
        uploader.upload(key, path, digest)
    assert client.put_calls == 0
