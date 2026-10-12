"""CI private artifact policy tests."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_rejects_private_catalog_artifacts_without_rejecting_source_modules() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"git ls-files \| grep -E '([^']+)'", workflow)
    assert match is not None
    prohibited = re.compile(match.group(1)).search

    assert prohibited("src/english_vocab_trainer/ingest/catalog.py") is None
    assert prohibited("docs/catalog.md") is None
    assert prohibited("config/settings.json") is None
    for path in (
        ".private/catalog.jsonl",
        ".private/mvp-catalog.jsonl",
        ".private/catalog.jsonl.manifest.json",
        ".private/catalog.jsonl.journal.jsonl",
        ".private/catalog.jsonl.transcription-errors.jsonl",
        ".private/catalog.jsonl.audit.json",
        "private/audio/word.mp3",
    ):
        assert prohibited(path) is not None
