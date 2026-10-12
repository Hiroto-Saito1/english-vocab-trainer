"""Durable catalog, manifest, journal, report, and lock persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from .models import (
    CATALOG_SCHEMA_VERSION,
    PROFILES,
    TRANSCRIPTION_ERROR_REASONS,
    CatalogManifest,
    CatalogRecord,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def manifest_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".manifest.json")


def journal_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".journal.jsonl")


def report_path(catalog: Path, name: str) -> Path:
    return catalog.with_suffix(catalog.suffix + f".{name}.json")


def error_report_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".transcription-errors.jsonl")


def write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_name = temporary.name
    os.replace(temporary_name, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def remove_durable_file(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_manifest(catalog: Path) -> CatalogManifest | None:
    path = manifest_path(catalog)
    if not path.exists():
        return None
    try:
        manifest = CatalogManifest(**json.loads(path.read_text(encoding="utf-8")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("catalog provenance manifest is invalid") from exc
    if (
        manifest.schema_version != CATALOG_SCHEMA_VERSION
        or manifest.profile not in PROFILES
        or manifest.expected_count < 1
        or _SHA256.fullmatch(manifest.inventory_digest) is None
        or manifest.language != "en"
        or not manifest.model
    ):
        raise ValueError("catalog provenance manifest is invalid")
    return manifest


def write_manifest(catalog: Path, manifest: CatalogManifest) -> None:
    write_text_atomically(
        manifest_path(catalog), json.dumps(asdict(manifest), sort_keys=True) + "\n"
    )


def read_journal_updates(catalog: Path, fingerprint: str | None = None) -> dict[str, CatalogRecord]:
    path = journal_path(catalog)
    if not path.exists():
        return {}
    updates: dict[str, CatalogRecord] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    header_seen = False
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise ValueError("transcription journal is corrupt") from None
        if not isinstance(entry, dict):
            raise ValueError("transcription journal is corrupt")
        if entry.get("kind") == "header":
            if header_seen or not isinstance(entry.get("fingerprint"), str):
                raise ValueError("transcription journal is corrupt")
            header_seen = True
            if fingerprint is not None and entry.get("fingerprint") != fingerprint:
                raise ValueError("catalog identity does not match transcription journal")
            continue
        if (
            not header_seen
            or entry.get("kind") != "record"
            or not isinstance(entry.get("record"), dict)
        ):
            raise ValueError("transcription journal is corrupt")
        record = CatalogRecord(**entry["record"])
        previous = updates.get(record.checksum)
        if previous is not None and previous != record:
            raise ValueError("transcription journal has conflicting duplicate record")
        updates[record.checksum] = record
    return updates


def merge_journal_records(
    base_records: Iterable[CatalogRecord], updates: dict[str, CatalogRecord]
) -> list[CatalogRecord]:
    """Apply checkpoint text only after proving every immutable field still matches base."""
    base = list(base_records)
    by_checksum = {record.checksum: record for record in base}
    for checksum_value, update in updates.items():
        original = by_checksum.get(checksum_value)
        if original is None:
            raise ValueError("transcription journal record is absent from catalog base")
        if replace(update, transcript=None) != replace(original, transcript=None):
            raise ValueError("transcription journal changes immutable catalog metadata")
    return [updates.get(record.checksum, record) for record in base]


def _repair_truncated_journal_tail(path: Path) -> None:
    """Discard only an undecodable final write before appending a fresh checkpoint."""
    if not path.exists() or path.stat().st_size == 0:
        return
    raw = path.read_bytes()
    if raw.endswith(b"\n"):
        return
    prefix, separator, tail = raw.rpartition(b"\n")
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        retained = prefix + separator
        with path.open("r+b") as journal:
            journal.truncate(len(retained))
            journal.flush()
            os.fsync(journal.fileno())
        return
    with path.open("ab") as journal:
        journal.write(b"\n")
        journal.flush()
        os.fsync(journal.fileno())


def append_journal_record(catalog: Path, fingerprint: str, record: CatalogRecord) -> None:
    path = journal_path(catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_truncated_journal_tail(path)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8") as journal:
        if new:
            journal.write(
                json.dumps({"kind": "header", "fingerprint": fingerprint}, sort_keys=True) + "\n"
            )
        journal.write(
            json.dumps({"kind": "record", "record": asdict(record)}, sort_keys=True) + "\n"
        )
        journal.flush()
        os.fsync(journal.fileno())


def write_error_report(catalog: Path, unresolved: dict[str, dict[str, str]]) -> None:
    if not unresolved:
        remove_durable_file(error_report_path(catalog))
        return
    text = "".join(
        json.dumps(unresolved[checksum_value], sort_keys=True) + "\n"
        for checksum_value in sorted(unresolved)
    )
    write_text_atomically(error_report_path(catalog), text)


def read_error_report(catalog: Path) -> dict[str, dict[str, str]]:
    path = error_report_path(catalog)
    if not path.exists():
        return {}
    result: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("transcription error report is corrupt") from exc
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("checksum"), str)
            or _SHA256.fullmatch(entry["checksum"]) is None
            or not isinstance(entry.get("term"), str)
            or entry.get("reason") not in TRANSCRIPTION_ERROR_REASONS
        ):
            raise ValueError("transcription error report is corrupt")
        previous = result.get(entry["checksum"])
        if previous is not None and previous != entry:
            raise ValueError("transcription error report is corrupt")
        result[entry["checksum"]] = {
            "checksum": entry["checksum"],
            "term": entry["term"],
            "reason": entry["reason"],
        }
    return result


def read_catalog(path: Path, *, include_journal: bool = True) -> list[CatalogRecord]:
    if not path.exists():
        return []
    records = [
        CatalogRecord(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not include_journal:
        return records
    manifest = read_manifest(path)
    updates = read_journal_updates(path, manifest.fingerprint if manifest is not None else None)
    return merge_journal_records(records, updates) if updates else records


def write_catalog(path: Path, records: Iterable[CatalogRecord]) -> None:
    text = "".join(json.dumps(asdict(record), sort_keys=True) + "\n" for record in records)
    write_text_atomically(path, text)


def catalog_fingerprint(records: Iterable[CatalogRecord]) -> str:
    identity = [
        {"audio_key": record.audio_key, "checksum": record.checksum, "term": record.term}
        for record in sorted(records, key=lambda item: item.audio_key)
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@contextmanager
def catalog_lock(path: Path) -> Iterator[None]:
    """Fail fast if another transcriber owns this private catalog."""
    import fcntl

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("catalog is already being transcribed") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def write_audit_report(
    catalog: Path, manifest: CatalogManifest | None, warnings: list[dict[str, str]]
) -> None:
    payload: dict[str, object] = {"warnings": warnings, "warning_count": len(warnings)}
    if manifest is not None:
        payload["manifest"] = asdict(manifest)
    write_text_atomically(report_path(catalog, "audit"), json.dumps(payload, sort_keys=True) + "\n")
