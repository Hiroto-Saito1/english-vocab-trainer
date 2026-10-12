"""Validate source paths, catalog records, and English transcripts."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from .models import APPROVED_SOURCES, TRANSCRIPTION_ERROR_REASONS, CatalogRecord

_TERM = re.compile(r"(?:^|[ \u3000])\d{4}[ \u3000]+(.+?)\.mp3$", re.IGNORECASE)
_TERM_VALUE = re.compile(r"[A-Za-z][A-Za-z '\-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_COMMON_ENGLISH_PUNCTUATION = str.maketrans(
    {
        "\u00a0": " ",  # non-breaking space
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark / apostrophe
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2013": " - ",  # en dash
        "\u2014": " - ",  # em dash
        "\u2026": "...",  # ellipsis
    }
)
_VALIDATION_FAILURE_REASONS = {
    "transcript must use Latin alphabetic characters only": "transcript-script-invalid",
    "transcript contains unsafe Unicode": "transcript-unicode-invalid",
    "transcript must contain ASCII punctuation only": "transcript-punctuation-invalid",
    "transcript contains an unbound combining mark": "transcript-unbound-combining-mark",
}


def validate_term(term: str) -> str:
    value = term.strip()
    if _TERM_VALUE.fullmatch(value) is None:
        raise ValueError(
            "audio term must contain only ASCII letters, spaces, hyphens, or apostrophes"
        )
    return value


def parse_audio_path(root: Path, path: Path) -> tuple[int | None, str]:
    """Parse actual SVL names without consulting duplicate folders or CSV files."""
    relative = path.resolve().relative_to(root.resolve())
    if len(relative.parts) < 3 or relative.parts[0] not in APPROVED_SOURCES:
        raise ValueError(f"audio is outside approved source trees: {path}")
    _, level_part = relative.parts[:2]
    level = int(level_part) if level_part.isdecimal() else None
    match = _TERM.search(path.name)
    if match is None:
        raise ValueError(f"unrecognised audio filename: {path.name}")
    return level, validate_term(match.group(1).strip())


def checksum(path: Path) -> str:
    with path.open("rb") as audio:
        return hashlib.file_digest(audio, "sha256").hexdigest()


def r2_audio_key(checksum_value: str) -> str:
    """Return the only allowed private R2 object key for a catalog checksum."""
    if _SHA256.fullmatch(checksum_value) is None:
        raise ValueError("checksum must be a lowercase SHA-256 hex digest")
    return f"audio/{checksum_value}.mp3"


def relative_audio_path(audio_key: str) -> PurePosixPath:
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


def validate_transcript(transcript: str) -> str:
    value = unicodedata.normalize("NFKC", transcript).translate(_COMMON_ENGLISH_PUNCTUATION)
    value = re.sub(r"[ \t]+", " ", value).strip()
    if _JAPANESE.search(value):
        raise ValueError("transcript must be English only")
    if len(value) < 12 or len(value.split()) < 3:
        raise ValueError("transcript is too short to contain a definition or example")
    preceding_latin = False
    for character in value:
        category = unicodedata.category(character)
        if category.startswith("C") or character == "\ufffd":
            raise ValueError("transcript contains unsafe Unicode")
        if character.isalpha():
            if "LATIN" not in unicodedata.name(character, ""):
                raise ValueError("transcript must use Latin alphabetic characters only")
            preceding_latin = True
        elif category.startswith("M"):
            if not preceding_latin:
                raise ValueError("transcript contains an unbound combining mark")
        else:
            if ord(character) > 127:
                raise ValueError("transcript must contain ASCII punctuation only")
            preceding_latin = False
    words = _WORDS.findall(value.casefold())
    if len(value) > 2_000 or len(words) > 350:
        raise ValueError("transcript-quality-excessive-length")
    consecutive = 1
    for previous, current in zip(words, words[1:], strict=False):
        consecutive = consecutive + 1 if current == previous else 1
        if consecutive >= 8:
            raise ValueError("transcript-quality-repeated-word")
    for size in range(2, min(9, len(words) // 3 + 1)):
        for start in range(len(words) - size * 3 + 1):
            phrase = words[start : start + size]
            if (
                phrase
                == words[start + size : start + size * 2]
                == words[start + size * 2 : start + size * 3]
            ):
                raise ValueError("transcript-quality-repeated-phrase")
    if len(words) >= 30 and len(set(words)) / len(words) < 0.2:
        raise ValueError("transcript-quality-low-lexical-diversity")
    return value


def transcription_failure_reason(error: Exception) -> str:
    if not isinstance(error, ValueError):
        return "runtime-failure"
    message = str(error)
    if message in TRANSCRIPTION_ERROR_REASONS:
        return message
    return _VALIDATION_FAILURE_REASONS.get(message, "transcript-validation-failed")


def _validate_record(
    record: CatalogRecord, root: Path | None, *, require_transcript: bool = True
) -> None:
    if record.source not in APPROVED_SOURCES:
        raise ValueError("catalog source is not approved")
    validate_term(record.term)
    if _SHA256.fullmatch(record.checksum) is None:
        raise ValueError("catalog checksum must be a lowercase SHA-256 hex digest")
    if record.id != f"audio-{record.checksum}":
        raise ValueError("catalog id must match checksum")
    relative = relative_audio_path(record.audio_key)
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
        if not source_path.is_relative_to(resolved_root / record.source):
            raise ValueError("audio key is outside approved source trees")
        if not source_path.is_file():
            raise ValueError("catalog source audio is missing")
        if checksum(source_path) != record.checksum:
            raise ValueError("local audio does not match catalog checksum")
    if require_transcript:
        if record.transcript is None:
            raise ValueError(f"missing transcript for {record.term}")
        validate_transcript(record.transcript)


def validate_records(
    records: Iterable[CatalogRecord],
    expected_count: int = 20,
    *,
    root: Path | None = None,
    require_transcript: bool = True,
) -> list[CatalogRecord]:
    """Validate catalog structure and, when root is provided, its local source files."""
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
        _validate_record(record, root, require_transcript=require_transcript)
    return result
