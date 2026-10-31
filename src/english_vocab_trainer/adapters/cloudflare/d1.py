from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from fsrs import Card

from english_vocab_trainer.domain.models import (
    Review,
    ReviewAction,
    Word,
    apply_review,
    replay_card,
)
from english_vocab_trainer.ports.vocabulary import MissingWordError, ReviewConflictError


def _native(value: Any) -> Any:
    return value.to_py() if hasattr(value, "to_py") else value


JsonObject = dict[str, Any]


def _word(row: JsonObject) -> Word:
    return Word(
        str(row["id"]),
        str(row["term"]),
        row["level"],
        str(row["definition"]),
        str(row["audio_key"]),
    )


def _review(row: JsonObject) -> Review:
    return Review(
        UUID(str(row["id"])),
        str(row["word_id"]),
        ReviewAction(row["action"]),
        datetime.fromisoformat(row["reviewed_at"]),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _changes(value: Any) -> int:
    value = _native(value)
    meta = _native(value.get("meta", {})) if isinstance(value, dict) else _native(value.meta)
    return int(meta.get("changes", 0) if isinstance(meta, dict) else meta.changes)


class D1VocabularyRepository:
    """D1 FFI and optimistic, transactional review updates."""

    def __init__(self, db: object) -> None:
        self.db: Any = db

    def _prepare(self, sql: str, *params: object) -> Any:
        return self.db.prepare(sql).bind(*params)

    async def _first(self, sql: str, *params: object) -> JsonObject | None:
        result = await self._prepare(sql, *params).first()
        native = _native(result)
        return cast(JsonObject | None, native)

    async def _all(self, sql: str, *params: object) -> list[JsonObject]:
        result = _native(await self._prepare(sql, *params).run())
        rows = result.get("results", []) if isinstance(result, dict) else result.results
        return [cast(JsonObject, _native(row)) for row in _native(rows)]

    async def get_word(self, word_id: str) -> Word | None:
        row = await self._first("SELECT * FROM words WHERE id=?", word_id)
        return _word(row) if row else None

    async def next_word(self, now: datetime) -> Word | None:
        row = await self._first(
            "SELECT w.* FROM word_state s JOIN words w ON w.id=s.word_id "
            "WHERE s.due_at<=? ORDER BY s.due_at,w.id LIMIT 1",
            _iso(now),
        )
        if not row:
            row = await self._first(
                "SELECT w.* FROM words w WHERE NOT EXISTS(SELECT 1 FROM reviews r "
                "WHERE r.word_id=w.id) ORDER BY w.level IS NULL,w.level,w.id LIMIT 1"
            )
        return _word(row) if row else None

    async def next_due(self, now: datetime) -> datetime | None:
        row = await self._first(
            "SELECT MIN(due_at) AS due_at FROM word_state WHERE due_at>?", _iso(now)
        )
        return datetime.fromisoformat(row["due_at"]) if row and row["due_at"] else None

    async def history_page(
        self, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[Review, Word]]:
        where, args = "", []
        if cursor:
            where = "WHERE (r.reviewed_at<? OR (r.reviewed_at=? AND r.id<?)) "
            args = [_iso(cursor[0]), _iso(cursor[0]), str(cursor[1])]
        rows = await self._all(
            "SELECT r.id AS review_id,r.word_id,r.action,r.reviewed_at,w.* "
            "FROM reviews r JOIN words w ON w.id=r.word_id "
            + where
            + "ORDER BY r.reviewed_at DESC,r.id DESC LIMIT ?",
            *args,
            limit,
        )
        return [
            (
                Review(
                    UUID(str(r["review_id"])),
                    str(r["word_id"]),
                    ReviewAction(r["action"]),
                    datetime.fromisoformat(r["reviewed_at"]),
                ),
                _word(r),
            )
            for r in rows
        ]

    async def save_review(self, review: Review) -> None:
        for _ in range(3):
            old = await self._first("SELECT * FROM reviews WHERE id=?", str(review.id))
            if old and old["word_id"] != review.word_id:
                raise ReviewConflictError("review UUID word conflict")
            if old and old["action"] == review.action.value:
                return
            if not old and not await self.get_word(review.word_id):
                raise MissingWordError("word not found")
            state = await self._first("SELECT * FROM word_state WHERE word_id=?", review.word_id)
            latest = await self._first(
                "SELECT reviewed_at,id FROM reviews WHERE word_id=? "
                "ORDER BY reviewed_at DESC,id DESC LIMIT 1",
                review.word_id,
            )
            if old or (
                latest
                and (_iso(review.reviewed_at), str(review.id))
                < (latest["reviewed_at"], latest["id"])
            ):
                rows = await self._all(
                    "SELECT * FROM reviews WHERE word_id=? ORDER BY reviewed_at,id", review.word_id
                )
                reviews = [_review(row) for row in rows]
                if old:
                    reviews = [
                        Review(
                            r.id,
                            r.word_id,
                            review.action if r.id == review.id else r.action,
                            r.reviewed_at,
                        )
                        for r in reviews
                    ]
                else:
                    reviews.append(review)
                card = replay_card(review.word_id, reviews)
            else:
                card = apply_review(
                    Card.from_json(state["card_json"])
                    if state
                    else replay_card(review.word_id, []),
                    review,
                )
            version = int(state["version"]) if state else 0
            mutation = str(uuid4())
            guard = (
                "EXISTS(SELECT 1 FROM reviews WHERE id=? AND word_id=? AND action=?)"
                if old
                else "NOT EXISTS(SELECT 1 FROM reviews WHERE id=?)"
            )
            guard_args = (
                (str(review.id), review.word_id, old["action"]) if old else (str(review.id),)
            )
            # The same guard is needed for both insert and update paths of the upsert.
            state_sql = (
                "INSERT INTO word_state(word_id,due_at,card_json,version,mutation_id) "
                "SELECT ?,?,?,?,? WHERE "
                + guard
                + " AND (?=0 OR EXISTS(SELECT 1 FROM word_state WHERE word_id=? AND version=?)) "
                "ON CONFLICT(word_id) DO UPDATE SET due_at=excluded.due_at, "
                "card_json=excluded.card_json,version=excluded.version, "
                "mutation_id=excluded.mutation_id WHERE word_state.version=?"
            )
            state_stmt = self._prepare(
                state_sql,
                review.word_id,
                _iso(card.due),
                card.to_json(),
                version + 1,
                mutation,
                *guard_args,
                version,
                review.word_id,
                version,
                version,
            )
            if old:
                event_stmt = self._prepare(
                    "UPDATE reviews SET action=? WHERE id=? AND word_id=? AND action=? "
                    "AND EXISTS(SELECT 1 FROM word_state WHERE word_id=? AND mutation_id=?)",
                    review.action.value,
                    str(review.id),
                    review.word_id,
                    old["action"],
                    review.word_id,
                    mutation,
                )
            else:
                event_stmt = self._prepare(
                    "INSERT OR IGNORE INTO reviews(id,word_id,action,reviewed_at) "
                    "SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM word_state "
                    "WHERE word_id=? AND mutation_id=?)",
                    str(review.id),
                    review.word_id,
                    review.action.value,
                    _iso(review.reviewed_at),
                    review.word_id,
                    mutation,
                )
            results = _native(await self.db.batch([state_stmt, event_stmt]))
            if len(results) == 2 and all(_changes(result) == 1 for result in results):
                return
        raise ReviewConflictError("concurrent review update")
