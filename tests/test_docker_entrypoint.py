from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ENTRYPOINT = ROOT / "deploy" / "docker-entrypoint.sh"


def deployment_environment(executable_directory: Path) -> dict[str, str]:
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


def _run_entrypoint(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ENTRYPOINT)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


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
    environment = deployment_environment(executable_directory)
    completed = _run_entrypoint(environment)
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
    missing = deployment_environment(executable_directory)
    missing.pop("LITESTREAM_SECRET_ACCESS_KEY")
    completed = _run_entrypoint(missing)
    assert completed.returncode == 64
    assert "LITESTREAM_SECRET_ACCESS_KEY" in completed.stderr
    assert "backup-secret-not-printed" not in completed.stderr

    invalid = deployment_environment(executable_directory)
    invalid["VOCAB_DB_PATH"] = "/data/../outside.db"
    completed = _run_entrypoint(invalid)
    assert completed.returncode == 64
    assert "canonical" in completed.stderr

    invalid = deployment_environment(executable_directory)
    invalid["APP_ENV"] = "test"
    completed = _run_entrypoint(invalid)
    assert completed.returncode == 64 and "APP_ENV=production" in completed.stderr

    invalid = deployment_environment(executable_directory)
    invalid["AUDIO_BACKEND"] = "filesystem"
    completed = _run_entrypoint(invalid)
    assert completed.returncode == 64 and "AUDIO_BACKEND=r2" in completed.stderr

    for key, value, message in (
        ("VOCAB_DB_PATH", "/data/vocab#bad.db", "unsupported characters"),
        ("LITESTREAM_R2_BUCKET", "bad_bucket", "R2-safe"),
        ("LITESTREAM_R2_ENDPOINT_URL", "https://bad.example/path", "Cloudflare R2 HTTPS"),
    ):
        invalid = deployment_environment(executable_directory)
        invalid[key] = value
        completed = _run_entrypoint(invalid)
        assert completed.returncode == 64 and message in completed.stderr

    derived = deployment_environment(executable_directory)
    derived.pop("ALLOWED_HOSTS")
    derived["FLY_APP_NAME"] = "vocab-study"
    completed = _run_entrypoint(derived)
    assert completed.returncode == 0
