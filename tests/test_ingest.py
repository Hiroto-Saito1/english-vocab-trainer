from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.ingest import (
    FULL_PROFILE,
    AudioCandidate,
    CatalogRecord,
    MlxWhisperTranscriber,
    _error_updates,
    _failure_reason,
    _merge_journal,
    _read_current_error_report,
    _validate_catalog_records,
    audit_catalog,
    audit_records,
    catalog_lock,
    error_journal_path,
    error_report_path,
    journal_path,
    main,
    manifest_path,
    parse_audio_path,
    profile_for,
    publish_records,
    r2_audio_key,
    read_catalog,
    read_manifest,
    records_from_audio,
    scan_catalog,
    select_audio,
    transcribe_catalog,
    transcribe_records,
    upload_audio,
    validate_records,
    validate_term,
    validate_transcript,
    write_audit_report,
    write_catalog,
)


def _audio(root: Path, source: str, level: str, name: str, content: bytes) -> Path:
    path = root / source / level / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _term(index: int) -> str:
    return f"term {chr(ord('a') + index)}"


def _records(count: int = 20) -> list[CatalogRecord]:
    return [
        CatalogRecord(
            f"audio-{sha256(f'catalog-{index}'.encode()).hexdigest()}",
            f"上級SVL/2/STAGE 01\u300000{index:02d} {_term(index)}.mp3",
            "上級SVL",
            2,
            _term(index),
            sha256(f"catalog-{index}".encode()).hexdigest(),
            "A clear English definition includes an example sentence.",
        )
        for index in range(count)
    ]


def _materialize_catalog_audio(root: Path, records: list[CatalogRecord]) -> list[CatalogRecord]:
    result: list[CatalogRecord] = []
    for index, record in enumerate(records):
        content = f"audio-{index}".encode()
        path = root / record.audio_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = sha256(content).hexdigest()
        result.append(replace(record, id=f"audio-{digest}", checksum=digest))
    return result


def test_selection_is_lowest_deterministic_and_approved_only(tmp_path: Path) -> None:
    _audio(tmp_path, "上級SVL", "9", "123 STAGE 08\u30000723 mobilize.mp3", b"a")
    _audio(tmp_path, "上級SVL", "2", "001 STAGE 01\u30000001 alpha term.mp3", b"b")
    _audio(tmp_path, "超上級SVL", "unknown", "STAGE 02\u30000193 careful phrase.mp3", b"c")
    _audio(tmp_path, "超上級SVL", "3", "STAGE 02\u30000194 beta term.mp3", b"d")
    _audio(tmp_path, "上級", "1", "STAGE 01\u30000001 forbidden.mp3", b"x")

    selected = select_audio(tmp_path, limit_per_source=1)

    assert [(item.source, item.level, item.term) for item in selected] == [
        ("上級SVL", 2, "alpha term"),
        ("超上級SVL", 3, "beta term"),
    ]
    assert all(item.audio_key.startswith(item.source) for item in selected)
    assert records_from_audio(selected)[0].id.startswith("audio-")
    with pytest.raises(ValueError):
        select_audio(tmp_path, 0)
    invalid = tmp_path / "outside" / "9" / "STAGE 01\u30000001 invalid.mp3"
    invalid.parent.mkdir(parents=True)
    invalid.touch()
    with pytest.raises(ValueError, match="approved"):
        parse_audio_path(tmp_path, invalid)
    malformed = tmp_path / "上級SVL" / "9" / "not-an-svl-name.mp3"
    malformed.touch()
    with pytest.raises(ValueError, match="unrecognised"):
        parse_audio_path(tmp_path, malformed)


