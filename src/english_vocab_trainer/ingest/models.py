from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

APPROVED_SOURCES = ("上級SVL", "超上級SVL")
CATALOG_SCHEMA_VERSION = 1
TRANSCRIPTION_MODEL = "mlx-community/whisper-large-v3-turbo"
TRANSCRIPTION_ERROR_REASONS = frozenset(
    {
        "runtime-failure",
        "transcript-validation-failed",
        "transcript-script-invalid",
        "transcript-unicode-invalid",
        "transcript-punctuation-invalid",
        "transcript-unbound-combining-mark",
        "transcript-quality-excessive-length",
        "transcript-quality-repeated-word",
        "transcript-quality-repeated-phrase",
        "transcript-quality-low-lexical-diversity",
    }
)


@dataclass(frozen=True, slots=True)
class CatalogProfile:
    name: str
    per_source: int | None

    def expected_count(self, mvp_limit: int = 10) -> int:
        if mvp_limit < 1:
            raise ValueError("limit_per_source must be positive")
        return len(APPROVED_SOURCES) * (
            self.per_source if self.per_source is not None else mvp_limit
        )


MVP_PROFILE = CatalogProfile("mvp", None)
FULL_PROFILE = CatalogProfile("full", 1000)
PROFILES = {profile.name: profile for profile in (MVP_PROFILE, FULL_PROFILE)}


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
    errors: int = 0


@dataclass(frozen=True, slots=True)
class UploadSummary:
    uploaded: int
    skipped: int
    total: int


@dataclass(frozen=True, slots=True)
class CatalogManifest:
    schema_version: int
    profile: str
    expected_count: int
    inventory_digest: str
    model: str
    language: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()
