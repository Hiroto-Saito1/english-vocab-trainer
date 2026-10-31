"""Build a private, deterministic D1 catalog import without learning history."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from english_vocab_trainer.ports.audio import r2_audio_key

from .catalog import audit_records
from .models import CatalogRecord
from .storage import write_text_atomically


def _literal(value: str | int | None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if "\x00" in value:
        raise ValueError("SQL text cannot contain NUL")
    return "'" + value.replace("'", "''") + "'"


def export_d1(
    root: Path,
    records: list[CatalogRecord],
    output: Path,
    *,
    expected_count: int,
    published_at: str | None = None,
    dry_run: bool = False,
) -> tuple[str, int]:
    valid, _warnings = audit_records(root, records, expected_count=expected_count)
    rows = sorted(
        ((r.id, r.term, r.level, r.transcript, r2_audio_key(r.checksum)) for r in valid),
        key=lambda row: row[0],
    )
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not dry_run:
        timestamp = published_at or datetime.now(UTC).isoformat(timespec="seconds")
        statements = [
            "INSERT INTO words (id,term,level,definition,audio_key) VALUES ("
            + ",".join(_literal(value) for value in row)
            + ") ON CONFLICT(id) DO UPDATE SET term=excluded.term,level=excluded.level,"
            "definition=excluded.definition,audio_key=excluded.audio_key;"
            for row in rows
        ]
        statements.append(
            "UPDATE schema_metadata SET catalog_sha256="
            + _literal(digest)
            + ",catalog_count="
            + str(len(rows))
            + ",published_at="
            + _literal(timestamp)
            + " WHERE singleton=1;"
        )
        write_text_atomically(output, "\n".join(statements) + "\n")
    return digest, len(rows)