def test_private_catalog_transcribe_resume_and_validation(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    record = _records(1)[0]
    write_catalog(catalog, [record])
    assert read_catalog(catalog) == [record]
    assert read_catalog(tmp_path / "absent.jsonl") == []

    class FakeTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            return "A useful English definition with a short example."

    fake = FakeTranscriber()
    completed = transcribe_records(tmp_path, [record], fake)
    assert completed[0].transcript == record.transcript and fake.calls == 0
    forced = transcribe_records(tmp_path, [record], fake, force=True)
    assert forced[0].transcript is not None and fake.calls == 1
    assert _validate_catalog_records(_records(4), expected_count=4)
    with pytest.raises(ValueError, match="missing transcript"):
        _validate_catalog_records([replace(record, transcript=None)], 1)
    with pytest.raises(ValueError, match="English only"):
        validate_transcript("これは English definition example")
    with pytest.raises(ValueError, match="too short"):
        validate_transcript("too short")
    with pytest.raises(ValueError, match="expected"):
        _validate_catalog_records([], expected_count=1)
    with pytest.raises(ValueError, match="duplicate"):
        _validate_catalog_records([record, record], expected_count=2)
    for non_latin in (
        "A clear definition пример sentence.",
        "A clear definition 한국어 sentence.",
        "A clear definition عربية sentence.",
        "A clear definition 漢字 sentence.",
    ):
        with pytest.raises(ValueError, match="Latin|English only"):
            validate_transcript(non_latin)
    assert validate_transcript("Café gives a clear English definition.")
    assert (
        validate_transcript("“Café”\u2014a useful definition\u2026 with an example.\u00a0")
        == '"Café" - a useful definition... with an example.'
    )
    with pytest.raises(ValueError, match="unsafe Unicode"):
        validate_transcript("A clear\x00 English definition sentence.")
    with pytest.raises(ValueError, match="ASCII punctuation"):
        validate_transcript("A clear English definition sentence. 😀")
    with pytest.raises(ValueError, match="English only|ASCII punctuation"):
        validate_transcript("A clear English definition。 sentence.")
    with pytest.raises(ValueError, match="repeated-word"):
        validate_transcript("An English definition says on on on on on on on on for too long.")
    with pytest.raises(ValueError, match="repeated-phrase"):
        validate_transcript(("A clear English phrase repeats here. " * 8).strip())
    long_english = " ".join(
        f"word{chr(ord('a') + index // 26)}{chr(ord('a') + index % 26)}" for index in range(150)
    )
    assert validate_transcript(long_english) == long_english
    assert validate_term("well-known O'Connor") == "well-known O'Connor"
    with pytest.raises(ValueError, match="ASCII letters"):
        validate_term("term 7")


def test_mlx_transcriber_uses_english_large_turbo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def transcribe(path: str, **kwargs: object) -> dict[str, str]:
        calls["path"] = path
        calls.update(kwargs)
        return {"text": " English definition and example. "}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    result = MlxWhisperTranscriber().transcribe(tmp_path / "audio.mp3")
    assert result == "English definition and example."
    assert calls["language"] == "en"
    assert calls["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert calls["temperature"] == 0.0
    assert calls["condition_on_previous_text"] is False


def test_publish_records_writes_sqlite_words(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    records = _materialize_catalog_audio(tmp_path, _records())
    publish_records(database, records, root=tmp_path)
    repository = SQLiteVocabularyRepository(database, "local-user")
    try:
        words = repository.list_words(limit=30)
        assert len(words) == 20 and words[0].transcript is not None
    finally:
        repository.close()


def test_publish_r2_uses_canonical_keys_and_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    records = _materialize_catalog_audio(tmp_path, _records())
    publish_records(database, records, audio_backend="r2", root=tmp_path)
    repository = SQLiteVocabularyRepository(database, "local-user")
    try:
        words = repository.list_words(limit=30)
        assert len(words) == 20
        assert {word.audio_key for word in words} == {
            r2_audio_key(record.checksum) for record in records
        }
        prior = repository.get_word(records[0].id)
        assert prior is not None
        repository.db.execute(
            "CREATE TRIGGER reject_one BEFORE INSERT ON words WHEN NEW.id="
            f"'{records[10].id}' BEGIN SELECT RAISE(ABORT, 'no'); END"
        )
        changed = [
            replace(
                record, transcript="A different English definition includes an example sentence."
            )
            for record in records
        ]
        with pytest.raises(Exception, match="no"):
            publish_records(database, changed, audio_backend="r2", root=tmp_path)
        assert repository.get_word(records[0].id) == prior
    finally:
        repository.close()


def test_catalog_integrity_rejects_tampering_and_canonical_key() -> None:
    record = _records(1)[0]
    assert r2_audio_key(record.checksum) == f"audio/{record.checksum}.mp3"
    for malformed in ("A" * 64, "a" * 63, "a" * 65, "not-a-checksum"):
        with pytest.raises(ValueError):
            r2_audio_key(malformed)
    for altered in (
        replace(record, id="audio-" + "a" * 64),
        replace(record, audio_key="../outside.mp3"),
        replace(record, source="other"),
        replace(record, term="wrong"),
    ):
        with pytest.raises(ValueError):
            _validate_catalog_records([altered], expected_count=1)


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
    records = _materialize_catalog_audio(tmp_path, _records())
    bad = replace(records[-1], checksum="a" * 64, id="audio-" + "a" * 64)
    client = _UploadClient()
    with pytest.raises(ValueError, match="does not match"):
        upload_audio(tmp_path, [*records[:-1], bad], client, "private")


def test_tampered_source_rejects_validate_and_publish_without_db_mutation(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    records = _materialize_catalog_audio(tmp_path, _records())
    publish_records(database, records, root=tmp_path)
    repository = SQLiteVocabularyRepository(database, "local-user")
    try:
        original = repository.get_word(records[0].id)
        assert original is not None
        (tmp_path / records[-1].audio_key).write_bytes(b"tampered")
        with pytest.raises(ValueError, match="does not match"):
            validate_records(records, root=tmp_path)
        with pytest.raises(ValueError, match="does not match"):
            publish_records(database, records, root=tmp_path)
        assert repository.get_word(records[0].id) == original
    finally:
        repository.close()


def test_publish_requires_source_root_and_rejects_source_mismatch(tmp_path: Path) -> None:
    records = _materialize_catalog_audio(tmp_path, _records())
    with pytest.raises(ValueError, match="requires a source root"):
        publish_records(tmp_path / "vocab.db", records)
    mismatched = replace(records[0], source="超上級SVL")
    with pytest.raises(ValueError, match="source does not match"):
        publish_records(tmp_path / "vocab.db", [mismatched, *records[1:]], root=tmp_path)
    assert not (tmp_path / "vocab.db").exists()


class _CliUploadClient:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts = 0

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
        if Key not in self.objects:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
            raise error
        body, digest = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": {"sha256": digest}}

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
        self.puts += 1
        self.objects[Key] = (Body.read(), Metadata["sha256"])
        return {}


def test_cli_upload_dry_run_needs_no_client_and_normal_upload_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records = _materialize_catalog_audio(tmp_path, _records())
    write_catalog(catalog, records)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vocab-ingest",
            "upload-audio",
            "--source",
            str(tmp_path),
            "--catalog",
            str(catalog),
            "--dry-run",
        ],
    )
    monkeypatch.setattr(
        "english_vocab_trainer.ingest.r2_client_from_env",
        lambda _: (_ for _ in ()).throw(AssertionError("dry run must not construct a client")),
    )
    main()

    client = _CliUploadClient()
    monkeypatch.setattr(
        "english_vocab_trainer.ingest.r2_client_from_env", lambda _: (client, "private")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["vocab-ingest", "upload-audio", "--source", str(tmp_path), "--catalog", str(catalog)],
    )
    main()
    main()
    output = capsys.readouterr().out
    assert "would upload 20" in output and client.puts == 20 and "skipped 20" in output


def test_cli_scan_and_validate_private_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for source in ("上級SVL", "超上級SVL"):
        for index in range(10):
            _audio(
                tmp_path,
                source,
                "2",
                f"STAGE 01\u3000{index:04d} {_term(index)}.mp3",
                f"{source}-{index}".encode(),
            )
    catalog = tmp_path / ".private" / "catalog.jsonl"
    monkeypatch.setattr(
        sys, "argv", ["vocab-ingest", "scan", "--source", str(tmp_path), "--catalog", str(catalog)]
    )
    main()
    records = [
        replace(record, transcript="An English definition has an example.")
        for record in read_catalog(catalog)
    ]
    write_catalog(catalog, records)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vocab-ingest", "validate", "--source", str(tmp_path), "--catalog", str(catalog)],
    )
    main()
    assert "valid: 20" in capsys.readouterr().out


def test_cli_scan_dry_run_does_not_write_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for source in ("上級SVL", "超上級SVL"):
        _audio(tmp_path, source, "2", "STAGE 01\u30000001 sample term.mp3", source.encode())
    catalog = tmp_path / ".private" / "catalog.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vocab-ingest",
            "scan",
            "--source",
            str(tmp_path),
            "--catalog",
            str(catalog),
            "--limit-per-source",
            "1",
            "--dry-run",
        ],
    )
    main()
    assert not catalog.exists() and "would write 2" in capsys.readouterr().out


