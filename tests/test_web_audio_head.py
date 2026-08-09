from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.ports.audio import AudioMetadata, AudioResult, AudioStorageError
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.container import AppContainer


def test_audio_head_full_and_range(tmp_path: Path) -> None:
    (tmp_path / "x.mp3").write_bytes(b"abcde")
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    app = create_app(AppContainer(provider, FilesystemAudioStore(tmp_path), "test", "local-user"))
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
    provider = SQLiteRepositoryProvider(tmp_path / "v.db")
    repository = provider.for_user("local-user")
    repository.add_word(Word("one", "one", 9, None, "x.mp3"))
    repository.close()
    store = MetadataOnlyStore()
    app = create_app(AppContainer(provider, store, "test", "local-user"))
    with TestClient(app) as client:
        invalid = client.head("/api/v1/audio/one", headers={"Range": "bytes=9-"})
    assert invalid.status_code == 416 and store.head_calls == 1 and store.get_calls == 0

    unavailable = MetadataOnlyStore(unavailable=True)
    app = create_app(AppContainer(provider, unavailable, "test", "local-user"))
    with TestClient(app) as client:
        failed = client.head("/api/v1/audio/one")
        failed_get = client.get("/api/v1/audio/one", headers={"Range": "bytes=0-1"})
    assert (
        failed.status_code == 502 and failed_get.status_code == 502 and "token" not in failed.text
    )
