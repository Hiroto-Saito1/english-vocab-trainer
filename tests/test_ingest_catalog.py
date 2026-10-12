from __future__ import annotations

import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from ingest_support import approved_source_tree, audio, materialize_catalog_audio, records

from english_vocab_trainer.ingest.catalog import (
    audit_catalog,
    audit_records,
    records_from_audio,
    scan_catalog,
    select_audio,
)
from english_vocab_trainer.ingest.models import FULL_PROFILE, MVP_PROFILE, AudioCandidate
from english_vocab_trainer.ingest.storage import (
    journal_path,
    read_catalog,
    write_audit_report,
    write_catalog,
)
from english_vocab_trainer.ingest.transcription import transcribe_catalog
from english_vocab_trainer.ingest.validation import parse_audio_path


def test_selection_is_lowest_deterministic_and_approved_only(tmp_path: Path) -> None:
    audio(tmp_path, "上級SVL", "9", "123 STAGE 08　0723 mobilize.mp3", b"a")
    audio(tmp_path, "上級SVL", "2", "001 STAGE 01　0001 alpha term.mp3", b"b")
    audio(tmp_path, "超上級SVL", "unknown", "STAGE 02　0193 careful phrase.mp3", b"c")
    audio(tmp_path, "超上級SVL", "3", "STAGE 02　0194 beta term.mp3", b"d")
    audio(tmp_path, "上級", "1", "STAGE 01　0001 forbidden.mp3", b"x")

    selected = select_audio(tmp_path, limit_per_source=1)

    assert [(item.source, item.level, item.term) for item in selected] == [
        ("上級SVL", 2, "alpha term"),
        ("超上級SVL", 3, "beta term"),
    ]
    assert all(item.audio_key.startswith(item.source) for item in selected)
    assert records_from_audio(selected)[0].id.startswith("audio-")
    with pytest.raises(ValueError):
        select_audio(tmp_path, 0)
    with pytest.raises(ValueError):
        MVP_PROFILE.expected_count(0)
    invalid = tmp_path / "outside" / "9" / "STAGE 01　0001 invalid.mp3"
    invalid.parent.mkdir(parents=True)
    invalid.touch()
    with pytest.raises(ValueError, match="approved"):
        parse_audio_path(tmp_path, invalid)
    malformed = tmp_path / "上級SVL" / "9" / "not-an-svl-name.mp3"
    malformed.touch()
    with pytest.raises(ValueError, match="unrecognised"):
        parse_audio_path(tmp_path, malformed)


def test_full_profile_requires_exact_inventory_and_reuses_only_checksum_transcripts(
    tmp_path: Path,
) -> None:
    approved_source_tree(tmp_path, 2, label="full")
    with pytest.raises(ValueError, match="exactly 1000"):
        select_audio(tmp_path, profile=FULL_PROFILE)

    catalog = tmp_path / ".private" / "catalog.jsonl"
    first, _ = scan_catalog(tmp_path, catalog, limit_per_source=2)
    write_catalog(
        catalog, [replace(first[0], transcript="An English definition has an example."), *first[1:]]
    )
    changed = tmp_path / first[1].audio_key
    changed.write_bytes(b"changed audio")
    resumed, _ = scan_catalog(tmp_path, catalog, limit_per_source=2)
    assert resumed[0].transcript is not None and resumed[1].transcript is None
    assert resumed[0].term == first[0].term and resumed[1].checksum != first[1].checksum


def test_scan_reuses_validated_state_by_default_and_force_discards_it(tmp_path: Path) -> None:
    approved_source_tree(tmp_path, label="sample")
    catalog = tmp_path / ".private" / "catalog.jsonl"
    existing, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)
    write_catalog(
        catalog,
        [
            replace(record, transcript="An English definition with an example.")
            for record in existing
        ],
    )

    resumed, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)
    assert all(record.transcript for record in resumed)
    forced, _ = scan_catalog(tmp_path, catalog, limit_per_source=1, force=True)
    assert all(record.transcript is None for record in forced)


def test_scan_force_discards_mismatched_pending_journal(tmp_path: Path) -> None:
    approved_source_tree(tmp_path, label="sample")
    catalog = tmp_path / ".private" / "catalog.jsonl"
    current, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)

    class OneTranscription:
        def transcribe(self, _: Path) -> str:
            return "An English definition with an example."

    transcribe_catalog(tmp_path, catalog, OneTranscription(), force=False, max_records=1)
    (tmp_path / current[0].audio_key).write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity"):
        scan_catalog(tmp_path, catalog, limit_per_source=1)
    scan_catalog(tmp_path, catalog, limit_per_source=1, force=True)
    assert not journal_path(catalog).exists()


