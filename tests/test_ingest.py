from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.ingest import (
    CatalogRecord,
    MlxWhisperTranscriber,
    main,
    parse_audio_path,
    publish_records,
    read_catalog,
    records_from_audio,
    select_audio,
    transcribe_records,
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
            f"audio-{index}",
            f"上級SVL/2/STAGE 01\u300000{index:02d} term {index}.mp3",
            "上級SVL",
            2,
            f"term {index}",
            f"checksum-{index}",
            "A clear English definition includes an example sentence.",
        )
        for index in range(count)
    ]


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
    write_catalog(catalog, _records())
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
