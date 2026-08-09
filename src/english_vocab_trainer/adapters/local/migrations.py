from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


class MigrationError(RuntimeError):
    pass


def _statements(sql: str) -> list[str]:
    """Split SQL only at boundaries recognised by SQLite.

    ``sqlite3.complete_statement`` keeps trigger bodies together and does not
    mistake semicolons inside quoted values for a statement terminator.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        # SQLite permits the last regular statement without a semicolon.  It
        # also treats a comment-only tail as a no-op, so leave validation to
        # SQLite instead of trying to parse SQL ourselves.
        statements.append(buffer)
    return statements


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
            connection.execute("BEGIN IMMEDIATE")
            for statement in _statements(sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,checksum) VALUES(?,?)", (path.name, checksum)
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