def test_cli_dry_run_transcribe_and_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, _materialize_catalog_audio(tmp_path, _records()))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vocab-ingest",
            "transcribe",
            "--source",
            str(tmp_path),
            "--catalog",
            str(catalog),
            "--dry-run",
        ],
    )
    main()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vocab-ingest",
            "publish",
            "--source",
            str(tmp_path),
            "--catalog",
            str(catalog),
            "--database",
            str(tmp_path / "v.db"),
            "--dry-run",
        ],
    )
    main()
    output = capsys.readouterr().out
    assert "would transcribe 0" in output and "would publish 20" in output


def test_transcribe_checkpoints_and_resumes_after_failure(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records = [replace(record, transcript=None) for record in _records(2)]
    write_catalog(catalog, records)

    class FailingTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("interrupted")
            return "An English definition with an example."

    with pytest.raises(RuntimeError, match="interrupted"):
        transcribe_catalog(tmp_path, catalog, FailingTranscriber(), force=False)
    assert read_catalog(catalog)[0].transcript is not None

    class ResumingTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            return "An English definition with an example."

    resumed = ResumingTranscriber()
    updates: list[tuple[int, int, str]] = []
    summary = transcribe_catalog(
        tmp_path,
        catalog,
        resumed,
        force=False,
        on_transcribed=lambda completed, total, record: updates.append(
            (completed, total, record.term)
        ),
    )
    assert resumed.calls == 1 and all(record.transcript for record in read_catalog(catalog))
    assert summary.completed == 2 and summary.skipped == 1 and updates == [(2, 2, "term b")]
    with catalog_lock(catalog):
        with pytest.raises(RuntimeError, match="already"):
            with catalog_lock(catalog):
                pass


def test_full_profile_requires_exact_inventory_and_reuses_only_checksum_transcripts(
    tmp_path: Path,
) -> None:
    for source in ("上級SVL", "超上級SVL"):
        for index in range(2):
            _audio(
                tmp_path,
                source,
                "2",
                f"STAGE 01\u3000{index:04d} full term {_term(index)}.mp3",
                f"{source}-{index}".encode(),
            )
    with pytest.raises(ValueError, match="exactly 1000"):
        select_audio(tmp_path, profile=FULL_PROFILE)

    catalog = tmp_path / ".private" / "catalog.jsonl"
    first, _ = scan_catalog(tmp_path, catalog, limit_per_source=2)
    write_catalog(
        catalog, [replace(first[0], transcript="An English definition has an example."), *first[1:]]
    )
    changed = tmp_path / first[1].audio_key
    changed.write_bytes(b"changed audio")
    resumed, _ = scan_catalog(tmp_path, catalog, limit_per_source=2, resume=True)
    assert resumed[0].transcript is not None and resumed[1].transcript is None
    assert resumed[0].term == first[0].term and resumed[1].checksum != first[1].checksum


def test_scan_preserves_existing_state_only_with_resume_or_force(tmp_path: Path) -> None:
    for source in ("上級SVL", "超上級SVL"):
        _audio(tmp_path, source, "2", "STAGE 01　0001 sample term.mp3", source.encode())
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)
    write_catalog(
        catalog,
        [
            replace(record, transcript="An English definition with an example.")
            for record in records
        ],
    )

    with pytest.raises(ValueError, match="--resume or --force"):
        scan_catalog(tmp_path, catalog, limit_per_source=1)
    resumed, _ = scan_catalog(tmp_path, catalog, limit_per_source=1, resume=True)
    assert all(record.transcript for record in resumed)
    forced, _ = scan_catalog(tmp_path, catalog, limit_per_source=1, force=True)
    assert all(record.transcript is None for record in forced)


