from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.r2 import Boto3R2AudioStore, S3LikeClient
from english_vocab_trainer.ports.audio import AudioMetadata, AudioResult, AudioStore
from english_vocab_trainer.ports.repositories import RepositoryProvider, VocabularyRepository
from english_vocab_trainer.web.auth import AuthService


class ConfigurationError(RuntimeError):
    pass


S3ClientFactory = Callable[..., S3LikeClient]


def _boto3_client_factory(**kwargs: Any) -> S3LikeClient:
    # Import only when R2 is selected so local/test users do not need network setup.
    import boto3  # type: ignore[import-untyped]

    return cast(S3LikeClient, boto3.client("s3", **kwargs))


def r2_client_from_env(
    environ: Mapping[str, str], client_factory: S3ClientFactory | None = None
) -> tuple[S3LikeClient, str]:
    """Build the one private R2 client used by serving and ingest commands."""
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


def audio_store_from_env(
    environ: Mapping[str, str], client_factory: S3ClientFactory | None = None
) -> AudioStore:
    """Create the explicitly selected private audio backend, or fail closed."""
    backend = environ.get("AUDIO_BACKEND")
    if backend == "filesystem":
        root = environ.get("AUDIO_ROOT")
        if not root:
            raise ConfigurationError("AUDIO_ROOT is required for the filesystem audio backend")
        return FilesystemAudioStore(Path(root))
    if backend == "r2":
        client, bucket = r2_client_from_env(environ, client_factory)
        return Boto3R2AudioStore(client, bucket)
    raise ConfigurationError("AUDIO_BACKEND must be filesystem or r2")


@dataclass(frozen=True, slots=True)
class AppContainer:
    repositories: RepositoryProvider
    audio: AudioStore
    environment: str
    local_user_id: str | None = None
    auth: AuthService | None = None
    trusted_hosts: tuple[str, ...] = ()


def auth_from_env(
    environ: Mapping[str, str], database: Path, *, secure_cookies: bool
) -> AuthService:
    password_hash = environ.get("APP_PASSWORD_HASH")
    secret = environ.get("SESSION_SIGNING_SECRET")
    if not password_hash or not secret:
        raise ConfigurationError("APP_PASSWORD_HASH and SESSION_SIGNING_SECRET are required")
    try:
        import base64

        signing_secret = base64.b64decode(
            secret + "=" * (-len(secret) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(signing_secret).rstrip(b"=").decode("ascii") != secret:
            raise ValueError("session signing secret is not canonical base64url")
        return AuthService(
            password_hash,
            signing_secret,
            SQLiteLoginAttemptLimiter(database),
            secure_cookies=secure_cookies,
        )
    except Exception as exc:
        raise ConfigurationError("authentication configuration is invalid") from exc


def trusted_hosts_from_env(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Require an explicit public host, or safely derive Fly's canonical hostname."""
    raw_hosts = environ.get("ALLOWED_HOSTS", "")
    if raw_hosts:
        configured = tuple(raw_hosts.split(","))
        if any(host != host.strip() for host in configured) or not all(
            _is_valid_host(host) for host in configured
        ):
            raise ConfigurationError("ALLOWED_HOSTS must contain valid host names only")
        return configured
    fly_app = environ.get("FLY_APP_NAME")
    if fly_app and _FLY_APP_NAME.fullmatch(fly_app):
        return (f"{fly_app}.fly.dev",)
    raise ConfigurationError("ALLOWED_HOSTS or FLY_APP_NAME is required in production")


_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_FLY_APP_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _is_valid_host(host: str) -> bool:
    candidate = host[2:] if host.startswith("*.") else host
    if not candidate or len(candidate) > 253 or any(character.isspace() for character in host):
        return False
    return all(_HOST_LABEL.fullmatch(label) is not None for label in candidate.split("."))


class UnavailableRepositoryProvider:
    def for_user(self, user_id: str) -> VocabularyRepository:
        raise ConfigurationError("repository is not configured")


class UnavailableAudioStore:
    def head(self, key: str) -> AudioMetadata:
        raise ConfigurationError("audio store is not configured")

    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        raise ConfigurationError("audio store is not configured")
