import importlib.resources
import sqlite3

SCHEMA_VERSION = 1
EXPECTED_COLUMNS = {
    "schema_metadata": {"singleton", "version", "catalog_sha256", "catalog_count", "published_at"},
    "words": {"id", "term", "level", "definition", "audio_key"},
    "word_state": {"word_id", "due_at", "card_json", "version", "mutation_id"},
    "reviews": {"id", "word_id", "action", "reviewed_at"},
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
    tables -= {"d1_migrations"}
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
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'd1_migrations' LIMIT 1"
    ).fetchone()
    if existing is None:
        schema = (
            importlib.resources.files("english_vocab_trainer").joinpath("schema.sql").read_text()
        )
        connection.executescript(schema)
    validate_database_schema(connection)
