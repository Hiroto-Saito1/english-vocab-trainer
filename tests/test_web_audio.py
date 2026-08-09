from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.ports.audio import AudioResult
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
