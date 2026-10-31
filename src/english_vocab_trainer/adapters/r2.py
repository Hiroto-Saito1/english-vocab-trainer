from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

from english_vocab_trainer.configuration import ConfigurationError
from english_vocab_trainer.ports.audio import (
    MAX_AUDIO_BYTES,
    AudioMetadata,
    AudioStorageError,
    r2_audio_key,
    valid_audio_digest,
)


class S3LikeClient(Protocol):
    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...
    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: BinaryIO,
        ContentType: str,
        Metadata: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


S3ClientFactory = Callable[..., S3LikeClient]


def _boto3_client_factory(**kwargs: Any) -> S3LikeClient:
    # Import only when R2 is selected so local/test users do not need network setup.
    import boto3  # type: ignore[import-not-found]

    return cast(S3LikeClient, boto3.client("s3", **kwargs))


def r2_client_from_env(
    environ: Mapping[str, str], client_factory: S3ClientFactory | None = None
) -> tuple[S3LikeClient, str]:
    """Build the private R2 client for ingest commands."""
    required = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    if any(not environ.get(name) for name in required):
        raise ConfigurationError("private R2 audio configuration is incomplete")
    factory = client_factory or _boto3_client_factory
    try:
        client = factory(
            endpoint_url=environ["R2_ENDPOINT_URL"],
            aws_access_key_id=environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=environ["R2_SECRET_ACCESS_KEY"],
            region_name=environ.get("R2_REGION", "auto"),
        )
    except Exception as exc:
        raise ConfigurationError("private R2 audio client is unavailable") from exc
    return client, environ["R2_BUCKET"]


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return None
    error = response.get("Error")
    if isinstance(error, Mapping):
        code = error.get("Code")
        if isinstance(code, str):
            return code
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("HTTPStatusCode"), int):
        return str(metadata["HTTPStatusCode"])
    return None


def _metadata(response: Mapping[str, Any]) -> AudioMetadata:
    raw_metadata = response.get("Metadata", {})
    checksum = raw_metadata.get("sha256", "") if isinstance(raw_metadata, Mapping) else ""
    length = response.get("ContentLength")
    if (
        not isinstance(checksum, str)
        or not valid_audio_digest(checksum)
        or not isinstance(length, int)
        or length < 0
        or length > MAX_AUDIO_BYTES
    ):
        raise AudioStorageError("private audio storage is unavailable")
    return AudioMetadata(length, checksum)


class Boto3R2AudioUploader:
    """Safe, idempotent writer for private, content-addressed audio objects."""

    def __init__(self, client: S3LikeClient, bucket: str) -> None:
        self._client, self._bucket = client, bucket

    @staticmethod
    def _metadata_matches(response: Mapping[str, Any], size: int, checksum: str) -> bool:
        try:
            metadata = _metadata(response)
        except AudioStorageError:
            return False
        return metadata.size == size and metadata.etag == checksum

    def _head(self, key: str) -> Mapping[str, Any] | None:
        try:
            return self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            code = _error_code(exc)
            if code in {"NoSuchKey", "NoSuchObject", "404"}:
                return None
            raise AudioStorageError("private audio storage is unavailable") from exc

    def upload(self, key: str, path: Path, checksum: str, *, force: bool = False) -> bool:
        """Upload one object. Return true iff a PUT was issued."""
        if not valid_audio_digest(checksum) or key != r2_audio_key(checksum):
            raise ValueError("invalid audio object identity")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AudioStorageError("private audio source is unavailable") from exc
        if size > MAX_AUDIO_BYTES:
            raise ValueError("audio source exceeds maximum size")
        existing = self._head(key)
        if existing is not None:
            if self._metadata_matches(existing, size, checksum):
                return False
            if not force:
                raise AudioStorageError("private audio object conflicts with catalog")
        try:
            with path.open("rb") as body:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentType="audio/mpeg",
                    Metadata={"sha256": checksum},
                )
        except AudioStorageError:
            raise
        except Exception as exc:
            raise AudioStorageError("private audio storage is unavailable") from exc
        verified = self._head(key)
        if verified is None or not self._metadata_matches(verified, size, checksum):
            raise AudioStorageError("private audio storage verification failed")
        return True
