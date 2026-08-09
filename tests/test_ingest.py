from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.ingest import (
    CatalogRecord,
    MlxWhisperTranscriber,
    catalog_lock,
    main,
    parse_audio_path,
    publish_records,
    r2_audio_key,
    read_catalog,
    records_from_audio,
    select_audio,
    transcribe_catalog,
    transcribe_records,
    upload_audio,
    validate_records,
    validate_transcript,
    write_catalog,
)


def _audio(root: Path, source: str, level: str, name: str, content: bytes) -> Path:
    path = root / source / level / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _records(count: int = 20) -> list[CatalogRecord]:
    return [
        CatalogRecord(
            f"audio-{sha256(f'catalog-{index}'.encode()).hexdigest()}",
            f"上級SVL/2/STAGE 01\u300000{index:02d} term {index}.mp3",
            "上級SVL",
            2,
            f"term {index}",
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
    assert validate_records(_records(4), expected_count=4)
    with pytest.raises(ValueError, match="missing transcript"):
        validate_records([replace(record, transcript=None)], 1)
    with pytest.raises(ValueError, match="English only"):
        validate_transcript("これは English definition example")
    with pytest.raises(ValueError, match="too short"):
        validate_transcript("too short")
    with pytest.raises(ValueError, match="expected"):
        validate_records([], expected_count=1)
    with pytest.raises(ValueError, match="duplicate"):
        validate_records([record, record], expected_count=2)


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


def test_publish_records_writes_sqlite_words(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    publish_records(database, _records())
    repository = SQLiteVocabularyRepository(database, "local-user")
    try:
        words = repository.list_words(limit=30)
        assert len(words) == 20 and words[0].transcript is not None
    finally:
        repository.close()


def test_publish_r2_uses_canonical_keys_and_rolls_back_as_one_transaction(tmp_path: Path) -> None:
    database = tmp_path / "vocab.db"
    records = _records()
    publish_records(database, records, audio_backend="r2")
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
            publish_records(database, changed, audio_backend="r2")
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
            validate_records([altered], expected_count=1)


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
                f"STAGE 01\u3000{index:04d} term {index}.mp3",
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
    assert summary.completed == 2 and summary.skipped == 1 and updates == [(2, 2, "term 1")]
    with catalog_lock(catalog):
        with pytest.raises(RuntimeError, match="already"):
            with catalog_lock(catalog):
                pass
