from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.ports.audio import (
    AudioMetadata,
    AudioResult,
    AudioStorageError,
    InvalidRangeError,
)
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.audio import build_audio_response
from english_vocab_trainer.web.container import AppContainer


def test_audio_response_range_head_and_etag() -> None:
    result = AudioResult(b"ab", 4, "tag", 1, 2, True)
    response = build_audio_response(result, False, None)
    assert response.status_code == 206 and response.headers["content-range"] == "bytes 1-2/4"
    assert build_audio_response(result, True, None).body == b""
    assert build_audio_response(result, False, '"tag"').status_code == 304
    assert build_audio_response(result, False, 'W/"tag"').status_code == 304
    assert build_audio_response(result, False, '"other", W/"tag"').status_code == 304


def test_audio_api_full_and_range(tmp_path: Path) -> None:
    (tmp_path / "x.mp3").write_bytes(b"abcde")
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
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
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
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
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    store = ConditionalStore()
    app = create_app(AppContainer(provider, store, "test", "local-user"))
    with TestClient(app) as client:
        # A matching precondition takes precedence over Range, including an invalid Range.
        cached = client.get(
            "/api/v1/audio/one",
            headers={"If-None-Match": validator, "Range": "bytes=99-"},
        )
    assert cached.status_code == 304 and cached.content == b""
    assert store.head_calls == 1 and store.get_calls == 0


def test_conditional_audio_get_fetches_when_validator_does_not_match(tmp_path: Path) -> None:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    store = ConditionalStore()
    app = create_app(AppContainer(provider, store, "test", "local-user"))
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


def _audio_app_with_store(tmp_path: Path, store: ConditionalStore) -> FastAPI:
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    return create_app(AppContainer(provider, store, "test", "local-user"))


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
