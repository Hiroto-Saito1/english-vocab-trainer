import sqlite3
from pathlib import Path

import pytest

from english_vocab_trainer.adapters.local.migrations import MigrationError, apply_migrations


def test_migrations_are_idempotent_and_recorded(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text("CREATE TABLE things(id INTEGER PRIMARY KEY);")
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)
    apply_migrations(connection, directory)
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    connection.close()


def test_migration_checksum_change_fails(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    path = directory / "0001.sql"
    path.write_text("CREATE TABLE things(id INTEGER PRIMARY KEY);")
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)
    path.write_text("CREATE TABLE changed(id INTEGER PRIMARY KEY);")
    with pytest.raises(MigrationError):
        apply_migrations(connection, directory)
    connection.close()


def test_failed_migration_rolls_back_ddl_and_history(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text(
        "CREATE TABLE transient(id INTEGER PRIMARY KEY);\nINSERT INTO missing_table VALUES (1);\n"
    )
    connection = sqlite3.connect(":memory:")

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(connection, directory)

    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'transient'"
        ).fetchone()
        is None
    )
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
    connection.close()


def test_comments_empty_statements_and_trigger_are_applied(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text(
        "-- This comment includes a semicolon; safely ignored.\n"
        ";\n"
        "CREATE TABLE source(id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE audit(source_id INTEGER NOT NULL);\n"
        "CREATE TRIGGER copied AFTER INSERT ON source\n"
        "BEGIN\n"
        "  INSERT INTO audit(source_id) VALUES (NEW.id);\n"
        "END;\n"
        "-- A trailing comment does not need a semicolon.\n"
    )
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)
    connection.execute("INSERT INTO source(id) VALUES (7)")

    assert connection.execute("SELECT source_id FROM audit").fetchone()[0] == 7
    connection.close()
