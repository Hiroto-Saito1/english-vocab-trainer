from pathlib import Path

from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.domain.models import Word
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
