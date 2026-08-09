from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from english_vocab_trainer.adapters.local.migrations import apply_migrations


class SQLiteLoginAttemptLimiter:
    """A global limiter for the one configured account; no client IP is trusted."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def reserve(self, now: datetime) -> bool:
        cutoff = int((now - timedelta(minutes=15)).timestamp())
        connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            apply_migrations(connection, Path(__file__).parents[4] / "migrations")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM login_attempts WHERE attempted_at<?", (cutoff,))
            count = int(connection.execute("SELECT count(*) FROM login_attempts").fetchone()[0])
            if count >= 5:
                connection.execute("COMMIT")
                return False
            connection.execute(
                "INSERT INTO login_attempts(attempted_at) VALUES(?)",
                (int(now.astimezone(UTC).timestamp()),),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def clear(self) -> None:
        connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            apply_migrations(connection, Path(__file__).parents[4] / "migrations")
            connection.execute("DELETE FROM login_attempts")
        finally:
            connection.close()
