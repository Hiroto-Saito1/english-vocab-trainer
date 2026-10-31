from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.r2 import r2_client_from_env
from english_vocab_trainer.configuration import ConfigurationError
from english_vocab_trainer.web.container import container_from_env


class FactoryClient:
    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
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
        raise AssertionError("not used")


def test_filesystem_factory_requires_explicit_backend_and_root(tmp_path: Path) -> None:
    env = {"VOCAB_DB_PATH": str(tmp_path / "v.db")}
    with pytest.raises(ConfigurationError):
        container_from_env(env)
    with pytest.raises(ConfigurationError):
        container_from_env({**env, "AUDIO_BACKEND": "filesystem"})
    container = container_from_env(
        {**env, "AUDIO_BACKEND": "filesystem", "AUDIO_ROOT": str(tmp_path)}
    )
    assert isinstance(container.audio, FilesystemAudioStore)


def test_container_requires_database_and_rejects_production(tmp_path: Path) -> None:
    audio_env = {"AUDIO_BACKEND": "filesystem", "AUDIO_ROOT": str(tmp_path)}
    with pytest.raises(ConfigurationError, match="VOCAB_DB_PATH"):
        container_from_env(audio_env)
    with pytest.raises(ConfigurationError, match="Cloudflare Worker"):
        container_from_env({**audio_env, "APP_ENV": "production"})


def test_r2_client_uses_explicit_region_and_sanitizes_factory_failure() -> None:
    received: dict[str, object] = {}

    def factory(**kwargs: object) -> FactoryClient:
        received.update(kwargs)
        return FactoryClient()

    env = {
        "R2_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
        "R2_ACCESS_KEY_ID": "access",
        "R2_SECRET_ACCESS_KEY": "secret",
        "R2_BUCKET": "private-audio",
        "R2_REGION": "us-east-1",
    }
    client, bucket = r2_client_from_env(env, factory)
    assert isinstance(client, FactoryClient) and bucket == "private-audio"
    assert received["region_name"] == "us-east-1"

    def failing_factory(**_: object) -> FactoryClient:
        raise RuntimeError("credential=secret")

    with pytest.raises(ConfigurationError, match="client is unavailable"):
        r2_client_from_env(env, failing_factory)


@pytest.mark.parametrize("backend", ["r2", "unknown"])
def test_factory_fails_closed_for_incomplete_or_unknown_backend(backend: str) -> None:
    with pytest.raises(ConfigurationError):
        container_from_env({"VOCAB_DB_PATH": "v.db", "AUDIO_BACKEND": backend})
