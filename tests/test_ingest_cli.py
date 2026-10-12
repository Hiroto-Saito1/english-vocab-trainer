from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from ingest_support import approved_source_tree, materialize_catalog_audio, records

from english_vocab_trainer.ingest.catalog import scan_catalog
from english_vocab_trainer.ingest.cli import main
from english_vocab_trainer.ingest.storage import read_catalog, write_catalog


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
    current = materialize_catalog_audio(tmp_path, records())
    write_catalog(catalog, current)
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
        "english_vocab_trainer.ingest.cli.r2_client_from_env",
        lambda _: (_ for _ in ()).throw(AssertionError("dry run must not construct a client")),
    )
    main()

    client = _CliUploadClient()
    monkeypatch.setattr(
        "english_vocab_trainer.ingest.cli.r2_client_from_env", lambda _: (client, "private")
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
    approved_source_tree(tmp_path, 10)
    catalog = tmp_path / ".private" / "catalog.jsonl"
    monkeypatch.setattr(
        sys, "argv", ["vocab-ingest", "scan", "--source", str(tmp_path), "--catalog", str(catalog)]
    )
    main()
    current = [
        replace(record, transcript="An English definition has an example.")
        for record in read_catalog(catalog)
    ]
    write_catalog(catalog, current)
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
    approved_source_tree(tmp_path, label="sample")
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
    write_catalog(catalog, materialize_catalog_audio(tmp_path, records()))
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


def test_cli_status_accepts_untranscribed_profile_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    approved_source_tree(tmp_path, 2, label="status")
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
