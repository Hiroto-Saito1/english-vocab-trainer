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


def _to_python(value: Any) -> Any:
    return value.to_py() if hasattr(value, "to_py") else value


D1Row = dict[str, Any]


def _word_from_row(row: D1Row) -> Word:
    return Word(
        str(row["id"]),
        str(row["term"]),
        row["level"],
        str(row["definition"]),
        str(row["audio_key"]),
    )


def _review_from_row(row: D1Row) -> Review:
    return Review(
        UUID(str(row["id"])),
        str(row["word_id"]),
        ReviewAction(row["action"]),
        datetime.fromisoformat(row["reviewed_at"]),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _changed_rows(value: Any) -> int:
    value = _to_python(value)
    meta = _to_python(value.get("meta", {})) if isinstance(value, dict) else _to_python(value.meta)
    return int(meta.get("changes", 0) if isinstance(meta, dict) else meta.changes)


class D1VocabularyRepository:
    """D1 FFI and optimistic, transactional review updates."""

    def __init__(self, db: object) -> None:
        self.db: Any = db

    def _prepare(self, sql: str, *params: object) -> Any:
        return self.db.prepare(sql).bind(*params)

    async def _first(self, sql: str, *params: object) -> D1Row | None:
        result = await self._prepare(sql, *params).first()
        row = _to_python(result)
        return cast(D1Row | None, row)

    async def _all(self, sql: str, *params: object) -> list[D1Row]:
        result = _to_python(await self._prepare(sql, *params).run())
        rows = result.get("results", []) if isinstance(result, dict) else result.results
        return [cast(D1Row, _to_python(row)) for row in _to_python(rows)]

    async def get_word(self, word_id: str) -> Word | None:
        row = await self._first("SELECT * FROM words WHERE id=?", word_id)
        return _word_from_row(row) if row else None

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
        return _word_from_row(row) if row else None

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
            cursor_reviewed_at = _iso(cursor[0])
            args = [cursor_reviewed_at, cursor_reviewed_at, str(cursor[1])]
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
                _word_from_row(r),
            )
            for r in rows
        ]

    async def save_review(self, review: Review) -> None:
        for _ in range(3):
            existing_review = await self._first("SELECT * FROM reviews WHERE id=?", str(review.id))
            if existing_review and existing_review["word_id"] != review.word_id:
                raise ReviewConflictError("review UUID word conflict")
            if existing_review and existing_review["action"] == review.action.value:
                return
            if not existing_review and not await self.get_word(review.word_id):
                raise MissingWordError("word not found")
            stored_state = await self._first(
                "SELECT * FROM word_state WHERE word_id=?", review.word_id
            )
            latest_review = await self._first(
                "SELECT reviewed_at,id FROM reviews WHERE word_id=? "
                "ORDER BY reviewed_at DESC,id DESC LIMIT 1",
                review.word_id,
            )
            is_out_of_order = bool(
                latest_review
                and (_iso(review.reviewed_at), str(review.id))
                < (latest_review["reviewed_at"], latest_review["id"])
            )
            if existing_review or is_out_of_order:
                rows = await self._all(
                    "SELECT * FROM reviews WHERE word_id=? ORDER BY reviewed_at,id", review.word_id
                )
                reviews = [_review_from_row(row) for row in rows]
                if existing_review:
                    reviews = [
                        Review(
                            historical_review.id,
                            historical_review.word_id,
                            review.action
                            if historical_review.id == review.id
                            else historical_review.action,
                            historical_review.reviewed_at,
                        )
                        for historical_review in reviews
                    ]
                else:
                    reviews.append(review)
                next_card = replay_card(review.word_id, reviews)
            else:
                next_card = apply_review(
                    Card.from_json(stored_state["card_json"])
                    if stored_state
                    else replay_card(review.word_id, []),
                    review,
                )
            current_version = int(stored_state["version"]) if stored_state else 0
            mutation_id = str(uuid4())
            review_guard = (
                "EXISTS(SELECT 1 FROM reviews WHERE id=? AND word_id=? AND action=?)"
                if existing_review
                else "NOT EXISTS(SELECT 1 FROM reviews WHERE id=?)"
            )
            review_guard_params = (
                (str(review.id), review.word_id, existing_review["action"])
                if existing_review
                else (str(review.id),)
            )
            # The same guard is needed for both insert and update paths of the upsert.
            state_sql = (
                "INSERT INTO word_state(word_id,due_at,card_json,version,mutation_id) "
                "SELECT ?,?,?,?,? WHERE "
                + review_guard
                + " AND (?=0 OR EXISTS(SELECT 1 FROM word_state WHERE word_id=? AND version=?)) "
                "ON CONFLICT(word_id) DO UPDATE SET due_at=excluded.due_at, "
                "card_json=excluded.card_json,version=excluded.version, "
                "mutation_id=excluded.mutation_id WHERE word_state.version=?"
            )
            state_stmt = self._prepare(
                state_sql,
                review.word_id,
                _iso(next_card.due),
                next_card.to_json(),
                current_version + 1,
                mutation_id,
                *review_guard_params,
                current_version,
                review.word_id,
                current_version,
                current_version,
            )
            if existing_review:
                event_stmt = self._prepare(
                    "UPDATE reviews SET action=? WHERE id=? AND word_id=? AND action=? "
                    "AND EXISTS(SELECT 1 FROM word_state WHERE word_id=? AND mutation_id=?)",
                    review.action.value,
                    str(review.id),
                    review.word_id,
                    existing_review["action"],
                    review.word_id,
                    mutation_id,
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
                    mutation_id,
                )
            results = _to_python(await self.db.batch([state_stmt, event_stmt]))
            if len(results) == 2 and all(_changed_rows(result) == 1 for result in results):
                return
        raise ReviewConflictError("concurrent review update")
