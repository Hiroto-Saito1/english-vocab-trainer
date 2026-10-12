from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.r2 import (
    Boto3R2AudioStore,
    S3ClientFactory,
    r2_client_from_env,
)
from english_vocab_trainer.configuration import ConfigurationError
from english_vocab_trainer.ports.audio import AudioStore
from english_vocab_trainer.web.auth import AuthService


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
    database: Path
    audio: AudioStore
    environment: str
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
