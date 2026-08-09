import sqlite3
from pathlib import Path

import pytest

from english_vocab_trainer.adapters.local.migrations import MigrationError, apply_migrations


def test_packaged_migrations_and_connection_pragmas_work_without_a_checkout(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "installed.db")
    apply_migrations(connection)
    versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert versions == {"0001_initial.sql", "0002_auth.sql", "0003_tier.sql"}
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_migrations_are_idempotent_and_recorded(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text("CREATE TABLE things(id INTEGER PRIMARY KEY);")
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)
    apply_migrations(connection, directory)
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    connection.close()


def test_tier_migration_upgrades_an_existing_catalog_without_losing_rows(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    packaged = Path(__file__).parents[1] / "src/english_vocab_trainer/migrations"
    for name in ("0001_initial.sql", "0002_auth.sql"):
        (directory / name).write_text((packaged / name).read_text())
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)
    connection.execute(
        "INSERT INTO words(id,term,level,transcript,audio_key) "
        "VALUES('old','old',NULL,NULL,'old.mp3')"
    )
    connection.commit()
    (directory / "0003_tier.sql").write_text((packaged / "0003_tier.sql").read_text())
    apply_migrations(connection, directory)
    assert connection.execute("SELECT tier FROM words WHERE id='old'").fetchone()[0] == "unknown"
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


def test_multiple_statements_on_one_line_are_applied(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text(
        "CREATE TABLE one(id INTEGER); CREATE TABLE two(id INTEGER);\n"
    )
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)

    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"one", "two"} <= tables
    connection.close()


def test_semicolon_in_quoted_string_is_not_a_statement_boundary(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text(
        "CREATE TABLE notes(value TEXT); INSERT INTO notes(value) VALUES ('one; two');\n"
    )
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)

    assert connection.execute("SELECT value FROM notes").fetchone()[0] == "one; two"
    connection.close()


def test_semicolons_in_comments_are_not_statement_boundaries(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text(
        "-- Line comment; still a comment.\n"
        "/* Block comment; also still a comment. */\n"
        "CREATE TABLE comments(id INTEGER);\n"
    )
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)

    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'comments'").fetchone()
    connection.close()


def test_empty_statement_and_trigger_body_are_applied(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001.sql").write_text(
        ";\n"
        "CREATE TABLE source(id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE audit(source_id INTEGER NOT NULL);\n"
        "CREATE TRIGGER copied AFTER INSERT ON source\n"
        "BEGIN\n"
        "  INSERT INTO audit(source_id) VALUES (NEW.id);\n"
        "  INSERT INTO audit(source_id) VALUES (NEW.id + 1);\n"
        "END;\n"
    )
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, directory)
    connection.execute("INSERT INTO source(id) VALUES (7)")

    assert [
        row[0] for row in connection.execute("SELECT source_id FROM audit ORDER BY source_id")
    ] == [
        7,
        8,
    ]
    connection.close()
