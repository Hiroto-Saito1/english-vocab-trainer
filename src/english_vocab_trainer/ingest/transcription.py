from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .models import (
    TRANSCRIPTION_MODEL,
    CatalogRecord,
    TranscriptionSummary,
)
from .storage import (
    append_journal_record,
    catalog_fingerprint,
    catalog_lock,
    journal_path,
    merge_journal_records,
    read_catalog,
    read_error_report,
    read_journal_updates,
    read_manifest,
    remove_durable_file,
    write_catalog,
    write_error_report,
)
from .validation import transcription_failure_reason, validate_transcript


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> str: ...


class MlxWhisperTranscriber:
    """Lazy MLX wrapper so scan/publish do not require a downloaded model."""

    model = TRANSCRIPTION_MODEL
    temperature = 0.0
    condition_on_previous_text = False

    def transcribe(self, path: Path) -> str:
        try:
            import mlx_whisper
        except ImportError:  # pragma: no cover - depends on optional group
            raise RuntimeError(
                "MLX is not installed; run scripts/ingest-env doctor before transcription"
            ) from None
        except Exception:  # pragma: no cover - depends on machine-specific MLX loading
            raise RuntimeError(
                "MLX could not load; run scripts/ingest-env doctor before transcription"
            ) from None
        result: Any = mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=self.model,
            language="en",
            temperature=self.temperature,
            condition_on_previous_text=self.condition_on_previous_text,
        )
        return str(result["text"]).strip()


def transcribe_catalog(
    root: Path,
    catalog: Path,
    transcriber: Transcriber,
    *,
    force: bool,
    continue_on_error: bool = False,
    max_records: int | None = None,
    on_transcribed: Callable[[int, int, CatalogRecord], None] | None = None,
) -> TranscriptionSummary:
    with catalog_lock(catalog):
        base_records = read_catalog(catalog, include_journal=False)
        manifest = read_manifest(catalog)
        fingerprint = (
            manifest.fingerprint if manifest is not None else catalog_fingerprint(base_records)
        )
        if force:
            remove_durable_file(journal_path(catalog))
            updates: dict[str, CatalogRecord] = {}
        else:
            updates = read_journal_updates(catalog, fingerprint)
        records = merge_journal_records(base_records, updates)
        unresolved = read_error_report(catalog)
        pending_checksums = {record.checksum for record in records if record.transcript is None}
        unresolved = {
            checksum_value: error
            for checksum_value, error in unresolved.items()
            if checksum_value in pending_checksums
        }
        write_error_report(catalog, unresolved)
        total = len(records)
        completed = 0 if force else sum(record.transcript is not None for record in records)
        skipped = completed
        attempted = 0
        for index, record in enumerate(records):
            if record.transcript is not None and not force:
                continue
            if max_records is not None and attempted >= max_records:
                break
            attempted += 1
            try:
                transcript = validate_transcript(transcriber.transcribe(root / record.audio_key))
            except Exception as exc:
                reason = transcription_failure_reason(exc)
                unresolved[record.checksum] = {
                    "checksum": record.checksum,
                    "term": record.term,
                    "reason": reason,
                }
                write_error_report(catalog, unresolved)
                if continue_on_error:
                    continue
                raise
            records[index] = replace(record, transcript=transcript)
            append_journal_record(catalog, fingerprint, records[index])
            if record.checksum in unresolved:
                unresolved.pop(record.checksum)
                write_error_report(catalog, unresolved)
            completed += 1
            if on_transcribed is not None:
                on_transcribed(completed, total, records[index])
        write_error_report(catalog, unresolved)
        if not unresolved and all(record.transcript is not None for record in records):
            write_catalog(catalog, records)
            remove_durable_file(journal_path(catalog))
        return TranscriptionSummary(completed, skipped, total, len(unresolved))
