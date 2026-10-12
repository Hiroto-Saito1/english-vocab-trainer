import importlib.resources
import sqlite3

SCHEMA_VERSION = 2
EXPECTED_COLUMNS = {
    "schema_metadata": {"singleton", "version"},
    "words": {"id", "term", "level", "transcript", "audio_key", "tier"},
    "word_state": {"word_id", "due_at"},
    "review_events": {"id", "word_id", "rating", "reviewed_at", "voided_at"},
    "login_attempts": {"attempted_at"},
}


class SchemaMismatchError(RuntimeError):
    pass


def validate_database_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {
        table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for table in tables
    }
    versions = (
        [
            (int(row[0]), int(row[1]))
            for row in connection.execute("SELECT singleton,version FROM schema_metadata")
        ]
        if "schema_metadata" in tables
        else []
    )
    if (
        tables != set(EXPECTED_COLUMNS)
        or columns != EXPECTED_COLUMNS
        or versions != [(1, SCHEMA_VERSION)]
    ):
        raise SchemaMismatchError(
            "database schema is incompatible; recreate the database and republish the catalog"
        )


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if existing is None:
        schema = (
            importlib.resources.files("english_vocab_trainer").joinpath("schema.sql").read_text()
        )
        connection.executescript(schema)
    validate_database_schema(connection)
