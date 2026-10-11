import importlib.resources
import sqlite3


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    schema = importlib.resources.files("english_vocab_trainer").joinpath("schema.sql").read_text()
    connection.executescript(schema)
