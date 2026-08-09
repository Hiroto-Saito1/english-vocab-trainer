from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ingest-env"


def _fake_uv(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    cache = tmp_path / "cache"
    executable = fake_bin / "uv"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'arguments="$*"\n'
        "arguments=\"${arguments//$'\\n'/ }\"\n"
        'printf \'%s|%s\\n\' "${UV_PROJECT_ENVIRONMENT:-}" "$arguments" >> "$FAKE_UV_LOG"\n'
        'if [ "${1:-}" = cache ] && [ "${2:-}" = dir ]; then printf \'%s\\n\' "$FAKE_CACHE"; fi\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return fake_bin, log, cache


def test_ingest_wrapper_uses_external_cache_environment_and_frozen_lock(tmp_path: Path) -> None:
    fake_bin, log, cache = _fake_uv(tmp_path)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "XDG_CACHE_HOME": str(cache),
        "FAKE_UV_LOG": str(log),
        "FAKE_CACHE": str(cache),
    }
    syntax = subprocess.run(["sh", "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    doctor = subprocess.run(
        ["bash", str(SCRIPT), "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert doctor.returncode == 0, doctor.stderr
    command = subprocess.run(
        ["bash", str(SCRIPT), "validate", "--source", "private-source"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert command.returncode == 0, command.stderr

    calls = log.read_text(encoding="utf-8").splitlines()
    assert any(call.endswith("|lock --check") for call in calls)
    assert sum(call.endswith("|sync --group ingest --frozen") for call in calls) == 2
    assert any("|run --group ingest python -c " in call for call in calls)
    assert any(
        call.endswith("|run --group ingest vocab-ingest validate --source private-source")
        for call in calls
    )
    expected_prefix = f"{cache}/english-vocab-trainer/ingest-"
    assert all(call.split("|", 1)[0].startswith(expected_prefix) for call in calls)
    assert all(".venv" not in call.split("|", 1)[0] for call in calls)


def test_ingest_wrapper_rejects_project_local_cache(tmp_path: Path) -> None:
    fake_bin, log, cache = _fake_uv(tmp_path)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "XDG_CACHE_HOME": str(SCRIPT.parents[1] / ".cache"),
        "FAKE_UV_LOG": str(log),
        "FAKE_CACHE": str(cache),
    }
    rejected = subprocess.run(
        ["bash", str(SCRIPT), "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert "must be outside the project directory" in rejected.stderr
    assert not log.exists()
