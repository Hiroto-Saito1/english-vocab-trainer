from __future__ import annotations

import hashlib
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from ingest_support import approved_source_tree, materialize_catalog_audio, records

from english_vocab_trainer.adapters.worker_r2 import WorkerR2AudioStore, _bytes
from english_vocab_trainer.ingest.catalog import scan_catalog
from english_vocab_trainer.ingest.export_d1 import export_d1
from english_vocab_trainer.ingest.storage import write_catalog
from english_vocab_trainer.ingest.validation import validate_records
from english_vocab_trainer.ports.audio import AudioStorageError, InvalidRangeError


class Object:
    def __init__(self, data: bytes, size: int | None = None) -> None:
        self.data = data
        self.size = len(data) if size is None else size

    async def arrayBuffer(self) -> bytes:
        return self.data


class Binding:
    def __init__(self, obj: Object | None) -> None:
        self.obj = obj
        self.calls = 0

    async def head(self, key: str) -> Object | None:
        self.calls += 1
        return self.obj

    async def get(self, key: str) -> Object | None:
        self.calls += 1
        return self.obj


@pytest.mark.asyncio
async def test_worker_binding_ranges_keys_size_and_integrity() -> None:
    data = b"abcde"
    key = f"audio/{hashlib.sha256(data).hexdigest()}.mp3"
    binding = Binding(Object(data))
    store = WorkerR2AudioStore(binding)
    assert (await store.head(key)).size == 5
    assert (await store.get(key)).body == data
    assert (await store.get(key, "bytes=1-3")).body == b"bcd"
    assert (await store.get(key, "bytes=-2")).body == b"de"
    with pytest.raises(InvalidRangeError):
        await store.get(key, "bytes=9-")
    with pytest.raises(FileNotFoundError):
        await store.get("../" + key)
    assert binding.calls == 5
    with pytest.raises(AudioStorageError):
        await WorkerR2AudioStore(Binding(Object(data, 1_048_577))).head(key)
    with pytest.raises(AudioStorageError):
        await WorkerR2AudioStore(Binding(Object(b"wrong"))).get(key)
    with pytest.raises(FileNotFoundError):
        await WorkerR2AudioStore(Binding(None)).get(key)


def test_export_deterministic_escaped_and_atomic(tmp_path: Path) -> None:
    approved_source_tree(tmp_path, 10)
    catalog = tmp_path / "catalog.jsonl"
    current, _ = scan_catalog(tmp_path, catalog)
    current = [
        replace(record, transcript="A clear English definition includes an example sentence.")
        for record in current
    ]
    current[0] = replace(current[0], transcript="It's a clear English example sentence.")
    write_catalog(catalog, current)
    output = tmp_path / "private.sql"
    digest, count, warnings = export_d1(tmp_path, catalog, output, published_at="2026-01-01")
    text = output.read_text()
    assert "It''s" in text and count == 20 and warnings
    again = tmp_path / "again.sql"
    write_catalog(catalog, list(reversed(current)))
    assert export_d1(tmp_path, catalog, again, published_at="2026-01-01")[0] == digest
    assert again.read_text() == text
    dry_output = tmp_path / "dry.sql"
    assert export_d1(tmp_path, catalog, dry_output, dry_run=True)[0] == digest
    assert not dry_output.exists()
    database = sqlite3.connect(":memory:")
    database.executescript(
        Path("src/english_vocab_trainer/migrations/0001_initial.sql").read_text()
    )
    database.executescript(text)
    assert database.execute(
        "SELECT catalog_sha256,catalog_count,published_at FROM schema_metadata"
    ).fetchone() == (digest, 20, "2026-01-01")
    assert database.execute("SELECT count(*) FROM words").fetchone() == (20,)
    database.close()
    (tmp_path / current[-1].audio_key).write_bytes(b"bad")
    with pytest.raises(ValueError):
        export_d1(tmp_path, catalog, output)
    assert output.read_text() == text


def test_capacity_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = materialize_catalog_audio(tmp_path, records())
    monkeypatch.setattr("english_vocab_trainer.ingest.validation.MAX_AGGREGATE_AUDIO_BYTES", 1)
    with pytest.raises(ValueError, match="capacity"):
        validate_records(current, root=tmp_path)
    monkeypatch.setattr(
        "english_vocab_trainer.ingest.validation.MAX_AGGREGATE_AUDIO_BYTES", 8_000_000_000
    )
    monkeypatch.setattr("english_vocab_trainer.ingest.validation.MAX_AUDIO_BYTES", 1)
    with pytest.raises(ValueError, match="maximum size"):
        validate_records(current, root=tmp_path)


def test_js_arraybuffer_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    token = object()

    class FakeUint8Array:
        @staticmethod
        def new(buffer: object) -> SimpleNamespace:
            assert buffer is token
            return SimpleNamespace(to_py=lambda: [1, 2, 255])

    monkeypatch.setitem(sys.modules, "js", SimpleNamespace(Uint8Array=FakeUint8Array))
    assert _bytes(token) == b"\x01\x02\xff"