def test_scan_reconciles_or_force_clears_current_error_state(tmp_path: Path) -> None:
    approved_source_tree(tmp_path, label="sample")
    catalog = tmp_path / ".private" / "catalog.jsonl"
    current, _ = scan_catalog(tmp_path, catalog, limit_per_source=1)

    class FailingTranscriber:
        def transcribe(self, _: Path) -> str:
            raise RuntimeError("failed")

    transcribe_catalog(
        tmp_path, catalog, FailingTranscriber(), force=False, continue_on_error=True, max_records=1
    )
    error_report = catalog.with_suffix(".jsonl.transcription-errors.jsonl")
    assert error_report.exists()
    (tmp_path / current[0].audio_key).write_bytes(b"changed")
    scan_catalog(tmp_path, catalog, limit_per_source=1)
    assert not error_report.exists()

    transcribe_catalog(
        tmp_path, catalog, FailingTranscriber(), force=False, continue_on_error=True, max_records=1
    )
    assert error_report.exists()
    scan_catalog(tmp_path, catalog, limit_per_source=1, force=True)
    assert not error_report.exists()


def test_scan_rejects_audio_symlink_outside_its_source(tmp_path: Path) -> None:
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"not approved")
    linked = tmp_path / "上級SVL" / "2" / "STAGE 01　0001 linked term.mp3"
    linked.parent.mkdir(parents=True)
    os.symlink(outside, linked)
    audio(tmp_path, "超上級SVL", "2", "STAGE 01　0001 safe term.mp3", b"safe")
    with pytest.raises(ValueError, match="symlink"):
        select_audio(tmp_path, limit_per_source=1)


def test_audit_writes_machine_readable_private_warnings(tmp_path: Path) -> None:
    current = materialize_catalog_audio(tmp_path, records(2))
    current = [
        replace(current[0], transcript="An English definition includes an example."),
        replace(current[1], transcript="An English definition includes an example."),
    ]
    valid, warnings = audit_records(tmp_path, current, expected_count=2)
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_audit_report(catalog, None, warnings)
    report = (tmp_path / ".private" / "catalog.jsonl.audit.json").read_text(encoding="utf-8")
    assert len(valid) == 2 and "duplicate-transcript" in report and "term-not-found" in report
    with pytest.raises(ValueError, match="transcript-quality-repeated-word"):
        audit_records(
            tmp_path,
            [
                replace(
                    current[0], transcript="An English definition says on on on on on on on on."
                ),
                current[1],
            ],
            expected_count=2,
        )


def test_audit_catalog_blocks_source_inventory_drift(tmp_path: Path) -> None:
    approved_source_tree(tmp_path, 2, label="audit")
    catalog = tmp_path / ".private" / "catalog.jsonl"
    current, _ = scan_catalog(tmp_path, catalog, limit_per_source=2)
    write_catalog(
        catalog,
        [
            replace(record, transcript=f"{record.term} has an English definition and example.")
            for record in current
        ],
    )
    assert audit_catalog(tmp_path, catalog, profile=MVP_PROFILE, limit_per_source=2)[0]
    (tmp_path / current[0].audio_key).write_bytes(b"mutated")
    with pytest.raises(ValueError, match="inventory"):
        audit_catalog(tmp_path, catalog, profile=MVP_PROFILE, limit_per_source=2)


def test_scan_rejects_source_change_while_a_matching_journal_is_pending(tmp_path: Path) -> None:
    approved_source_tree(tmp_path, 2, label="journal")
    catalog = tmp_path / ".private" / "catalog.jsonl"
    scan_catalog(tmp_path, catalog, limit_per_source=2)

    class OneTranscription:
        def transcribe(self, _: Path) -> str:
            return "An English definition with an example."

    transcribe_catalog(tmp_path, catalog, OneTranscription(), force=False, max_records=1)
    first = read_catalog(catalog)[0]
    (tmp_path / first.audio_key).write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity"):
        scan_catalog(tmp_path, catalog, limit_per_source=2)


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
        "english_vocab_trainer.ingest.catalog._source_candidates",
        lambda _root, source: candidates(source),
    )
    monkeypatch.setattr(
        "english_vocab_trainer.ingest.catalog.checksum",
        lambda path: (
            "a" * 64 if duplicate == "checksum" else sha256(str(path).encode()).hexdigest()
        ),
    )
    with pytest.raises(ValueError, match=f"duplicate audio {duplicate}s|duplicate terms"):
        select_audio(Path("/unused"), profile=FULL_PROFILE)
