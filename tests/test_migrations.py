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
