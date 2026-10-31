from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.r2 import Boto3R2AudioStore, S3ClientFactory, r2_client_from_env
from english_vocab_trainer.configuration import ConfigurationError
from english_vocab_trainer.ports.audio import AudioStore


def audio_store_from_env(
    environ: Mapping[str, str], client_factory: S3ClientFactory | None = None
) -> AudioStore:
    """Build local/test storage; production uses the Worker's R2 binding."""
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
    environment: str = "test"


def container_from_env(environ: Mapping[str, str] = os.environ) -> AppContainer:
    environment = environ.get("APP_ENV", "local")
    if environment not in {"local", "test"}:
        raise ConfigurationError("production must run through the Cloudflare Worker entry point")
    try:
        database = Path(environ["VOCAB_DB_PATH"])
    except KeyError as exc:
        raise ConfigurationError("VOCAB_DB_PATH is required") from exc
    return AppContainer(database, audio_store_from_env(environ), environment)
