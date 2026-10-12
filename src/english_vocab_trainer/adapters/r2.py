from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, Protocol, cast

from english_vocab_trainer.configuration import ConfigurationError
from english_vocab_trainer.ports.audio import (
    AudioMetadata,
    AudioResult,
    AudioStorageError,
    InvalidRangeError,
    parse_single_range,
)

_KEY = re.compile(r"audio/[0-9a-f]{64}\.mp3\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class StreamingBody(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...
    def close(self) -> None: ...


class S3LikeClient(Protocol):
    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...
    def get_object(self, **kwargs: str) -> Mapping[str, Any]: ...
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
    import boto3  # type: ignore[import-untyped]

    return cast(S3LikeClient, boto3.client("s3", **kwargs))


def r2_client_from_env(
    environ: Mapping[str, str], client_factory: S3ClientFactory | None = None
) -> tuple[S3LikeClient, str]:
    """Build the private R2 client shared by serving and ingest commands."""
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


class Boto3R2AudioStore:
    """Injected S3-compatible private R2 adapter; no global client or public URLs."""

    def __init__(self, client: S3LikeClient, bucket: str) -> None:
        self._client, self._bucket = client, bucket

    def _key(self, key: str) -> str:
        if not _KEY.fullmatch(key):
            raise FileNotFoundError("invalid audio key")
        return key

    @staticmethod
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

    @classmethod
    def _raise_store_error(cls, exc: Exception, key: str, *, range_request: bool) -> NoReturn:
        code = cls._error_code(exc)
        if code in {"NoSuchKey", "NoSuchObject", "404"}:
            raise FileNotFoundError(key) from exc
        if range_request and code in {"InvalidRange", "416"}:
            raise InvalidRangeError("invalid byte range") from exc
        raise AudioStorageError("private audio storage is unavailable") from exc

    @staticmethod
    def _metadata(response: Mapping[str, Any]) -> AudioMetadata:
        raw_metadata = response.get("Metadata", {})
        checksum = raw_metadata.get("sha256", "") if isinstance(raw_metadata, Mapping) else ""
        length = response.get("ContentLength")
        if (
            not isinstance(checksum, str)
            or _SHA256.fullmatch(checksum) is None
            or not isinstance(length, int)
            or length < 0
        ):
            raise AudioStorageError("private audio storage is unavailable")
        return AudioMetadata(length, checksum)

    def head(self, key: str) -> AudioMetadata:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=self._key(key))
            return self._metadata(response)
        except (FileNotFoundError, AudioStorageError):
            raise
        except Exception as exc:
            self._raise_store_error(exc, key, range_request=False)

    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        metadata = self.head(key)
        selected = parse_single_range(range_header, metadata.size)
        kwargs: dict[str, str] = {"Bucket": self._bucket, "Key": self._key(key)}
        if selected is not None:
            kwargs["Range"] = f"bytes={selected[0]}-{selected[1]}"
        try:
            response = self._client.get_object(**kwargs)
            body = cast(StreamingBody, response["Body"])
            try:
                data = bytes(body.read())
            finally:
                body.close()
            response_metadata = self._metadata(response)
            if response_metadata.etag != metadata.etag:
                raise AudioStorageError("private audio storage is unavailable")
            content_length = response.get("ContentLength")
            if not isinstance(content_length, int) or content_length != len(data):
                raise AudioStorageError("private audio storage is unavailable")
            if selected is not None:
                content_range = response.get("ContentRange")
                match = (
                    re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if isinstance(content_range, str)
                    else None
                )
                if (
                    match is None
                    or (int(match.group(1)), int(match.group(2))) != selected
                    or int(match.group(3)) != metadata.size
                ):
                    raise AudioStorageError("private audio storage is unavailable")
                start, end = selected
            else:
                start, end = 0, metadata.size - 1
                if "ContentRange" in response:
                    raise AudioStorageError("private audio storage is unavailable")
            if len(data) != max(0, end - start + 1):
                raise AudioStorageError("private audio storage is unavailable")
            return AudioResult(data, metadata.size, metadata.etag, start, end, selected is not None)
        except (InvalidRangeError, AudioStorageError):
            raise
        except Exception as exc:
            self._raise_store_error(exc, key, range_request=range_header is not None)


class Boto3R2AudioUploader:
    """Safe, idempotent writer for private, content-addressed audio objects."""

    def __init__(self, client: S3LikeClient, bucket: str) -> None:
        self._client, self._bucket = client, bucket

    @staticmethod
    def _metadata_matches(response: Mapping[str, Any], size: int, checksum: str) -> bool:
        try:
            metadata = Boto3R2AudioStore._metadata(response)
        except AudioStorageError:
            return False
        return metadata.size == size and metadata.etag == checksum

    def _head(self, key: str) -> Mapping[str, Any] | None:
        try:
            return self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            code = Boto3R2AudioStore._error_code(exc)
            if code in {"NoSuchKey", "NoSuchObject", "404"}:
                return None
            raise AudioStorageError("private audio storage is unavailable") from exc

    def upload(self, key: str, path: Path, checksum: str, *, force: bool = False) -> bool:
        """Upload one object. Return true iff a PUT was issued."""
        if _SHA256.fullmatch(checksum) is None or key != f"audio/{checksum}.mp3":
            raise ValueError("invalid audio object identity")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AudioStorageError("private audio source is unavailable") from exc
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
