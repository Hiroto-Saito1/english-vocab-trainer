from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from ingest_support import records

from english_vocab_trainer.ingest.catalog import select_audio
from english_vocab_trainer.ingest.storage import (
    error_report_path,
    manifest_path,
    read_catalog,
    read_error_report,
    read_manifest,
    write_catalog,
)
from english_vocab_trainer.ingest.validation import (
    r2_audio_key,
    transcription_failure_reason,
    validate_records,
    validate_term,
    validate_transcript,
)


def test_private_catalog_transcribe_resume_and_validation(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    record = records(1)[0]
    write_catalog(catalog, [record])
    assert read_catalog(catalog) == [record]
    assert read_catalog(tmp_path / "absent.jsonl") == []

    assert validate_records(records(4), expected_count=4)
    with pytest.raises(ValueError, match="missing transcript"):
        validate_records([replace(record, transcript=None)], 1)
    with pytest.raises(ValueError, match="English only"):
        validate_transcript("これは English definition example")
    with pytest.raises(ValueError, match="too short"):
        validate_transcript("too short")
    with pytest.raises(ValueError, match="expected"):
        validate_records([], expected_count=1)
    with pytest.raises(ValueError, match="duplicate"):
        validate_records([record, record], expected_count=2)
    for non_latin in (
        "A clear definition пример sentence.",
        "A clear definition 한국어 sentence.",
        "A clear definition عربية sentence.",
        "A clear definition 漢字 sentence.",
    ):
        with pytest.raises(ValueError, match="Latin|English only"):
            validate_transcript(non_latin)
    assert validate_transcript("Café gives a clear English definition.")
    assert (
        validate_transcript("“Café”—a useful definition… with an example. ")
        == '"Café" - a useful definition... with an example.'
    )
    with pytest.raises(ValueError, match="unsafe Unicode"):
        validate_transcript("A clear\x00 English definition sentence.")
    with pytest.raises(ValueError, match="ASCII punctuation"):
        validate_transcript("A clear English definition sentence. 😀")
    with pytest.raises(ValueError, match="English only|ASCII punctuation"):
        validate_transcript("A clear English definition。 sentence.")
    with pytest.raises(ValueError, match="repeated-word"):
        validate_transcript("An English definition says on on on on on on on on for too long.")
    with pytest.raises(ValueError, match="repeated-phrase"):
        validate_transcript(("A clear English phrase repeats here. " * 8).strip())
    long_english = " ".join(
        f"word{chr(ord('a') + index // 26)}{chr(ord('a') + index % 26)}" for index in range(150)
    )
    assert validate_transcript(long_english) == long_english
    assert validate_term("well-known O'Connor") == "well-known O'Connor"
    with pytest.raises(ValueError, match="ASCII letters"):
        validate_term("term 7")


def test_catalog_integrity_rejects_tampering_and_canonical_key() -> None:
    record = records(1)[0]
    assert r2_audio_key(record.checksum) == f"audio/{record.checksum}.mp3"
    for malformed in ("A" * 64, "a" * 63, "a" * 65, "not-a-checksum"):
        with pytest.raises(ValueError):
            r2_audio_key(malformed)
    for altered in (
        replace(record, id="audio-" + "a" * 64),
        replace(record, audio_key="../outside.mp3"),
        replace(record, source="other"),
        replace(record, term="wrong"),
    ):
        with pytest.raises(ValueError):
            validate_records([altered], expected_count=1)


def test_profile_manifest_and_error_report_reject_malformed_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved source is missing"):
        select_audio(tmp_path)
    catalog = tmp_path / ".private" / "catalog.jsonl"
    manifest_path(catalog).parent.mkdir(parents=True)
    manifest_path(catalog).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is invalid"):
        read_manifest(catalog)
    error_report_path(catalog).write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="error report is corrupt"):
        read_error_report(catalog)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("opaque"), "runtime-failure"),
        (ValueError("transcript-quality-repeated-word"), "transcript-quality-repeated-word"),
        (
            ValueError("transcript must use Latin alphabetic characters only"),
            "transcript-script-invalid",
        ),
        (ValueError("transcript contains unsafe Unicode"), "transcript-unicode-invalid"),
        (
            ValueError("transcript must contain ASCII punctuation only"),
            "transcript-punctuation-invalid",
        ),
        (
            ValueError("transcript contains an unbound combining mark"),
            "transcript-unbound-combining-mark",
        ),
        (ValueError("other validation"), "transcript-validation-failed"),
    ],
)
def test_failure_reason_is_whitelisted(error: Exception, reason: str) -> None:
    assert transcription_failure_reason(error) == reason
