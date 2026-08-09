"""Private, read-only importer for the two approved SVL audio trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from english_vocab_trainer.adapters.local.sqlite import SQLiteVocabularyRepository
from english_vocab_trainer.adapters.r2 import Boto3R2AudioUploader, S3LikeClient
from english_vocab_trainer.domain.models import Tier, Word
from english_vocab_trainer.validation import validate_english_transcript
from english_vocab_trainer.web.container import ConfigurationError, r2_client_from_env

APPROVED_SOURCES = ("上級SVL", "超上級SVL")
_PUBLISHED_TIERS = {"上級SVL": Tier.UPPER, "超上級SVL": Tier.ULTRA}
_TERM = re.compile(r"(?:^|[ \u3000])\d{4}[ \u3000]+(.+?)\.mp3$", re.IGNORECASE)
_TERM_VALUE = re.compile(r"[A-Za-z][A-Za-z '\-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)
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
CATALOG_SCHEMA_VERSION = 1
_ERROR_REASONS = frozenset(
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


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> str: ...


class MlxWhisperTranscriber:
    """Lazy MLX wrapper so scan/publish do not require a downloaded model."""

    model = "mlx-community/whisper-large-v3-turbo"
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
    return level, validate_term(match.group(1).strip())


def validate_term(term: str) -> str:
    value = term.strip()
    if _TERM_VALUE.fullmatch(value) is None:
        raise ValueError(
            "audio term must contain only ASCII letters, spaces, hyphens, or apostrophes"
        )
    return value


def checksum(path: Path) -> str:
    with path.open("rb") as audio:
        return hashlib.file_digest(audio, "sha256").hexdigest()


def r2_audio_key(checksum_value: str) -> str:
    """Return the only allowed private R2 object key for a catalog checksum."""
    if _SHA256.fullmatch(checksum_value) is None:
        raise ValueError("checksum must be a lowercase SHA-256 hex digest")
    return f"audio/{checksum_value}.mp3"


def profile_for(name: str) -> CatalogProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError("profile must be mvp or full") from exc


def _source_candidates(root: Path, source: str) -> list[AudioCandidate]:
    candidates: list[AudioCandidate] = []
    source_root = root / source
    if not source_root.is_dir():
        raise ValueError(f"approved source is missing: {source}")
    expected_root = root.resolve() / source
    try:
        source_root.resolve().relative_to(expected_root)
    except ValueError as exc:
        raise ValueError("approved source symlink is outside its source tree") from exc
    for path in source_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".mp3":
            continue
        try:
            path.resolve().relative_to(expected_root)
        except ValueError as exc:
            raise ValueError("audio symlink is outside its approved source tree") from exc
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


def inventory_digest(candidates: Iterable[AudioCandidate]) -> str:
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
    all_candidates: list[AudioCandidate] = []
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
        all_candidates.extend(completed)
    if full_profile:
        if len({item.audio_key for item in all_candidates}) != len(all_candidates):
            raise ValueError("full profile contains duplicate audio paths")
        if len({item.checksum for item in all_candidates}) != len(all_candidates):
            raise ValueError("full profile contains duplicate audio checksums")
        if len({item.term.casefold() for item in all_candidates}) != len(all_candidates):
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


def manifest_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".manifest.json")


def journal_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".journal.jsonl")


def report_path(catalog: Path, name: str) -> Path:
    return catalog.with_suffix(catalog.suffix + f".{name}.json")


def error_report_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".transcription-errors.jsonl")


def error_journal_path(catalog: Path) -> Path:
    return catalog.with_suffix(catalog.suffix + ".transcription-errors.journal.jsonl")


def _atomic_write(path: Path, text: str) -> None:
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
    _atomic_write(manifest_path(catalog), json.dumps(asdict(manifest), sort_keys=True) + "\n")


def _journal_updates(catalog: Path, fingerprint: str | None = None) -> dict[str, CatalogRecord]:
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


def _merge_journal(
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


def _append_journal(catalog: Path, fingerprint: str, record: CatalogRecord) -> None:
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


def _failure_reason(error: Exception) -> str:
    if not isinstance(error, ValueError):
        return "runtime-failure"
    message = str(error)
    if message in _ERROR_REASONS:
        return message
    if message == "transcript must use Latin alphabetic characters only":
        return "transcript-script-invalid"
    if message == "transcript contains unsafe Unicode":
        return "transcript-unicode-invalid"
    if message == "transcript must contain ASCII punctuation only":
        return "transcript-punctuation-invalid"
    if message == "transcript contains an unbound combining mark":
        return "transcript-unbound-combining-mark"
    return "transcript-validation-failed"


def _error_updates(catalog: Path, fingerprint: str | None = None) -> dict[str, dict[str, str]]:
    path = error_journal_path(catalog)
    if not path.exists():
        return {}
    unresolved: dict[str, dict[str, str]] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    header_seen = False
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise ValueError("transcription error journal is corrupt") from None
        if not isinstance(entry, dict):
            raise ValueError("transcription error journal is corrupt")
        if entry.get("kind") == "header":
            if header_seen or not isinstance(entry.get("fingerprint"), str):
                raise ValueError("transcription error journal is corrupt")
            if fingerprint is not None and entry["fingerprint"] != fingerprint:
                raise ValueError("catalog identity does not match transcription error journal")
            header_seen = True
            continue
        if (
            not header_seen
            or entry.get("kind") != "event"
            or not isinstance(entry.get("checksum"), str)
        ):
            raise ValueError("transcription error journal is corrupt")
        checksum_value = entry["checksum"]
        if _SHA256.fullmatch(checksum_value) is None:
            raise ValueError("transcription error journal is corrupt")
        status = entry.get("status")
        if (
            status == "failed"
            and isinstance(entry.get("term"), str)
            and entry.get("reason") in _ERROR_REASONS
        ):
            unresolved[checksum_value] = {
                "checksum": checksum_value,
                "term": entry["term"],
                "reason": entry["reason"],
            }
        elif status == "resolved":
            unresolved.pop(checksum_value, None)
        else:
            raise ValueError("transcription error journal is corrupt")
    return unresolved


def _append_error_event(
    catalog: Path, fingerprint: str, record: CatalogRecord, status: str, reason: str | None = None
) -> None:
    path = error_journal_path(catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_truncated_journal_tail(path)
    new = not path.exists() or path.stat().st_size == 0
    event: dict[str, str] = {"checksum": record.checksum, "kind": "event", "status": status}
    if status == "failed":
        event["term"] = record.term
        if reason not in _ERROR_REASONS:
            raise ValueError("transcription error reason is invalid")
        event["reason"] = reason
    with path.open("a", encoding="utf-8") as journal:
        if new:
            journal.write(
                json.dumps({"kind": "header", "fingerprint": fingerprint}, sort_keys=True) + "\n"
            )
        journal.write(json.dumps(event, sort_keys=True) + "\n")
        journal.flush()
        os.fsync(journal.fileno())


def _unlink_durable(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_current_error_report(catalog: Path, unresolved: dict[str, dict[str, str]]) -> None:
    if not unresolved:
        _unlink_durable(error_report_path(catalog))
        _unlink_durable(error_journal_path(catalog))
        return
    text = "".join(
        json.dumps(unresolved[checksum_value], sort_keys=True) + "\n"
        for checksum_value in sorted(unresolved)
    )
    _atomic_write(error_report_path(catalog), text)


def _read_current_error_report(catalog: Path) -> dict[str, dict[str, str]]:
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
            or entry.get("reason") not in _ERROR_REASONS
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


def _replace_error_journal(
    catalog: Path, fingerprint: str, unresolved: dict[str, dict[str, str]]
) -> None:
    if not unresolved:
        _unlink_durable(error_journal_path(catalog))
        return
    lines = [json.dumps({"kind": "header", "fingerprint": fingerprint}, sort_keys=True)]
    lines.extend(
        json.dumps(
            {
                "checksum": unresolved[checksum_value]["checksum"],
                "kind": "event",
                "reason": unresolved[checksum_value]["reason"],
                "status": "failed",
                "term": unresolved[checksum_value]["term"],
            },
            sort_keys=True,
        )
        for checksum_value in sorted(unresolved)
    )
    _atomic_write(error_journal_path(catalog), "\n".join(lines) + "\n")


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
    updates = _journal_updates(path, manifest.fingerprint if manifest is not None else None)
    if not updates:
        return records
    return _merge_journal(records, updates)


def write_catalog(path: Path, records: Iterable[CatalogRecord]) -> None:
    text = "".join(json.dumps(asdict(record), sort_keys=True) + "\n" for record in records)
    _atomic_write(path, text)


def catalog_fingerprint(records: Iterable[CatalogRecord]) -> str:
    identity = [
        {"audio_key": record.audio_key, "checksum": record.checksum, "term": record.term}
        for record in sorted(records, key=lambda item: item.audio_key)
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def validate_transcript(transcript: str) -> str:
    value = unicodedata.normalize("NFKC", transcript).translate(_COMMON_ENGLISH_PUNCTUATION)
    value = re.sub(r"[ \t]+", " ", value)
    value = validate_english_transcript(value)
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
            _unlink_durable(journal_path(catalog))
            updates: dict[str, CatalogRecord] = {}
        else:
            updates = _journal_updates(catalog, fingerprint)
        records = _merge_journal(base_records, updates)
        unresolved = _error_updates(catalog, fingerprint)
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
                reason = _failure_reason(exc)
                unresolved[record.checksum] = {
                    "checksum": record.checksum,
                    "term": record.term,
                    "reason": reason,
                }
                _append_error_event(catalog, fingerprint, record, "failed", reason)
                _write_current_error_report(catalog, unresolved)
                if continue_on_error:
                    continue
                raise
            records[index] = replace(record, transcript=transcript)
            _append_journal(catalog, fingerprint, records[index])
            if record.checksum in unresolved:
                unresolved.pop(record.checksum)
                _append_error_event(catalog, fingerprint, record, "resolved")
            completed += 1
            if on_transcribed is not None:
                on_transcribed(completed, total, records[index])
        _write_current_error_report(catalog, unresolved)
        if not unresolved and all(record.transcript is not None for record in records):
            write_catalog(catalog, records)
            _unlink_durable(journal_path(catalog))
        return TranscriptionSummary(completed, skipped, total, len(unresolved))


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
    if require_transcript:
        if record.transcript is None:
            raise ValueError(f"missing transcript for {record.term}")
        validate_transcript(record.transcript)


def _validate_catalog_records(
    records: Iterable[CatalogRecord],
    expected_count: int = 20,
    *,
    root: Path | None = None,
    require_transcript: bool = True,
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
        _validate_record(record, root, require_transcript=require_transcript)
    return result


def validate_records(
    records: Iterable[CatalogRecord],
    expected_count: int = 20,
    *,
    root: Path,
    require_transcript: bool = True,
) -> list[CatalogRecord]:
    """Validate a publishable catalog against its approved, immutable local source files."""
    return _validate_catalog_records(
        records, expected_count, root=root, require_transcript=require_transcript
    )


def expected_count_for(profile: CatalogProfile, limit_per_source: int) -> int:
    if limit_per_source < 1:
        raise ValueError("limit_per_source must be positive")
    return profile.expected_count(limit_per_source)


def scan_catalog(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile = MVP_PROFILE,
    limit_per_source: int = 10,
    resume: bool = False,
    force: bool = False,
) -> tuple[list[CatalogRecord], CatalogManifest]:
    """Scan immutable audio, selectively reuse verified text, and atomically replace a catalog."""
    with catalog_lock(catalog):
        base_records = read_catalog(catalog, include_journal=False)
        pending_journal = journal_path(catalog).exists()
        pending_error_journal = error_journal_path(catalog).exists()
        pending_error_report = error_report_path(catalog).exists()
        if (
            (
                pending_journal
                or pending_error_journal
                or pending_error_report
                or any(record.transcript is not None for record in base_records)
            )
            and not resume
            and not force
        ):
            raise ValueError("existing transcript state requires --resume or --force")
        candidates = select_audio(root, limit_per_source, profile=profile)
        records = records_from_audio(candidates)
        expected = expected_count_for(profile, limit_per_source)
        if len(records) != expected:
            raise ValueError(
                f"profile {profile.name} expected {expected} records, found {len(records)}"
            )
        manifest = CatalogManifest(
            CATALOG_SCHEMA_VERSION,
            profile.name,
            expected,
            inventory_digest(candidates),
            MlxWhisperTranscriber.model,
            "en",
        )
        updates: dict[str, CatalogRecord] = {}
        unresolved: dict[str, dict[str, str]] = {}
        prior_manifest: CatalogManifest | None = None
        if pending_journal or pending_error_journal:
            prior_manifest = read_manifest(catalog)
            if prior_manifest is None:
                raise ValueError("checkpoint state exists without a provenance manifest")
        if pending_journal and not force:
            assert prior_manifest is not None
            updates = _journal_updates(catalog, prior_manifest.fingerprint)
            if prior_manifest.fingerprint != manifest.fingerprint:
                raise ValueError("catalog identity does not match transcription journal")
        if pending_error_journal and not force:
            assert prior_manifest is not None
            unresolved = _error_updates(catalog, prior_manifest.fingerprint)
        elif pending_error_report and not force:
            unresolved = _read_current_error_report(catalog)
        if resume and not force:
            prior = {record.checksum: record for record in _merge_journal(base_records, updates)}
            reused: list[CatalogRecord] = []
            for record in records:
                old = prior.get(record.checksum)
                if old is not None and old.transcript is not None:
                    transcript = validate_transcript(old.transcript)
                    reused.append(replace(record, transcript=transcript))
                else:
                    reused.append(record)
            records = reused
        write_catalog(catalog, records)
        write_manifest(catalog, manifest)
        if force:
            _unlink_durable(journal_path(catalog))
            _unlink_durable(error_journal_path(catalog))
            _unlink_durable(error_report_path(catalog))
        elif pending_journal:
            _unlink_durable(journal_path(catalog))
        if resume and not force:
            current_missing = {record.checksum for record in records if record.transcript is None}
            unresolved = {
                checksum_value: error
                for checksum_value, error in unresolved.items()
                if checksum_value in current_missing
            }
            _replace_error_journal(catalog, manifest.fingerprint, unresolved)
            _write_current_error_report(catalog, unresolved)
    return records, manifest


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


def audit_catalog(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile,
    limit_per_source: int,
) -> tuple[list[CatalogRecord], list[dict[str, str]]]:
    """Verify catalog provenance against the current immutable source inventory."""
    manifest = read_manifest(catalog)
    if manifest is None:
        raise ValueError("catalog provenance manifest is missing")
    expected = expected_count_for(profile, limit_per_source)
    if (
        manifest.schema_version != CATALOG_SCHEMA_VERSION
        or manifest.profile != profile.name
        or manifest.expected_count != expected
    ):
        raise ValueError("catalog profile does not match provenance manifest")
    candidates = select_audio(root, limit_per_source, profile=profile)
    if inventory_digest(candidates) != manifest.inventory_digest:
        raise ValueError("approved source inventory does not match catalog provenance")
    return audit_records(root, read_catalog(catalog), expected_count=expected)


def catalog_status(
    root: Path,
    catalog: Path,
    *,
    profile: CatalogProfile,
    limit_per_source: int,
) -> tuple[int, int]:
    """Check provenance and immutable inventory before any transcript exists."""
    manifest = read_manifest(catalog)
    if manifest is None:
        raise ValueError("catalog provenance manifest is missing")
    expected = expected_count_for(profile, limit_per_source)
    if (
        manifest.schema_version != CATALOG_SCHEMA_VERSION
        or manifest.profile != profile.name
        or manifest.expected_count != expected
    ):
        raise ValueError("catalog profile does not match provenance manifest")
    candidates = select_audio(root, limit_per_source, profile=profile)
    if inventory_digest(candidates) != manifest.inventory_digest:
        raise ValueError("approved source inventory does not match catalog provenance")
    records = validate_records(read_catalog(catalog), expected, root=root, require_transcript=False)
    return len(records), sum(record.transcript is not None for record in records)


def write_audit_report(
    catalog: Path, manifest: CatalogManifest | None, warnings: list[dict[str, str]]
) -> None:
    payload: dict[str, object] = {"warnings": warnings, "warning_count": len(warnings)}
    if manifest is not None:
        payload["manifest"] = asdict(manifest)
    _atomic_write(report_path(catalog, "audit"), json.dumps(payload, sort_keys=True) + "\n")


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
            record.transcript,
            record.audio_key if audio_backend == "filesystem" else r2_audio_key(record.checksum),
            _PUBLISHED_TIERS[record.source],
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
    expected_count: int = 20,
    on_progress: Callable[[int, int, CatalogRecord, str], None] | None = None,
) -> UploadSummary:
    """Validate all catalog entries then idempotently upload private R2 objects."""
    validated = validate_records(records, expected_count, root=root)
    uploader = Boto3R2AudioUploader(client, bucket)
    uploaded = 0
    for index, record in enumerate(validated, start=1):
        path = root / Path(*_relative_audio_path(record.audio_key).parts)
        changed = uploader.upload(r2_audio_key(record.checksum), path, record.checksum, force=force)
        uploaded += int(changed)
        if on_progress is not None:
            on_progress(index, len(validated), record, "uploaded" if changed else "skipped")
    return UploadSummary(uploaded, len(validated) - uploaded, len(validated))


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
    parser.add_argument(
        "--resume", action="store_true", help="preserve matching catalog transcripts"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    profile = profile_for(args.profile)
    expected_count = expected_count_for(profile, args.limit_per_source)
    if args.max_records is not None and args.max_records < 1:
        raise SystemExit("--max-records must be positive")
    if args.command == "scan":
        candidates = select_audio(args.source, args.limit_per_source, profile=profile)
        records = records_from_audio(candidates)
        if len(records) != expected_count:
            raise SystemExit(
                f"profile {profile.name} expected {expected_count} records, found {len(records)}"
            )
        if args.dry_run:
            print(f"would write {len(records)} private catalog records")
        else:
            records, _ = scan_catalog(
                args.source,
                args.catalog,
                profile=profile,
                limit_per_source=args.limit_per_source,
                resume=args.resume,
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
        validated = validate_records(records, expected_count, root=args.source)
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
