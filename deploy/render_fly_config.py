#!/usr/bin/env python3
"""Render the sole Fly volume name into a temporary, valid TOML configuration."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

FLY_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
VOLUME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def render(template: Path, output: Path, app: str, volume: str) -> None:
    if not FLY_NAME.fullmatch(app):
        raise ValueError("FLY_APP must be a lowercase Fly app name")
    if not VOLUME.fullmatch(volume):
        raise ValueError("FLY_VOLUME must be a lowercase Fly volume name")
    source = template.read_text(encoding="utf-8")
    replacements = {
        "${FLY_APP}": app,
        "${FLY_HOST}": f"{app}.fly.dev",
        "${FLY_VOLUME}": volume,
    }
    if source.count("${FLY_APP}") != 1 or source.count("${FLY_VOLUME}") != 1:
        raise ValueError("Fly template must contain exactly one app and one volume marker")
    if source.count("${FLY_HOST}") != 1:
        raise ValueError("Fly template must contain exactly one health-check host marker")
    for marker, value in replacements.items():
        source = source.replace(marker, value)
    output.write_text(source, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=Path("fly.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--volume", required=True)
    arguments = parser.parse_args(argv)
    render(arguments.template, arguments.output, arguments.app, arguments.volume)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
