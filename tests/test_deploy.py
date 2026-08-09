from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from english_vocab_trainer.adapters.local.migrations import apply_migrations
from english_vocab_trainer.restore_drill import RestoreDrillError, restore_drill

ROOT = Path(__file__).parents[1]
ENTRYPOINT = ROOT / "deploy" / "docker-entrypoint.sh"
LITESTREAM_CONFIG = ROOT / "deploy" / "litestream.yml"


def deployment_environment(data: Path, executable_directory: Path) -> dict[str, str]:
    return {
        "PATH": f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
        "APP_ENV": "production",
        "VOCAB_DB_PATH": "/data/vocab.db",
        "APP_PASSWORD_HASH": "hash-not-printed",
        "SESSION_SIGNING_SECRET": "secret-not-printed",
        "ALLOWED_HOSTS": "vocab.example.test",
        "AUDIO_BACKEND": "r2",
        "R2_ENDPOINT_URL": "https://audio.example.test",
        "R2_ACCESS_KEY_ID": "audio-key-not-printed",
        "R2_SECRET_ACCESS_KEY": "audio-secret-not-printed",
        "R2_BUCKET": "audio",
        "LITESTREAM_ACCESS_KEY_ID": "backup-key-not-printed",
        "LITESTREAM_SECRET_ACCESS_KEY": "backup-secret-not-printed",
        "LITESTREAM_R2_ENDPOINT_URL": "https://abc123.r2.cloudflarestorage.com",
        "LITESTREAM_R2_BUCKET": "backups",
    }


def fake_litestream(directory: Path) -> None:
    executable = directory / "litestream"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    executable.chmod(0o755)
    mkdir = directory / "mkdir"
    mkdir.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    mkdir.chmod(0o755)
    chown = directory / "chown"
    chown.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >&2\n", encoding="utf-8")
    chown.chmod(0o755)
    setpriv = directory / "setpriv"
    setpriv.write_text(
        '#!/bin/sh\nprintf "setpriv:%s\\n" "$1" >&2\n'
        'while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) exec "$@" ;; esac; done\n',
        encoding="utf-8",
    )
    setpriv.chmod(0o755)


def test_entrypoint_validates_env_and_uses_constant_litestream_argv(tmp_path: Path) -> None:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    fake_litestream(executable_directory)
    environment = deployment_environment(tmp_path / "data", executable_directory)
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=environment, check=False
    )
    assert completed.returncode == 0
    assert completed.stdout.splitlines()[:4] == [
        "replicate",
        "-config",
        "/etc/litestream.yml",
        "-restore-if-db-not-exists",
    ]
    assert "--workers 1" in completed.stdout
    assert "not-printed" not in completed.stdout + completed.stderr
    assert "-R\nvocab:vocab\n/data" in completed.stderr
    assert "setpriv:--reuid=vocab" in completed.stderr


def test_entrypoint_fails_without_echoing_a_secret_or_accepting_non_volume_path(
    tmp_path: Path,
) -> None:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    fake_litestream(executable_directory)
    missing = deployment_environment(tmp_path / "data", executable_directory)
    missing.pop("LITESTREAM_SECRET_ACCESS_KEY")
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=missing, check=False
    )
    assert completed.returncode == 64
    assert "LITESTREAM_SECRET_ACCESS_KEY" in completed.stderr
    assert "backup-secret-not-printed" not in completed.stderr

    invalid = deployment_environment(tmp_path / "data", executable_directory)
    invalid["VOCAB_DB_PATH"] = "/data/../outside.db"
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=invalid, check=False
    )
    assert completed.returncode == 64
    assert "canonical" in completed.stderr

    invalid = deployment_environment(tmp_path / "data", executable_directory)
    invalid["APP_ENV"] = "test"
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=invalid, check=False
    )
    assert completed.returncode == 64 and "APP_ENV=production" in completed.stderr

    invalid = deployment_environment(tmp_path / "data", executable_directory)
    invalid["AUDIO_BACKEND"] = "filesystem"
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=invalid, check=False
    )
    assert completed.returncode == 64 and "AUDIO_BACKEND=r2" in completed.stderr

    for key, value, message in (
        ("VOCAB_DB_PATH", "/data/vocab#bad.db", "unsupported characters"),
        ("LITESTREAM_R2_BUCKET", "bad_bucket", "R2-safe"),
        ("LITESTREAM_R2_ENDPOINT_URL", "https://bad.example/path", "Cloudflare R2 HTTPS"),
    ):
        invalid = deployment_environment(tmp_path / "data", executable_directory)
        invalid[key] = value
        completed = subprocess.run(
            ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=invalid, check=False
        )
        assert completed.returncode == 64 and message in completed.stderr

    derived = deployment_environment(tmp_path / "data", executable_directory)
    derived.pop("ALLOWED_HOSTS")
    derived["FLY_APP_NAME"] = "vocab-study"
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT)], text=True, capture_output=True, env=derived, check=False
    )
    assert completed.returncode == 0


def test_restore_drill_refuses_live_or_existing_output_and_validates_restored_schema(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live.db"
    connection = sqlite3.connect(live)
    apply_migrations(connection)
    connection.execute("INSERT INTO words VALUES('one','one',1,NULL,'one.mp3')")
    connection.commit()
    connection.close()
    output = tmp_path / "drills" / "restored.db"
    commands: list[Sequence[str]] = []

    def restore(command: Sequence[str]) -> None:
        commands.append(command)
        source = sqlite3.connect(live)
        target = sqlite3.connect(output)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    assert restore_drill(
        live_database=live, output_database=output, config=Path("/safe.yml"), runner=restore
    ) == {
        "words": 1,
        "review_events": 0,
    }
    assert commands == [
        ("litestream", "restore", "-config", "/safe.yml", "-o", str(output), str(live))
    ]
    with pytest.raises(RestoreDrillError):
        restore_drill(live_database=live, output_database=live, config=Path("/safe.yml"))
    with pytest.raises(RestoreDrillError):
        restore_drill(live_database=live, output_database=output, config=Path("/safe.yml"))
    with pytest.raises(RestoreDrillError):
        restore_drill(
            live_database=live, output_database=Path(f"{live}-wal"), config=Path("/safe.yml")
        )


def test_restore_drill_removes_only_its_partial_output(tmp_path: Path) -> None:
    live, output = tmp_path / "live.db", tmp_path / "new.db"
    connection = sqlite3.connect(live)
    apply_migrations(connection)
    connection.close()

    def partial(_: Sequence[str]) -> None:
        output.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(RestoreDrillError):
        restore_drill(
            live_database=live, output_database=output, config=Path("/safe.yml"), runner=partial
        )
    assert not output.exists() and live.exists()


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
    failed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy" / "render_fly_config.py"),
            "--output",
            str(tmp_path / "bad.toml"),
            "--app",
            "Invalid app",
            "--volume",
            "Invalid volume",
        ],
        check=False,
    )
    assert failed.returncode != 0
    failed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy" / "render_fly_config.py"),
            "--output",
            str(tmp_path / "bad-volume.toml"),
            "--app",
            "vocab-study",
            "--volume",
            "Invalid volume",
        ],
        check=False,
    )
    assert failed.returncode != 0
