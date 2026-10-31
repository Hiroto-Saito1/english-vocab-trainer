from __future__ import annotations

import sqlite3


class FakeStatement:
    def __init__(self, db: FakeD1, sql: str) -> None:
        self.db, self.sql = db, sql
        self.params: tuple[object, ...] = ()

    def bind(self, *params: object) -> FakeStatement:
        self.params = params
        return self

    async def first(self) -> dict[str, object] | None:
        row = self.db.connection.execute(self.sql, self.params).fetchone()
        return dict(row) if row else None

    async def run(self) -> dict[str, object]:
        cursor = self.db.connection.execute(self.sql, self.params)
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        changes = self.db.connection.execute("SELECT changes()").fetchone()[0]
        return {"results": rows, "meta": {"changes": changes}}


class FakeD1:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def prepare(self, sql: str) -> FakeStatement:
        return FakeStatement(self, sql)

    async def batch(self, statements: list[FakeStatement]) -> list[dict[str, object]]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results: list[dict[str, object]] = []
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                changes = self.connection.execute("SELECT changes()").fetchone()[0]
                results.append({"results": rows, "meta": {"changes": changes}})
            self.connection.execute("COMMIT")
            return results
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.connection.close()
