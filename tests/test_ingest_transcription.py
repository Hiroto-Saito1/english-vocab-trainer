from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from ingest_support import records

from english_vocab_trainer.ingest.storage import (
    catalog_lock,
    journal_path,
    read_catalog,
    write_catalog,
)
from english_vocab_trainer.ingest.transcription import MlxWhisperTranscriber, transcribe_catalog


def test_mlx_transcriber_uses_english_large_turbo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def transcribe(path: str, **kwargs: object) -> dict[str, str]:
        calls["path"] = path
        calls.update(kwargs)
        return {"text": " English definition and example. "}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    result = MlxWhisperTranscriber().transcribe(tmp_path / "audio.mp3")
    assert result == "English definition and example."
    assert calls["language"] == "en"
    assert calls["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert calls["temperature"] == 0.0
    assert calls["condition_on_previous_text"] is False


def test_transcribe_checkpoints_and_resumes_after_failure(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    current = [replace(record, transcript=None) for record in records(2)]
    write_catalog(catalog, current)

    class FailingTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("interrupted")
            return "An English definition with an example."

    with pytest.raises(RuntimeError, match="interrupted"):
        transcribe_catalog(tmp_path, catalog, FailingTranscriber(), force=False)
    assert read_catalog(catalog)[0].transcript is not None

    class ResumingTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            return "An English definition with an example."

    resumed = ResumingTranscriber()
    updates: list[tuple[int, int, str]] = []
    summary = transcribe_catalog(
        tmp_path,
        catalog,
        resumed,
        force=False,
        on_transcribed=lambda completed, total, record: updates.append(
            (completed, total, record.term)
        ),
    )
    assert resumed.calls == 1 and all(record.transcript for record in read_catalog(catalog))
    assert summary.completed == 2 and summary.skipped == 1 and updates == [(2, 2, "term b")]
    with catalog_lock(catalog):
        with pytest.raises(RuntimeError, match="already"):
            with catalog_lock(catalog):
                pass


def test_force_transcription_resets_pending_journal(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, [replace(record, transcript=None) for record in records(2)])

    class StableTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            return "An English definition with an example."

    transcriber = StableTranscriber()
    transcribe_catalog(tmp_path, catalog, transcriber, force=False, max_records=1)
    assert journal_path(catalog).exists()
    summary = transcribe_catalog(tmp_path, catalog, transcriber, force=True, max_records=1)
    assert summary.completed == 1 and transcriber.calls == 2
    # A second forced retry starts from the durable base, never conflicting with its prior journal.
    transcribe_catalog(tmp_path, catalog, transcriber, force=True, max_records=1)


def test_transcription_batch_caps_failed_attempts(tmp_path: Path) -> None:
    catalog = tmp_path / ".private" / "catalog.jsonl"
    write_catalog(catalog, [replace(record, transcript=None) for record in records(2)])

    class FailingTranscriber:
        calls = 0

        def transcribe(self, _: Path) -> str:
            self.calls += 1
            raise RuntimeError("not persisted")

    transcriber = FailingTranscriber()
    summary = transcribe_catalog(
        tmp_path,
        catalog,
        transcriber,
        force=False,
        continue_on_error=True,
        max_records=1,
    )
    assert transcriber.calls == 1 and summary.errors == 1 and summary.completed == 0