def test_scan_force_discards_mismatched_pending_journal(tmp_path: Path) -> None:
    for source in ("上級SVL", "超上級SVL"):
        _audio(tmp_path, source, "2", "STAGE 01　0001 sample term.mp3", source.encode())
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)

    class OneTranscription:
        def transcribe(self, _: Path) -> str:
            return "An English definition with an example."

    transcribe_catalog(tmp_path, catalog, OneTranscription(), force=False, max_records=1)
    (tmp_path / records[0].audio_key).write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity"):
        scan_catalog(tmp_path, catalog, limit_per_source=1, resume=True)
    scan_catalog(tmp_path, catalog, limit_per_source=1, force=True)
    assert not journal_path(catalog).exists()


def test_scan_reconciles_or_force_clears_current_error_state(tmp_path: Path) -> None:
    for source in ("上級SVL", "超上級SVL"):
        _audio(tmp_path, source, "2", "STAGE 01　0001 sample term.mp3", source.encode())
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)

    class FailingTranscriber:
        def transcribe(self, _: Path) -> str:
            raise RuntimeError("failed")

    transcribe_catalog(
        tmp_path,
        catalog,
        FailingTranscriber(),
        force=False,
        continue_on_error=True,
        max_records=1,
    )
    error_report = catalog.with_suffix(".jsonl.transcription-errors.jsonl")
    assert error_report.exists()
    with pytest.raises(ValueError, match="--resume or --force"):
        scan_catalog(tmp_path, catalog, limit_per_source=1)
    (tmp_path / records[0].audio_key).write_bytes(b"changed")
    scan_catalog(tmp_path, catalog, limit_per_source=1, resume=True)
    assert not error_report.exists()

    transcribe_catalog(
        tmp_path,
        catalog,
        FailingTranscriber(),
        force=False,
        continue_on_error=True,
        max_records=1,
    )
    assert error_report.exists()
    scan_catalog(tmp_path, catalog, limit_per_source=1, force=True)
    assert not error_report.exists()
    assert not catalog.with_suffix(".jsonl.transcription-errors.journal.jsonl").exists()


