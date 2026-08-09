from collections.abc import Awaitable
from typing import overload


@overload
async def resolve[T](value: Awaitable[T]) -> T: ...


@overload
async def resolve[T](value: T) -> T: ...


async def resolve(value: object) -> object:
    if isinstance(value, Awaitable):
        return await value
    return value
