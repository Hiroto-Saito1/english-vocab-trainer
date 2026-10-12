"""Fly and Litestream deployment configuration tests."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from english_vocab_trainer.adapters.local.database import SCHEMA_VERSION

ROOT = Path(__file__).parents[1]
LITESTREAM_CONFIG = ROOT / "deploy" / "litestream.yml"


def test_litestream_config_separates_audio_credentials_and_rendered_fly_toml(
    tmp_path: Path,
) -> None:
    config = LITESTREAM_CONFIG.read_text(encoding="utf-8")
    top_level = {
        line.split(":", maxsplit=1)[0]
        for line in config.splitlines()
        if line and not line.startswith((" ", "#")) and ":" in line
    }
    assert {
        "retention",
        "validation",
        "verify-compaction",
        "shutdown-sync-timeout",
        "dbs",
    } <= top_level
    assert "replica:" in config and "replicas:" not in config
    assert f"path: backups/sqlite/schema-v{SCHEMA_VERSION}/vocab.db" in config
    assert "path: backups/sqlite/vocab.db" not in config
    assert "retention:\n  # No DeleteObject" in config
    assert "validation:\n  interval: 6h" in config
    assert "verify-compaction: true" in config and "shutdown-sync-timeout: 30s" in config
    assert "R2_ACCESS_KEY_ID" not in config and "R2_SECRET_ACCESS_KEY" not in config
    assert "LITESTREAM_ACCESS_KEY_ID" not in config and "LITESTREAM_SECRET_ACCESS_KEY" not in config
    template = (ROOT / "fly.toml").read_text(encoding="utf-8")
    assert template.count("${FLY_APP}") == template.count("${FLY_VOLUME}") == 1

    rendered = tmp_path / "fly.toml"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy" / "render_fly_config.py"),
            "--output",
            str(rendered),
            "--app",
            "vocab-study",
            "--volume",
            "vocab_data",
        ],
        check=True,
    )
    parsed = tomllib.loads(rendered.read_text(encoding="utf-8"))
    assert parsed["app"] == "vocab-study"
    assert parsed["http_service"]["checks"] == [
        {
            "grace_period": "15s",
            "interval": "15s",
            "method": "GET",
            "path": "/readyz",
            "timeout": "5s",
            "headers": {"Host": "vocab-study.fly.dev", "X-Forwarded-Proto": "https"},
        }
    ]
    assert parsed["mounts"] == [
        {"source": "vocab_data", "destination": "/data", "snapshot_retention": 14}
    ]
    for app, volume in (
        ("Invalid app", "Invalid volume"),
        ("vocab-study", "Invalid volume"),
    ):
        failed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "deploy" / "render_fly_config.py"),
                "--output",
                str(tmp_path / "bad.toml"),
                "--app",
                app,
                "--volume",
                volume,
            ],
            check=False,
        )
        assert failed.returncode != 0