def test_journal_handles_truncated_tail_and_continue_on_error(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records = [replace(record, transcript=None) for record in _records(2)]
    write_catalog(catalog, records)

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
    error_journal = catalog.with_suffix(".jsonl.transcription-errors.journal.jsonl")
    with error_journal.open("a", encoding="utf-8") as journal:
        journal.write('{"kind":')
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
    assert not error_journal.exists()


def test_force_transcription_resets_pending_journal(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, [replace(record, transcript=None) for record in _records(2)])

    class StableTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            return "An English definition with an example."

    transcriber = StableTranscriber()
    transcribe_catalog(tmp_path, catalog, transcriber, force=False, max_records=1)
    assert journal_path(catalog).exists()
    summary = transcribe_catalog(tmp_path, catalog, transcriber, force=True, max_records=1)
    assert summary.completed == 1 and transcriber.calls == 2
    # A second forced retry starts from the durable base, never conflicting with its prior journal.
    transcribe_catalog(tmp_path, catalog, transcriber, force=True, max_records=1)


def test_journal_rejects_non_object_entries(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, _records(1))
    journal_path(catalog).write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="journal is corrupt"):
        read_catalog(catalog)


def test_journal_cannot_change_immutable_catalog_fields(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    record = _records(1)[0]
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


def test_scan_rejects_audio_symlink_outside_its_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"not approved")
    linked = tmp_path / "上級SVL" / "2" / "STAGE 01　0001 linked term.mp3"
    linked.parent.mkdir(parents=True)
    os.symlink(outside, linked)
    _audio(tmp_path, "超上級SVL", "2", "STAGE 01　0001 safe term.mp3", b"safe")
    with pytest.raises(ValueError, match="symlink"):
        select_audio(tmp_path, limit_per_source=1)


