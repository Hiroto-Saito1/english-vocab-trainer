from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from ingest_support import records

from english_vocab_trainer.ingest.storage import (
    journal_path,
    manifest_path,
    merge_journal_records,
    read_catalog,
    write_catalog,
)
from english_vocab_trainer.ingest.transcription import transcribe_catalog


def test_journal_handles_truncated_tail_and_continue_on_error(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    current = [replace(record, transcript=None) for record in records(2)]
    write_catalog(catalog, current)

    class MixedTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            return "An English definition with an example."

    summary = transcribe_catalog(
        tmp_path, catalog, MixedTranscriber(), force=False, continue_on_error=True
    )
    assert summary.errors == 1 and summary.completed == 1
    error_report = catalog.with_suffix(".jsonl.transcription-errors.jsonl")
    assert error_report.exists() and "runtime-failure" in error_report.read_text()
    with journal_path(catalog).open("a", encoding="utf-8") as journal:
        journal.write('{"kind":')
    records_after_crash = read_catalog(catalog)
    assert records_after_crash[1].transcript is not None

    class RecoveryTranscriber:
        def transcribe(self, _: Path) -> str:
            return "An English definition with an example."

    recovered = transcribe_catalog(tmp_path, catalog, RecoveryTranscriber(), force=False)
    assert recovered.completed == 2 and not journal_path(catalog).exists()
    assert not catalog.with_suffix(".jsonl.transcription-errors.jsonl").exists()


def test_journal_rejects_non_object_entries(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, records(1))
    journal_path(catalog).write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="journal is corrupt"):
        read_catalog(catalog)


def test_journal_cannot_change_immutable_catalog_fields(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    record = records(1)[0]
    write_catalog(catalog, [record])
    journal_path(catalog).write_text(
        json.dumps({"kind": "header", "fingerprint": "f" * 64})
        + "\n"
        + json.dumps({"kind": "record", "record": asdict(replace(record, source="超上級SVL"))})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="immutable"):
        read_catalog(catalog)


def test_journal_rejects_corrupt_identity_and_records(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    record = records(1)[0]
    write_catalog(catalog, [record])
    journal_path(catalog).write_text('{"kind":"header","fingerprint":"a"}\n[]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="journal is corrupt"):
        read_catalog(catalog)
    journal_path(catalog).write_text(
        '{"kind":"header","fingerprint":"a"}\n{"kind":"header","fingerprint":"a"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="journal is corrupt"):
        read_catalog(catalog)
    journal_path(catalog).write_text('{"kind":"header","fingerprint":"wrong"}\n', encoding="utf-8")
    manifest_path(catalog).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "mvp",
                "expected_count": 2,
                "inventory_digest": "a" * 64,
                "model": "model",
                "language": "en",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="identity"):
        read_catalog(catalog)
    journal_path(catalog).write_text(
        json.dumps({"kind": "record", "record": asdict(record)}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="journal is corrupt"):
        read_catalog(catalog)
    with pytest.raises(ValueError, match="absent from catalog base"):
        merge_journal_records([], {record.checksum: record})
