"""Scan, reconcile, and audit private SVL source inventories."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .models import (
    APPROVED_SOURCES,
    CATALOG_SCHEMA_VERSION,
    FULL_PROFILE,
    MVP_PROFILE,
    TRANSCRIPTION_MODEL,
    AudioCandidate,
    CatalogManifest,
    CatalogProfile,
    CatalogRecord,
)
from .storage import (
    catalog_lock,
    error_report_path,
    journal_path,
    merge_journal_records,
    read_catalog,
    read_error_report,
    read_journal_updates,
    read_manifest,
    remove_durable_file,
    write_catalog,
    write_error_report,
    write_manifest,
)
from .validation import checksum, parse_audio_path, validate_records, validate_transcript


@dataclass(frozen=True, slots=True)
class _CatalogSnapshot:
    records: list[CatalogRecord]
    manifest: CatalogManifest


def _source_candidates(root: Path, source: str) -> list[AudioCandidate]:
    candidates: list[AudioCandidate] = []
    source_root = root / source
    if not source_root.is_dir():
        raise ValueError(f"approved source is missing: {source}")
    expected_root = root.resolve() / source
    if not source_root.resolve().is_relative_to(expected_root):
        raise ValueError("approved source symlink is outside its source tree")
    for path in source_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".mp3":
            continue
        if not path.resolve().is_relative_to(expected_root):
            raise ValueError("audio symlink is outside its approved source tree")
        level, term = parse_audio_path(root, path)
        candidates.append(
            AudioCandidate(path, path.relative_to(root).as_posix(), source, level, term)
        )
    candidates.sort(
        key=lambda item: (
            item.level is None,
            item.level if item.level is not None else 999,
            item.audio_key,
        )
    )
    return candidates


def inventory_digest(candidates: Iterable[AudioCandidate | CatalogRecord]) -> str:
    payload = "\n".join(
        f"{item.audio_key}\t{item.checksum}\t{item.term}"
        for item in sorted(candidates, key=lambda item: item.audio_key)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def select_audio(
    root: Path, limit_per_source: int = 10, *, profile: CatalogProfile = MVP_PROFILE
) -> list[AudioCandidate]:
    """Choose the lowest levels, deterministically, from only approved trees."""
    if limit_per_source < 1:
        raise ValueError("limit_per_source must be positive")
    selected: list[AudioCandidate] = []
    full_profile = profile == FULL_PROFILE
    candidates_by_source = {source: _source_candidates(root, source) for source in APPROVED_SOURCES}
    if full_profile:
        for source, candidates in candidates_by_source.items():
            if len(candidates) != FULL_PROFILE.per_source:
                raise ValueError(
                    f"full profile requires exactly {FULL_PROFILE.per_source} MP3 files in {source}"
                )
    for source in APPROVED_SOURCES:
        candidates = candidates_by_source[source]
        wanted = profile.per_source if profile.per_source is not None else limit_per_source
        chosen = candidates if full_profile else candidates[:wanted]
        completed = [replace(candidate, checksum=checksum(candidate.path)) for candidate in chosen]
        selected.extend(completed)
    if full_profile:
        if len({item.audio_key for item in selected}) != len(selected):
            raise ValueError("full profile contains duplicate audio paths")
        if len({item.checksum for item in selected}) != len(selected):
            raise ValueError("full profile contains duplicate audio checksums")
        if len({item.term.casefold() for item in selected}) != len(selected):
            raise ValueError("full profile contains duplicate terms")
    return selected


def records_from_audio(candidates: Iterable[AudioCandidate]) -> list[CatalogRecord]:
    return [
        CatalogRecord(
            id=f"audio-{candidate.checksum}",
            audio_key=candidate.audio_key,
            source=candidate.source,
            level=candidate.level,
            term=candidate.term,
            checksum=candidate.checksum,
        )
        for candidate in candidates
    ]


def _catalog_snapshot(
    root: Path, profile: CatalogProfile, limit_per_source: int
) -> _CatalogSnapshot:
    candidates = select_audio(root, limit_per_source, profile=profile)
    records = records_from_audio(candidates)
    expected = profile.expected_count(limit_per_source)
    if len(records) != expected:
        raise ValueError(
            f"profile {profile.name} expected {expected} records, found {len(records)}"
        )
    return _CatalogSnapshot(
        records,
        CatalogManifest(
            CATALOG_SCHEMA_VERSION,
            profile.name,
            expected,
            inventory_digest(candidates),
            TRANSCRIPTION_MODEL,
            "en",
        ),
    )


def _reconcile_existing_catalog(
    catalog: Path,
    base_records: list[CatalogRecord],
    snapshot: _CatalogSnapshot,
    *,
    pending_journal: bool,
) -> tuple[list[CatalogRecord], dict[str, dict[str, str]]]:
    prior_manifest = read_manifest(catalog)
    if prior_manifest is None:
        raise ValueError("catalog state exists without a provenance manifest")
    prior_records = validate_records(
        base_records,
        prior_manifest.expected_count,
        require_transcript=False,
    )
    if inventory_digest(prior_records) != prior_manifest.inventory_digest:
        raise ValueError("catalog inventory does not match provenance manifest")

    updates: dict[str, CatalogRecord] = {}
    if pending_journal:
        updates = read_journal_updates(catalog, prior_manifest.fingerprint)
        if prior_manifest.fingerprint != snapshot.manifest.fingerprint:
            raise ValueError("catalog identity does not match transcription journal")

    unresolved = read_error_report(catalog)
    prior = {record.checksum: record for record in merge_journal_records(base_records, updates)}
    records = [
        replace(record, transcript=validate_transcript(old.transcript))
        if (old := prior.get(record.checksum)) is not None and old.transcript is not None
        else record
        for record in snapshot.records
    ]
    current_missing = {record.checksum for record in records if record.transcript is None}
    return records, {
        checksum_value: error
        for checksum_value, error in unresolved.items()
        if checksum_value in current_missing
    }


def scan_catalog(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile = MVP_PROFILE,
    limit_per_source: int = 10,
    force: bool = False,
) -> tuple[list[CatalogRecord], CatalogManifest]:
    """Scan audio and reuse transcript checkpoints only after validating their provenance."""
    with catalog_lock(catalog):
        base_records = read_catalog(catalog, include_journal=False)
        pending_journal = journal_path(catalog).exists()
        pending_error_report = error_report_path(catalog).exists()
        snapshot = _catalog_snapshot(root, profile, limit_per_source)

        if force:
            write_catalog(catalog, snapshot.records)
            write_manifest(catalog, snapshot.manifest)
            remove_durable_file(journal_path(catalog))
            remove_durable_file(error_report_path(catalog))
            return snapshot.records, snapshot.manifest

        if not base_records and not pending_journal and not pending_error_report:
            write_catalog(catalog, snapshot.records)
            write_manifest(catalog, snapshot.manifest)
            return snapshot.records, snapshot.manifest

        records, unresolved = _reconcile_existing_catalog(
            catalog, base_records, snapshot, pending_journal=pending_journal
        )

        write_catalog(catalog, records)
        write_manifest(catalog, snapshot.manifest)
        if pending_journal:
            remove_durable_file(journal_path(catalog))

        write_error_report(catalog, unresolved)
    return records, snapshot.manifest


def audit_records(
    root: Path, records: Iterable[CatalogRecord], *, expected_count: int
) -> tuple[list[CatalogRecord], list[dict[str, str]]]:
    """Blocking catalog inventory checks plus non-blocking manual-review warnings."""
    valid = validate_records(records, expected_count, root=root)
    warnings: list[dict[str, str]] = []
    transcripts: dict[str, CatalogRecord] = {}
    for record in valid:
        transcript = record.transcript or ""
        folded = transcript.casefold()
        if record.term.casefold() not in folded:
            warnings.append(
                {"kind": "term-not-found", "checksum": record.checksum, "term": record.term}
            )
        if len(transcript) < 40 or len(transcript) > 2_000:
            warnings.append(
                {
                    "kind": "unusual-transcript-length",
                    "checksum": record.checksum,
                    "term": record.term,
                }
            )
        duplicate = transcripts.get(folded)
        if duplicate is not None:
            warnings.append(
                {"kind": "duplicate-transcript", "checksum": record.checksum, "term": record.term}
            )
        else:
            transcripts[folded] = record
    return valid, warnings


def _verify_catalog_inventory(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile,
    limit_per_source: int,
) -> int:
    manifest = read_manifest(catalog)
    if manifest is None:
        raise ValueError("catalog provenance manifest is missing")
    expected = profile.expected_count(limit_per_source)
    if (
        manifest.schema_version != CATALOG_SCHEMA_VERSION
        or manifest.profile != profile.name
        or manifest.expected_count != expected
    ):
        raise ValueError("catalog profile does not match provenance manifest")
    candidates = select_audio(root, limit_per_source, profile=profile)
    if inventory_digest(candidates) != manifest.inventory_digest:
        raise ValueError("approved source inventory does not match catalog provenance")
    return expected


def audit_catalog(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile,
    limit_per_source: int,
) -> tuple[list[CatalogRecord], list[dict[str, str]]]:
    """Verify catalog provenance against the current immutable source inventory."""
    expected = _verify_catalog_inventory(
        root, catalog, profile=profile, limit_per_source=limit_per_source
    )
    return audit_records(root, read_catalog(catalog), expected_count=expected)


def catalog_status(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile,
    limit_per_source: int,
) -> tuple[int, int]:
    """Check provenance and immutable inventory before any transcript exists."""
    expected = _verify_catalog_inventory(
        root, catalog, profile=profile, limit_per_source=limit_per_source
    )
    records = validate_records(read_catalog(catalog), expected, root=root, require_transcript=False)
    return len(records), sum(record.transcript is not None for record in records)
