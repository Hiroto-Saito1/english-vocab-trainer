import sqlite3
from pathlib import Path

from english_vocab_trainer.adapters.local.database import initialize_database

EXPECTED_TABLES = {"words", "word_state", "review_events", "login_attempts"}


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
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_current_schema_initialization_is_idempotent_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "current.db"
    connection = sqlite3.connect(path)
    initialize_database(connection)
    connection.execute("INSERT INTO words VALUES('one','one',9,NULL,'one.mp3','upper')")
    connection.commit()
    connection.close()

    reopened = sqlite3.connect(path)
    initialize_database(reopened)
    assert reopened.execute("SELECT term FROM words WHERE id='one'").fetchone() == ("one",)
    reopened.close()
