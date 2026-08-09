from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


class MigrationError(RuntimeError):
    pass


def apply_migrations(connection: sqlite3.Connection, directory: Path) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        existing = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?", (path.name,)
        ).fetchone()
        if existing:
            if existing[0] != checksum:
                raise MigrationError(f"checksum mismatch: {path.name}")
            continue
        try:
            # sqlite3.executescript owns its transaction boundary; migration SQL
            # itself is committed before the checksum record is inserted.
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,checksum) VALUES(?,?)", (path.name, checksum)
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
