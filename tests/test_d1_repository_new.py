import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fakes.d1 import FakeD1

from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository
from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.domain.models import Review, ReviewAction, Word
from english_vocab_trainer.ports.vocabulary import ReviewConflictError


def test_d1_parity_incremental_correction_and_idempotence(tmp_path: Path) -> None:
    async def run() -> None:
        local = SQLiteVocabularyRepository(tmp_path / "local.db")
        remote = SQLiteVocabularyRepository(tmp_path / "remote.db")
        word = Word("w", "word", 1, "definition", "audio")
        for db in (local, remote):
            db.bulk_upsert_words([word])
        d1 = D1VocabularyRepository(FakeD1(remote.db))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        reviews = [
            Review(
                UUID(int=i + 1),
                "w",
                ReviewAction.KNOWN if i % 2 else ReviewAction.UNKNOWN,
                now + timedelta(minutes=i),
            )
            for i in range(20)
        ]
        for review in reviews:
            local.save_review(review)
            await d1.save_review(review)
        assert (
            local.db.execute("SELECT due_at,card_json,version FROM word_state").fetchone()[:]
            == remote.db.execute("SELECT due_at,card_json,version FROM word_state").fetchone()[:]
        )
        old = reviews[3]
        correction = Review(old.id, old.word_id, ReviewAction.UNKNOWN, now + timedelta(days=1))
        local.save_review(correction)
        await d1.save_review(correction)
        await d1.save_review(correction)
        assert (
            local.db.execute("SELECT due_at,card_json,version FROM word_state").fetchone()[:]
            == remote.db.execute("SELECT due_at,card_json,version FROM word_state").fetchone()[:]
        )
        assert len(await d1.history_page(None, 10)) == 10
        with pytest.raises(ReviewConflictError):
            await d1.save_review(Review(old.id, "another", ReviewAction.KNOWN, now))
        local.close()
        remote.close()

    asyncio.run(run())


def test_retry_and_incremental_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        import english_vocab_trainer.adapters.cloudflare.d1 as module

        local = SQLiteVocabularyRepository(tmp_path / "retry.db")
        local.bulk_upsert_words([Word("w", "term", None, "definition", "key")])

        class RetryingD1(FakeD1):
            failures = 2

            async def batch(self, statements: list[Any]) -> list[dict[str, object]]:
                if self.failures:
                    self.failures -= 1
                    return [{"meta": {"changes": 0}}, {"meta": {"changes": 0}}]
                return await super().batch(statements)

        fake = RetryingD1(local.db)
        repo = D1VocabularyRepository(fake)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        await repo.save_review(Review(UUID(int=1), "w", ReviewAction.KNOWN, now))
        monkeypatch.setattr(
            module, "replay_card", lambda *_: (_ for _ in ()).throw(AssertionError("replayed"))
        )
        await repo.save_review(
            Review(UUID(int=2), "w", ReviewAction.KNOWN, now + timedelta(minutes=1))
        )
        fake.failures = 3
        with pytest.raises(ReviewConflictError):
            await repo.save_review(
                Review(UUID(int=3), "w", ReviewAction.KNOWN, now + timedelta(minutes=2))
            )
        assert local.db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 2
        local.close()

    asyncio.run(run())


def test_replay_500_correction(tmp_path: Path) -> None:
    async def run() -> None:
        local = SQLiteVocabularyRepository(tmp_path / "long.db")
        local.bulk_upsert_words([Word("w", "term", 1, "definition", "key")])
        repo = D1VocabularyRepository(FakeD1(local.db))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(500):
            await repo.save_review(
                Review(UUID(int=i + 1), "w", ReviewAction.KNOWN, now + timedelta(minutes=i))
            )
        await repo.save_review(
            Review(UUID(int=1), "w", ReviewAction.UNKNOWN, now + timedelta(days=1))
        )
        assert local.db.execute("SELECT version FROM word_state").fetchone()[0] == 501
        assert (
            local.db.execute(
                "SELECT reviewed_at FROM reviews WHERE id=?", (str(UUID(int=1)),)
            ).fetchone()[0]
            == now.isoformat()
        )
        local.close()

    asyncio.run(run())


def test_schema_and_history_cursor(tmp_path: Path) -> None:
    async def run() -> None:
        local = SQLiteVocabularyRepository(tmp_path / "schema.db")
        tables = {
            row[0] for row in local.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == {"schema_metadata", "words", "reviews", "word_state"}
        local.bulk_upsert_words([Word("w", "term", None, "definition", "key")])
        repo = D1VocabularyRepository(FakeD1(local.db))
        now = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(5):
            await repo.save_review(
                Review(UUID(int=i + 1), "w", ReviewAction.KNOWN, now + timedelta(minutes=i))
            )
        first = await repo.history_page(None, 2)
        second = await repo.history_page((first[-1][0].reviewed_at, first[-1][0].id), 2)
        assert [item[0].id.int for item in first + second] == [5, 4, 3, 2]
        local.close()

    asyncio.run(run())
