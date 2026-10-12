from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from ingest_support import materialize_catalog_audio, records

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.ingest.publish import publish_records, upload_audio
from english_vocab_trainer.ingest.validation import r2_audio_key, validate_records


def test_publish_records_writes_sqlite_words(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    current = materialize_catalog_audio(tmp_path, records())
    publish_records(database, current, root=tmp_path)
    repository = SQLiteVocabularyRepository(database)
    try:
        words = [word for record in current if (word := repository.get_word(record.id))]
        assert len(words) == 20 and words[0].definition.startswith("A clear English")
    finally:
        repository.close()


def test_publish_r2_uses_canonical_keys_and_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    current = materialize_catalog_audio(tmp_path, records())
    publish_records(database, current, audio_backend="r2", root=tmp_path)
    repository = SQLiteVocabularyRepository(database)
    try:
        words = [word for record in current if (word := repository.get_word(record.id))]
        assert len(words) == 20
        assert {word.audio_key for word in words} == {
            r2_audio_key(record.checksum) for record in current
        }
        prior = repository.get_word(current[0].id)
        assert prior is not None
        repository.db.execute(
            "CREATE TRIGGER reject_one BEFORE INSERT ON words WHEN NEW.id="
            f"'{current[10].id}' BEGIN SELECT RAISE(ABORT, 'no'); END"
        )
        changed = [
            replace(
                record, transcript="A different English definition includes an example sentence."
            )
            for record in current
        ]
        with pytest.raises(Exception, match="no"):
            publish_records(database, changed, audio_backend="r2", root=tmp_path)
        assert repository.get_word(current[0].id) == prior
    finally:
        repository.close()


class _UploadClient:
    def __init__(self) -> None:
        self.calls = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        raise AssertionError("checksum validation must happen before network")

    def get_object(self, **kwargs: str) -> dict[str, Any]:
        raise AssertionError("not used")

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: BinaryIO,
        ContentType: str,
        Metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("not used")


def test_upload_validates_all_files_before_any_network(tmp_path: Path) -> None:
    current = materialize_catalog_audio(tmp_path, records())
    bad = replace(current[-1], checksum="a" * 64, id="audio-" + "a" * 64)
    client = _UploadClient()
    with pytest.raises(ValueError, match="does not match"):
        upload_audio(tmp_path, [*current[:-1], bad], client, "private")


def test_tampered_source_rejects_validate_and_publish_without_db_mutation(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    current = materialize_catalog_audio(tmp_path, records())
    publish_records(database, current, root=tmp_path)
    repository = SQLiteVocabularyRepository(database)
    try:
        original = repository.get_word(current[0].id)
        assert original is not None
        (tmp_path / current[-1].audio_key).write_bytes(b"tampered")
        with pytest.raises(ValueError, match="does not match"):
            validate_records(current, root=tmp_path)
        with pytest.raises(ValueError, match="does not match"):
            publish_records(database, current, root=tmp_path)
        assert repository.get_word(current[0].id) == original
    finally:
        repository.close()


def test_publish_requires_source_root_and_rejects_source_mismatch(tmp_path: Path) -> None:
    current = materialize_catalog_audio(tmp_path, records())
    with pytest.raises(ValueError, match="requires a source root"):
        publish_records(tmp_path / "vocab.db", current)
    mismatched = replace(current[0], source="超上級SVL")
    with pytest.raises(ValueError, match="source does not match"):
        publish_records(tmp_path / "vocab.db", [mismatched, *current[1:]], root=tmp_path)
    assert not (tmp_path / "vocab.db").exists()
