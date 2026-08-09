"""Private, read-only importer for the two approved SVL audio trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Word

APPROVED_SOURCES = ("上級SVL", "超上級SVL")
_TERM = re.compile(r"(?:^|[ \u3000])\d{4}[ \u3000]+(.+?)\.mp3$", re.IGNORECASE)
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


@dataclass(frozen=True, slots=True)
class AudioCandidate:
    path: Path
    audio_key: str
    source: str
    level: int | None
    term: str
    checksum: str


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    id: str
    audio_key: str
    source: str
    level: int | None
    term: str
    checksum: str
    transcript: str | None = None


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> str: ...


class MlxWhisperTranscriber:
    """Lazy MLX wrapper so scan/publish do not require a downloaded model."""

    model = "mlx-community/whisper-large-v3-turbo"

    def transcribe(self, path: Path) -> str:
        try:
            import mlx_whisper  # type: ignore[import-untyped]
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
                AudioCandidate(
                    path, path.relative_to(root).as_posix(), source, level, term, checksum(path)
                )
            )
        candidates.sort(
            key=lambda item: (
                item.level is None,
                item.level if item.level is not None else 999,
                item.audio_key,
            )
        )
        selected.extend(candidates[:limit_per_source])
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
    path.write_text(text, encoding="utf-8")


def validate_transcript(transcript: str) -> str:
    value = transcript.strip()
    if _JAPANESE.search(value):
        raise ValueError("transcript must be English only")
    if len(value) < 12 or len(value.split()) < 3:
        raise ValueError("transcript is too short to contain a definition or example")
    return value


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


def validate_records(
    records: Iterable[CatalogRecord], expected_count: int = 20
) -> list[CatalogRecord]:
    result = list(records)
    if len(result) != expected_count:
        raise ValueError(f"expected {expected_count} records, found {len(result)}")
    if len({record.id for record in result}) != len(result):
        raise ValueError("audio checksum collision or duplicate record")
    for record in result:
        if record.transcript is None:
            raise ValueError(f"missing transcript for {record.term}")
        validate_transcript(record.transcript)
    return result


def publish_records(database: Path, records: Iterable[CatalogRecord]) -> None:
    repository = SQLiteVocabularyRepository(database, "local-user")
    try:
        for record in validate_records(records):
            repository.add_word(
                Word(record.id, record.term, record.level, record.transcript, record.audio_key)
            )
    finally:
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a private 20-audio SVL MVP catalog")
    parser.add_argument("command", choices=["scan", "transcribe", "validate", "publish"])
    parser.add_argument(
        "--source", type=Path, required=True, help="parent containing approved SVL trees"
    )
    parser.add_argument("--catalog", type=Path, default=Path(".private/catalog.jsonl"))
    parser.add_argument("--database", type=Path)
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
            write_catalog(
                args.catalog,
                transcribe_records(args.source, records, MlxWhisperTranscriber(), force=args.force),
            )
            print("updated private transcripts")
    elif args.command == "validate":
        print(
            f"valid: {len(validate_records(read_catalog(args.catalog)))} English transcript records"
        )
    else:
        if args.database is None:
            raise SystemExit("publish requires --database")
        records = read_catalog(args.catalog)
        if args.dry_run:
            print(f"would publish {len(validate_records(records))} records to SQLite")
        else:
            publish_records(args.database, records)
            print("published private catalog to SQLite")