def test_transcription_batch_caps_failed_attempts(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, [replace(record, transcript=None) for record in _records(2)])

    class FailingTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            raise RuntimeError("not persisted")

    transcriber = FailingTranscriber()
    summary = transcribe_catalog(
        tmp_path,
        catalog,
        transcriber,
        force=False,
        continue_on_error=True,
        max_records=1,
    )
    assert transcriber.calls == 1 and summary.errors == 1 and summary.completed == 0


def test_audit_writes_machine_readable_private_warnings(tmp_path: Path) -> None:
    records = _materialize_catalog_audio(tmp_path, _records(2))
    records = [
        replace(records[0], transcript="An English definition includes an example."),
        replace(records[1], transcript="An English definition includes an example."),
    ]
    valid, warnings = audit_records(tmp_path, records, expected_count=2)
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_audit_report(catalog, None, warnings)
    report = (tmp_path / ".private" / "catalog.jsonl.audit.json").read_text(encoding="utf-8")
    assert len(valid) == 2 and "duplicate-transcript" in report and "term-not-found" in report
    with pytest.raises(ValueError, match="transcript-quality-repeated-word"):
        audit_records(
            tmp_path,
            [
                replace(
                    records[0], transcript="An English definition says on on on on on on on on."
                ),
                records[1],
            ],
            expected_count=2,
        )


def test_audit_catalog_blocks_source_inventory_drift(tmp_path: Path) -> None:
    for source in ("上級SVL", "超上級SVL"):
        for index in range(2):
            term_index = index + (0 if source == "上級SVL" else 10)
            _audio(
                tmp_path,
                source,
                "2",
                f"STAGE 01\u3000{index:04d} audit {_term(term_index)}.mp3",
                f"{source}-{index}".encode(),
            )
    catalog = tmp_path / ".private" / "catalog.jsonl"
    records, _ = scan_catalog(tmp_path, catalog, limit_per_source=2)
    write_catalog(
        catalog,
        [
            replace(record, transcript=f"{record.term} has an English definition and example.")
            for record in records
        ],
    )
    assert audit_catalog(tmp_path, catalog, profile=profile_for("mvp"), limit_per_source=2)[0]
    (tmp_path / records[0].audio_key).write_bytes(b"mutated")
    with pytest.raises(ValueError, match="inventory"):
        audit_catalog(tmp_path, catalog, profile=profile_for("mvp"), limit_per_source=2)


def test_scan_rejects_source_change_while_a_matching_journal_is_pending(tmp_path: Path) -> None:
    for source in ("上級SVL", "超上級SVL"):
        for index in range(2):
            term_index = index + (10 if source == "超上級SVL" else 0)
            _audio(
                tmp_path,
                source,
                "2",
                f"STAGE 01\u3000{index:04d} journal {_term(term_index)}.mp3",
                f"{source}-{index}".encode(),
            )
    catalog = tmp_path / ".private" / "catalog.jsonl"
    scan_catalog(tmp_path, catalog, limit_per_source=2)

    class OneTranscription:
        def transcribe(self, _: Path) -> str:
            return "An English definition with an example."

    transcribe_catalog(tmp_path, catalog, OneTranscription(), force=False, max_records=1)
    first = read_catalog(catalog)[0]
    (tmp_path / first.audio_key).write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity"):
        scan_catalog(tmp_path, catalog, limit_per_source=2, resume=True)


