"""Private, read-only importer for the two approved SVL audio trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.adapters.r2 import Boto3R2AudioUploader, S3LikeClient
from english_vocab_trainer.domain.models import Word
from english_vocab_trainer.validation import validate_english_transcript
from english_vocab_trainer.web.container import ConfigurationError, r2_client_from_env

APPROVED_SOURCES = ("上級SVL", "超上級SVL")
_TERM = re.compile(r"(?:^|[ \u3000])\d{4}[ \u3000]+(.+?)\.mp3$", re.IGNORECASE)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class AudioCandidate:
    path: Path
    audio_key: str
    source: str
    level: int | None
    term: str
    checksum: str = ""


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    id: str
    audio_key: str
    source: str
    level: int | None
    term: str
    checksum: str
    transcript: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionSummary:
    completed: int
    skipped: int
    total: int


@dataclass(frozen=True, slots=True)
class UploadSummary:
    uploaded: int
    skipped: int
    total: int


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> str: ...


class MlxWhisperTranscriber:
    """Lazy MLX wrapper so scan/publish do not require a downloaded model."""

    model = "mlx-community/whisper-large-v3-turbo"

    def transcribe(self, path: Path) -> str:
        try:
            import mlx_whisper
        except ImportError as exc:  # pragma: no cover - depends on optional group
            raise RuntimeError("install the ingest dependency group to transcribe audio") from exc
        result: Any = mlx_whisper.transcribe(str(path), path_or_hf_repo=self.model, language="en")
        return str(result["text"]).strip()


def parse_audio_path(root: Path, path: Path) -> tuple[int | None, str]:
    """Parse actual SVL names without consulting duplicate folders or CSV files."""
    relative = path.resolve().relative_to(root.resolve())
    if len(relative.parts) < 3 or relative.parts[0] not in APPROVED_SOURCES:
        raise ValueError(f"audio is outside approved source trees: {path}")
    source, level_part = relative.parts[:2]
    level = int(level_part) if level_part.isdecimal() else None
    match = _TERM.search(path.name)
    if match is None:
        raise ValueError(f"unrecognised audio filename: {path.name}")
    return level, match.group(1).strip()


def checksum(path: Path) -> str:
    with path.open("rb") as audio:
        return hashlib.file_digest(audio, "sha256").hexdigest()


def r2_audio_key(checksum_value: str) -> str:
    """Return the only allowed private R2 object key for a catalog checksum."""
    if _SHA256.fullmatch(checksum_value) is None:
        raise ValueError("checksum must be a lowercase SHA-256 hex digest")
    return f"audio/{checksum_value}.mp3"


def select_audio(root: Path, limit_per_source: int = 10) -> list[AudioCandidate]:
    """Choose the lowest levels, deterministically, from only approved trees."""
    if limit_per_source < 1:
        raise ValueError("limit_per_source must be positive")
    selected: list[AudioCandidate] = []
    for source in APPROVED_SOURCES:
        candidates: list[AudioCandidate] = []
        for path in (root / source).rglob("*.mp3"):
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
        selected.extend(
            replace(candidate, checksum=checksum(candidate.path))
            for candidate in candidates[:limit_per_source]
        )
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


def read_catalog(path: Path) -> list[CatalogRecord]:
    if not path.exists():
        return []
    return [
        CatalogRecord(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def write_catalog(path: Path, records: Iterable[CatalogRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(asdict(record), sort_keys=True) + "\n" for record in records)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def validate_transcript(transcript: str) -> str:
    return validate_english_transcript(transcript)


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


def transcribe_records(
    root: Path,
    records: Iterable[CatalogRecord],
    transcriber: Transcriber,
    *,
    force: bool = False,
) -> list[CatalogRecord]:
    completed: list[CatalogRecord] = []
    for record in records:
        transcript = record.transcript
        if force or transcript is None:
            transcript = validate_transcript(transcriber.transcribe(root / record.audio_key))
        completed.append(
            CatalogRecord(
                record.id,
                record.audio_key,
                record.source,
                record.level,
                record.term,
                record.checksum,
                transcript,
            )
        )
    return completed


def transcribe_catalog(
    root: Path,
    catalog: Path,
    transcriber: Transcriber,
    *,
    force: bool,
    on_transcribed: Callable[[int, int, CatalogRecord], None] | None = None,
) -> TranscriptionSummary:
    with catalog_lock(catalog):
        records = read_catalog(catalog)
        total = len(records)
        completed = 0 if force else sum(record.transcript is not None for record in records)
        skipped = completed
        for index, record in enumerate(records):
            if record.transcript is not None and not force:
                continue
            transcript = validate_transcript(transcriber.transcribe(root / record.audio_key))
            records[index] = replace(record, transcript=transcript)
            write_catalog(catalog, records)
            completed += 1
            if on_transcribed is not None:
                on_transcribed(completed, total, records[index])
        return TranscriptionSummary(completed, skipped, total)


def _relative_audio_path(audio_key: str) -> PurePosixPath:
    path = PurePosixPath(audio_key)
    if (
        path.is_absolute()
        or len(path.parts) < 3
        or ".." in path.parts
        or path.parts[0] not in APPROVED_SOURCES
        or path.suffix.lower() != ".mp3"
    ):
        raise ValueError("audio key is outside approved source trees")
    return path


def _validate_record(record: CatalogRecord, root: Path | None) -> None:
    if record.source not in APPROVED_SOURCES:
        raise ValueError("catalog source is not approved")
    if _SHA256.fullmatch(record.checksum) is None:
        raise ValueError("catalog checksum must be a lowercase SHA-256 hex digest")
    if record.id != f"audio-{record.checksum}":
        raise ValueError("catalog id must match checksum")
    relative = _relative_audio_path(record.audio_key)
    if relative.parts[0] != record.source:
        raise ValueError("catalog source does not match audio key")
    synthetic_root = root if root is not None else Path("/")
    try:
        level, term = parse_audio_path(synthetic_root, synthetic_root / Path(*relative.parts))
    except ValueError as exc:
        raise ValueError("catalog audio key cannot be parsed") from exc
    if (level, term) != (record.level, record.term):
        raise ValueError("catalog term or level does not match audio key")
    if root is not None:
        resolved_root = root.resolve()
        source_path = (root / Path(*relative.parts)).resolve()
        try:
            source_path.relative_to(resolved_root / record.source)
        except ValueError as exc:
            raise ValueError("audio key is outside approved source trees") from exc
        if not source_path.is_file():
            raise ValueError("catalog source audio is missing")
        if checksum(source_path) != record.checksum:
            raise ValueError("local audio does not match catalog checksum")
    if record.transcript is None:
        raise ValueError(f"missing transcript for {record.term}")
    validate_transcript(record.transcript)


def _validate_catalog_records(
    records: Iterable[CatalogRecord], expected_count: int = 20, *, root: Path | None = None
) -> list[CatalogRecord]:
    """Internal structural validator; root=None is only for catalog construction/tests."""
    result = list(records)
    if len(result) != expected_count:
        raise ValueError(f"expected {expected_count} records, found {len(result)}")
    if len({record.id for record in result}) != len(result):
        raise ValueError("duplicate catalog id")
    if len({record.checksum for record in result}) != len(result):
        raise ValueError("duplicate catalog checksum")
    if len({record.audio_key for record in result}) != len(result):
        raise ValueError("duplicate catalog audio key")
    for record in result:
        _validate_record(record, root)
    return result


def validate_records(
    records: Iterable[CatalogRecord], expected_count: int = 20, *, root: Path
) -> list[CatalogRecord]:
    """Validate a publishable catalog against its approved, immutable local source files."""
    return _validate_catalog_records(records, expected_count, root=root)


def publish_records(
    database: Path,
    records: Iterable[CatalogRecord],
    *,
    audio_backend: str = "filesystem",
    root: Path | None = None,
) -> None:
    if audio_backend not in {"filesystem", "r2"}:
        raise ValueError("audio backend must be filesystem or r2")
    if root is None:
        raise ValueError("publish requires a source root for file checksum validation")
    validated = validate_records(records, root=root)
    words = [
        Word(
            record.id,
            record.term,
            record.level,
            record.transcript,
            record.audio_key if audio_backend == "filesystem" else r2_audio_key(record.checksum),
        )
        for record in validated
    ]
    repository = SQLiteVocabularyRepository(database, "local-user")
    try:
        repository.bulk_upsert_words(words)
    finally:
        repository.close()


def upload_audio(
    root: Path,
    records: Iterable[CatalogRecord],
    client: S3LikeClient,
    bucket: str,
    *,
    force: bool = False,
    on_progress: Callable[[int, int, CatalogRecord, str], None] | None = None,
) -> UploadSummary:
    """Validate all catalog entries then idempotently upload private R2 objects."""
    validated = validate_records(records, root=root)
    uploader = Boto3R2AudioUploader(client, bucket)
    uploaded = 0
    for index, record in enumerate(validated, start=1):
        path = root / Path(*_relative_audio_path(record.audio_key).parts)
        changed = uploader.upload(r2_audio_key(record.checksum), path, record.checksum, force=force)
        uploaded += int(changed)
        if on_progress is not None:
            on_progress(index, len(validated), record, "uploaded" if changed else "skipped")
    return UploadSummary(uploaded, len(validated) - uploaded, len(validated))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a private 20-audio SVL MVP catalog")
    parser.add_argument(
        "command", choices=["scan", "transcribe", "validate", "publish", "upload-audio"]
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
    parser.add_argument(
        "--resume", action="store_true", help="preserve matching catalog transcripts"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "scan":
        records = records_from_audio(select_audio(args.source, args.limit_per_source))
        if args.resume and not args.force:
            prior = {record.id: record for record in read_catalog(args.catalog)}
            records = [prior.get(record.id, record) for record in records]
        if args.dry_run:
            print(f"would write {len(records)} private catalog records")
        else:
            write_catalog(args.catalog, records)
            print(f"wrote {len(records)} private catalog records")
    elif args.command == "transcribe":
        records = read_catalog(args.catalog)
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
                on_transcribed=lambda completed, total, record: print(
                    f"transcribed {completed}/{total}: {record.term}", flush=True
                ),
            )
            print(
                f"completed {summary.completed}/{summary.total}; skipped {summary.skipped}",
                flush=True,
            )
    elif args.command == "validate":
        valid = validate_records(read_catalog(args.catalog), root=args.source)
        print(f"valid: {len(valid)} English transcript records")
    elif args.command == "upload-audio":
        records = read_catalog(args.catalog)
        validated = validate_records(records, root=args.source)
        if args.dry_run:
            print(f"would upload {len(validated)} private R2 audio objects")
        else:
            try:
                client, bucket = r2_client_from_env(os.environ)
            except ConfigurationError as exc:
                raise SystemExit(str(exc)) from exc
            upload_summary = upload_audio(
                args.source,
                validated,
                client,
                bucket,
                force=args.force,
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
            print(
                f"would publish {len(validate_records(records, root=args.source))} records "
                f"to SQLite using {args.audio_backend} audio keys"
            )
        else:
            publish_records(
                args.database, records, audio_backend=args.audio_backend, root=args.source
            )
            print("published private catalog to SQLite")
