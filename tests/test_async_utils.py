import pytest

from english_vocab_trainer.application.async_utils import resolve


@pytest.mark.asyncio
async def test_resolve_sync_value() -> None:
    assert await resolve(3) == 3


@pytest.mark.asyncio
async def test_resolve_awaitable() -> None:
    async def value() -> str:
        return "ok"

    assert await resolve(value()) == "ok"
