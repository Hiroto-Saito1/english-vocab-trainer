"""Safe, manual Litestream restore drill for the production SQLite backup."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[[Sequence[str]], None]


class RestoreDrillError(RuntimeError):
    pass


def _run(command: Sequence[str]) -> None:
    subprocess.run(command, check=True)


def restore_drill(
    *, live_database: Path, output_database: Path, config: Path, runner: Runner = _run
) -> dict[str, int]:
    """Restore only to a new path, validate SQLite, and return non-sensitive counts."""
    live = live_database.resolve()
    output = output_database.resolve()
    protected_paths = {live, Path(f"{live}-wal"), Path(f"{live}-shm")}
    if output in protected_paths or output.exists():
        raise RestoreDrillError("restore output must be a new path distinct from the live database")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        runner(
            (
                "litestream",
                "restore",
                "-config",
                str(config),
                "-o",
                str(output),
                str(live),
            )
        )
        if not output.is_file():
            raise RestoreDrillError(
                "restore completed without creating the requested output database"
            )
        connection = sqlite3.connect(output)
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RestoreDrillError("restored database failed SQLite quick_check")
            versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            expected = {"0001_initial.sql", "0002_auth.sql"}
            if not expected <= versions:
                raise RestoreDrillError("restored database is missing expected migrations")
            return {
                "words": int(connection.execute("SELECT count(*) FROM words").fetchone()[0]),
                "review_events": int(
                    connection.execute("SELECT count(*) FROM review_events").fetchone()[0]
                ),
            }
        except sqlite3.DatabaseError as exc:
            raise RestoreDrillError("restored database is not readable") from exc
        finally:
            connection.close()
    except Exception:
        for temporary in (output, Path(f"{output}-wal"), Path(f"{output}-shm")):
            temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a Litestream backup only to a new drill path"
    )
    parser.add_argument("--live-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("/etc/litestream.yml"))
    arguments = parser.parse_args(argv)
    counts = restore_drill(
        live_database=arguments.live_db, output_database=arguments.output, config=arguments.config
    )
    print(f"restore drill passed: words={counts['words']} review_events={counts['review_events']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
