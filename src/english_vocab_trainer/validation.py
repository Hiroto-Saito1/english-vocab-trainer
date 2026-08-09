from __future__ import annotations

import re

_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def validate_english_transcript(transcript: str) -> str:
    value = transcript.strip()
    if _JAPANESE.search(value):
        raise ValueError("transcript must be English only")
    if len(value) < 12 or len(value.split()) < 3:
        raise ValueError("transcript is too short to contain a definition or example")
    return value
