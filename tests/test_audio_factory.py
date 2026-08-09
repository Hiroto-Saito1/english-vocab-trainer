from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.r2 import Boto3R2AudioStore
from english_vocab_trainer.web.container import (
    ConfigurationError,
    audio_store_from_env,
    r2_client_from_env,
)


class FactoryClient:
    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
        raise AssertionError("not used")

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
        raise AssertionError("not used")


def test_filesystem_factory_requires_explicit_backend_and_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        audio_store_from_env({})
    with pytest.raises(ConfigurationError):
        audio_store_from_env({"AUDIO_BACKEND": "filesystem"})
    store = audio_store_from_env({"AUDIO_BACKEND": "filesystem", "AUDIO_ROOT": str(tmp_path)})
    assert isinstance(store, FilesystemAudioStore)


def test_r2_factory_injects_client_and_defaults_region() -> None:
    received: dict[str, object] = {}

    def factory(**kwargs: object) -> FactoryClient:
        received.update(kwargs)
        return FactoryClient()

    store = audio_store_from_env(
        {
            "AUDIO_BACKEND": "r2",
            "R2_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
            "R2_ACCESS_KEY_ID": "access",
            "R2_SECRET_ACCESS_KEY": "secret",
            "R2_BUCKET": "private-audio",
        },
        factory,
    )
    assert isinstance(store, Boto3R2AudioStore)
    assert received == {
        "endpoint_url": "https://account.r2.cloudflarestorage.com",
        "aws_access_key_id": "access",
        "aws_secret_access_key": "secret",
        "region_name": "auto",
    }


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
        audio_store_from_env({"AUDIO_BACKEND": backend})
