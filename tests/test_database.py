import sqlite3
from pathlib import Path

import pytest

from english_vocab_trainer.adapters.local.database import (
    SCHEMA_VERSION,
    SchemaMismatchError,
    initialize_database,
)

EXPECTED_TABLES = {
    "schema_metadata",
    "words",
    "word_state",
    "reviews",
    "login_attempts",
}


def test_current_schema_and_connection_pragmas(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "installed.db")
    initialize_database(connection)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    }
    assert tables == EXPECTED_TABLES
    assert {row[1] for row in connection.execute("PRAGMA table_info(word_state)")} == {
        "word_id",
        "due_at",
    }
    assert {row[1] for row in connection.execute("PRAGMA table_info(words)")} == {
        "id",
        "term",
        "level",
        "definition",
        "audio_key",
    }
    assert {row[1] for row in connection.execute("PRAGMA table_info(reviews)")} == {
        "id",
        "word_id",
        "action",
        "reviewed_at",
    }
    assert SCHEMA_VERSION == 4
    assert connection.execute("SELECT version FROM schema_metadata").fetchone() == (SCHEMA_VERSION,)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_current_schema_initialization_is_idempotent_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "current.db"
    connection = sqlite3.connect(path)
    initialize_database(connection)
    connection.execute(
        "INSERT INTO words(id,term,level,definition,audio_key) "
        "VALUES('one','one',9,'definition','one.mp3')"
    )
    connection.commit()
    connection.close()

    reopened = sqlite3.connect(path)
    initialize_database(reopened)
    assert reopened.execute("SELECT term FROM words WHERE id='one'").fetchone() == ("one",)
    reopened.close()


def test_incompatible_columns_and_version_fail_without_migration(tmp_path: Path) -> None:
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute(
        "CREATE TABLE reviews("
        "id TEXT PRIMARY KEY,word_id TEXT,answer TEXT,reviewed_at TEXT,voided_at TEXT,"
        "payload TEXT,user_id TEXT)"
    )
    old.commit()
    with pytest.raises(SchemaMismatchError, match="recreate"):
        initialize_database(old)
    assert old.execute("PRAGMA table_info(reviews)").fetchall()
    old.close()

    wrong_version = sqlite3.connect(tmp_path / "wrong-version.db")
    initialize_database(wrong_version)
    wrong_version.execute("UPDATE schema_metadata SET version=999")
    wrong_version.commit()
    with pytest.raises(SchemaMismatchError, match="recreate"):
        initialize_database(wrong_version)
    wrong_version.close()