def test_cli_status_accepts_untranscribed_profile_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for source in ("上級SVL", "超上級SVL"):
        for index in range(2):
            term_index = index + (10 if source == "超上級SVL" else 0)
            _audio(
                tmp_path,
                source,
                "2",
                f"STAGE 01\u3000{index:04d} status {_term(term_index)}.mp3",
                f"{source}-{index}".encode(),
            )
    catalog = tmp_path / ".private" / "catalog.jsonl"
    scan_catalog(tmp_path, catalog, limit_per_source=2)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vocab-ingest",
            "status",
            "--source",
            str(tmp_path),
            "--catalog",
            str(catalog),
            "--limit-per-source",
            "2",
        ],
    )
    main()
    assert "status valid: 4 inventory records; transcribed 0" in capsys.readouterr().out


def test_profile_manifest_and_error_report_reject_malformed_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mvp or full"):
        profile_for("other")
    with pytest.raises(ValueError, match="approved source is missing"):
        select_audio(tmp_path)
    catalog = tmp_path / ".private" / "catalog.jsonl"
    manifest_path(catalog).parent.mkdir(parents=True)
    manifest_path(catalog).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is invalid"):
        read_manifest(catalog)
    error_report_path(catalog).write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="error report is corrupt"):
        _read_current_error_report(catalog)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("opaque"), "runtime-failure"),
        (ValueError("transcript-quality-repeated-word"), "transcript-quality-repeated-word"),
        (
            ValueError("transcript must use Latin alphabetic characters only"),
            "transcript-script-invalid",
        ),
        (ValueError("transcript contains unsafe Unicode"), "transcript-unicode-invalid"),
        (
            ValueError("transcript must contain ASCII punctuation only"),
            "transcript-punctuation-invalid",
        ),
        (
            ValueError("transcript contains an unbound combining mark"),
            "transcript-unbound-combining-mark",
        ),
        (ValueError("other validation"), "transcript-validation-failed"),
    ],
)
def test_failure_reason_is_whitelisted(error: Exception, reason: str) -> None:
    assert _failure_reason(error) == reason


def test_journal_and_error_journal_reject_corrupt_identity_and_records(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    record = _records(1)[0]
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
        _merge_journal([], {record.checksum: record})

    error_journal_path(catalog).write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="error journal is corrupt"):
        _error_updates(catalog)
    error_journal_path(catalog).write_text(
        json.dumps({"kind": "header", "fingerprint": "wrong"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="identity"):
        _error_updates(catalog, "expected")


@pytest.mark.parametrize("duplicate", ["path", "checksum", "term"])
def test_full_profile_rejects_all_duplicate_identity_dimensions(
    monkeypatch: pytest.MonkeyPatch, duplicate: str
) -> None:
    def candidates(source: str) -> list[AudioCandidate]:
        return [
            AudioCandidate(
                Path(f"/{source}-{index}.mp3"),
                "shared.mp3" if duplicate == "path" else f"{source}-{index}.mp3",
                source,
                1,
                "shared"
                if duplicate == "term"
                else f"term{chr(ord('a') + index // 26)}{chr(ord('a') + index % 26)}",
            )
            for index in range(1000)
        ]

    monkeypatch.setattr(
        "english_vocab_trainer.ingest._source_candidates", lambda _root, source: candidates(source)
    )
    monkeypatch.setattr(
        "english_vocab_trainer.ingest.checksum",
        lambda path: (
            "a" * 64 if duplicate == "checksum" else sha256(str(path).encode()).hexdigest()
        ),
    )
    with pytest.raises(ValueError, match=f"duplicate audio {duplicate}s|duplicate terms"):
        select_audio(Path("/unused"), profile=FULL_PROFILE)
