"""Command-line wiring for private catalog ingestion."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from english_vocab_trainer.adapters.r2 import r2_client_from_env
from english_vocab_trainer.configuration import ConfigurationError

from .catalog import (
    audit_catalog,
    catalog_status,
    records_from_audio,
    scan_catalog,
    select_audio,
)
from .models import PROFILES
from .publish import publish_records, upload_audio
from .storage import read_catalog, read_manifest, write_audit_report
from .transcription import MlxWhisperTranscriber, transcribe_catalog
from .validation import validate_records


def main() -> None:  # pragma: no cover - thin argparse wiring covered by CLI integration tests
    parser = argparse.ArgumentParser(description="Import a private English-only SVL catalog")
    parser.add_argument(
        "command",
        choices=["scan", "transcribe", "status", "validate", "audit", "publish", "upload-audio"],
    )
    parser.add_argument(
        "--source", type=Path, required=True, help="parent containing approved SVL trees"
    )
    parser.add_argument("--catalog", type=Path, default=Path(".private/catalog.jsonl"))
    parser.add_argument("--database", type=Path)
    parser.add_argument(
        "--audio-backend",
        choices=["filesystem", "r2"],
        default="filesystem",
        help="audio keys to publish (default: filesystem)",
    )
    parser.add_argument("--limit-per-source", type=int, default=10)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="mvp")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    expected_count = profile.expected_count(args.limit_per_source)
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records must be positive")
    if args.command == "scan":
        if args.dry_run:
            candidates = select_audio(args.source, args.limit_per_source, profile=profile)
            records = records_from_audio(candidates)
            if len(records) != expected_count:
                raise SystemExit(
                    f"profile {profile.name} expected {expected_count} records, "
                    f"found {len(records)}"
                )
            print(f"would write {len(records)} private catalog records")
        else:
            records, _ = scan_catalog(
                args.source,
                args.catalog,
                profile=profile,
                limit_per_source=args.limit_per_source,
                force=args.force,
            )
            print(f"wrote {len(records)} private catalog records")
    elif args.command == "transcribe":
        records = read_catalog(args.catalog)
        validate_records(records, expected_count, root=args.source, require_transcript=False)
        if args.dry_run:
            print(
                f"would transcribe {sum(record.transcript is None for record in records)} records"
            )
        else:
            summary = transcribe_catalog(
                args.source,
                args.catalog,
                MlxWhisperTranscriber(),
                force=args.force,
                continue_on_error=args.continue_on_error,
                max_records=args.max_records,
                on_transcribed=lambda completed, total, record: print(
                    f"transcribed {completed}/{total}: {record.term}", flush=True
                ),
            )
            print(
                f"completed {summary.completed}/{summary.total}; skipped {summary.skipped}; "
                f"errors {summary.errors}",
                flush=True,
            )
            if summary.errors:
                raise SystemExit("transcription completed with errors; see private error report")
    elif args.command == "validate":
        valid = validate_records(read_catalog(args.catalog), expected_count, root=args.source)
        print(f"valid: {len(valid)} English transcript records")
    elif args.command == "status":
        total, transcribed = catalog_status(
            args.source,
            args.catalog,
            profile=profile,
            limit_per_source=args.limit_per_source,
        )
        print(f"status valid: {total} inventory records; transcribed {transcribed}")
    elif args.command == "audit":
        valid, warnings = audit_catalog(
            args.source,
            args.catalog,
            profile=profile,
            limit_per_source=args.limit_per_source,
        )
        write_audit_report(args.catalog, read_manifest(args.catalog), warnings)
        print(f"audit valid: {len(valid)} records; warnings {len(warnings)}")
    elif args.command == "upload-audio":
        records = read_catalog(args.catalog)
        if args.dry_run:
            validated = validate_records(records, expected_count, root=args.source)
            print(f"would upload {len(validated)} private R2 audio objects")
        else:
            try:
                client, bucket = r2_client_from_env(os.environ)
            except ConfigurationError as exc:
                raise SystemExit(str(exc)) from exc
            upload_summary = upload_audio(
                args.source,
                records,
                client,
                bucket,
                force=args.force,
                expected_count=expected_count,
                on_progress=lambda completed, total, record, status: print(
                    f"{status} {completed}/{total}: {record.term}", flush=True
                ),
            )
            print(
                f"uploaded {upload_summary.uploaded}; skipped {upload_summary.skipped}; "
                f"total {upload_summary.total}",
                flush=True,
            )
    else:
        if args.database is None:
            raise SystemExit("publish requires --database")
        records = read_catalog(args.catalog)
        if args.dry_run:
            validated_count = len(validate_records(records, expected_count, root=args.source))
            print(
                f"would publish {validated_count} records "
                f"to SQLite using {args.audio_backend} audio keys"
            )
        else:
            publish_records(
                args.database,
                records,
                audio_backend=args.audio_backend,
                root=args.source,
                expected_count=expected_count,
            )
            print("published private catalog to SQLite")
