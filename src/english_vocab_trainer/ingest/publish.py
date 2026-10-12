from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.adapters.r2 import Boto3R2AudioUploader, S3LikeClient
from english_vocab_trainer.domain.models import Word

from .models import CatalogRecord, UploadSummary
from .validation import r2_audio_key, relative_audio_path, validate_records


def publish_records(
    database: Path,
    records: Iterable[CatalogRecord],
    *,
    audio_backend: str = "filesystem",
    root: Path | None = None,
    expected_count: int = 20,
) -> None:
    if audio_backend not in {"filesystem", "r2"}:
        raise ValueError("audio backend must be filesystem or r2")
    if root is None:
        raise ValueError("publish requires a source root for file checksum validation")
    validated = validate_records(records, expected_count, root=root)
    words = [
        Word(
            record.id,
            record.term,
            record.level,
            cast(str, record.transcript),
            record.audio_key if audio_backend == "filesystem" else r2_audio_key(record.checksum),
        )
        for record in validated
    ]
    with SQLiteVocabularyRepository(database) as repository:
        repository.bulk_upsert_words(words)


def upload_audio(
    root: Path,
    records: Iterable[CatalogRecord],
    client: S3LikeClient,
    bucket: str,
    *,
    force: bool = False,
    expected_count: int = 20,
    on_progress: Callable[[int, int, CatalogRecord, str], None] | None = None,
) -> UploadSummary:
    """Validate all catalog entries then idempotently upload private R2 objects."""
    validated = validate_records(records, expected_count, root=root)
    uploader = Boto3R2AudioUploader(client, bucket)
    uploaded = 0
    for index, record in enumerate(validated, start=1):
        path = root / Path(*relative_audio_path(record.audio_key).parts)
        changed = uploader.upload(r2_audio_key(record.checksum), path, record.checksum, force=force)
        uploaded += int(changed)
        if on_progress is not None:
            on_progress(index, len(validated), record, "uploaded" if changed else "skipped")
    return UploadSummary(uploaded, len(validated) - uploaded, len(validated))
