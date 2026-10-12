from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.ports.audio import (
    AudioMetadata,
    AudioResult,
    AudioStorageError,
    AudioStore,
    InvalidRangeError,
)
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def _audio_app_with_store(tmp_path: Path, store: AudioStore) -> FastAPI:
    database = tmp_path / "v.db"
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words([Word("one", "one", 9, "definition", "x.mp3")])
    return create_app(AppContainer(database, store, "test"))


def test_audio_api_full_and_range(tmp_path: Path) -> None:
    (tmp_path / "x.mp3").write_bytes(b"abcde")
    app = _audio_app_with_store(tmp_path, FilesystemAudioStore(tmp_path))
    with TestClient(app) as client:
        full = client.get("/api/v1/audio/one")
        partial = client.get("/api/v1/audio/one", headers={"Range": "bytes=1-3"})
    assert (
        full.status_code == 200
        and full.content == b"abcde"
        and full.headers["content-length"] == "5"
    )
    assert full.headers["accept-ranges"] == "bytes" and full.headers["etag"]
    assert (
        partial.status_code == 206
        and partial.content == b"bcd"
        and partial.headers["content-range"] == "bytes 1-3/5"
    )


def test_audio_api_invalid_range_and_etag(tmp_path: Path) -> None:
    (tmp_path / "x.mp3").write_bytes(b"abcde")
    app = _audio_app_with_store(tmp_path, FilesystemAudioStore(tmp_path))
    with TestClient(app) as client:
        full = client.get("/api/v1/audio/one")
        invalid = client.get("/api/v1/audio/one", headers={"Range": "bytes=9-"})
        cached = client.get("/api/v1/audio/one", headers={"If-None-Match": full.headers["etag"]})
    assert invalid.status_code == 416 and invalid.headers["content-range"] == "bytes */5"
    assert cached.status_code == 304 and cached.content == b""


class ConditionalStore:
    def __init__(self) -> None:
        self.head_calls = 0
        self.get_calls = 0

    def head(self, key: str) -> AudioMetadata:
        self.head_calls += 1
        return AudioMetadata(5, "a" * 64)

    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        self.get_calls += 1
        return AudioResult(b"abcde", 5, "a" * 64, 0, 4, False)


@pytest.mark.parametrize(
    "validator",
    [f'"{"a" * 64}"', f'W/"{"a" * 64}"', f'"other", W/"{"a" * 64}"'],
)
def test_conditional_audio_get_precedes_range_and_never_fetches_body(
    tmp_path: Path, validator: str
) -> None:
    store = ConditionalStore()
    app = _audio_app_with_store(tmp_path, store)
    with TestClient(app) as client:
        # A matching precondition takes precedence over Range, including an invalid Range.
        cached = client.get(
            "/api/v1/audio/one",
            headers={"If-None-Match": validator, "Range": "bytes=99-"},
        )
    assert cached.status_code == 304 and cached.content == b""
    assert store.head_calls == 1 and store.get_calls == 0


def test_conditional_audio_get_fetches_when_validator_does_not_match(tmp_path: Path) -> None:
    store = ConditionalStore()
    app = _audio_app_with_store(tmp_path, store)
    with TestClient(app) as client:
        response = client.get("/api/v1/audio/one", headers={"If-None-Match": '"other"'})
    assert response.status_code == 200 and response.content == b"abcde"
    assert store.head_calls == 1 and store.get_calls == 1


class RacingRangeStore(ConditionalStore):
    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        self.get_calls += 1
        raise InvalidRangeError("object changed after validation")


class UnavailableGetStore(ConditionalStore):
    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        self.get_calls += 1
        raise AudioStorageError("upstream secret=private")


def test_audio_get_handles_upstream_range_race_and_storage_failure(tmp_path: Path) -> None:
    racing = RacingRangeStore()
    with TestClient(_audio_app_with_store(tmp_path, racing)) as client:
        race = client.get("/api/v1/audio/one", headers={"Range": "bytes=0-1"})
    assert race.status_code == 416 and race.headers["content-range"] == "bytes */5"
    assert racing.head_calls == 2 and racing.get_calls == 1

    unavailable = UnavailableGetStore()
    with TestClient(_audio_app_with_store(tmp_path, unavailable)) as client:
        failed = client.get("/api/v1/audio/one")
        missing_word = client.get("/api/v1/audio/missing")
    assert failed.status_code == 502 and "secret" not in failed.text
    assert missing_word.status_code == 404


def test_audio_head_full_and_range(tmp_path: Path) -> None:
    (tmp_path / "x.mp3").write_bytes(b"abcde")
    app = _audio_app_with_store(tmp_path, FilesystemAudioStore(tmp_path))
    with TestClient(app) as client:
        full = client.head("/api/v1/audio/one")
        partial = client.head("/api/v1/audio/one", headers={"Range": "bytes=1-3"})
    assert full.status_code == 200 and full.content == b"" and full.headers["content-length"] == "5"
    assert (
        partial.status_code == 206
        and partial.content == b""
        and partial.headers["content-length"] == "3"
        and partial.headers["content-range"] == "bytes 1-3/5"
    )


class MetadataOnlyStore:
    def __init__(self, unavailable: bool = False) -> None:
        self.head_calls = 0
        self.get_calls = 0
        self.unavailable = unavailable

    def head(self, key: str) -> AudioMetadata:
        self.head_calls += 1
        if self.unavailable:
            raise AudioStorageError("upstream token=private")
        return AudioMetadata(5, "a" * 64)

    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        self.get_calls += 1
        raise AssertionError("HEAD must not call get")


def test_audio_head_uses_metadata_only_and_sanitizes_storage_failure(tmp_path: Path) -> None:
    store = MetadataOnlyStore()
    with TestClient(_audio_app_with_store(tmp_path, store)) as client:
        invalid = client.head("/api/v1/audio/one", headers={"Range": "bytes=9-"})
    assert invalid.status_code == 416 and store.head_calls == 1 and store.get_calls == 0

    unavailable = MetadataOnlyStore(unavailable=True)
    with TestClient(_audio_app_with_store(tmp_path, unavailable)) as client:
        failed = client.head("/api/v1/audio/one")
        failed_get = client.get("/api/v1/audio/one", headers={"Range": "bytes=0-1"})
    assert (
        failed.status_code == 502 and failed_get.status_code == 502 and "token" not in failed.text
    )
