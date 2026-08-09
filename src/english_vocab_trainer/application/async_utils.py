from collections.abc import Awaitable
from typing import Any, cast, overload

from english_vocab_trainer.ports.repositories import (
    AnyVocabularyRepository,
    AsyncVocabularyRepository,
)


@overload
async def resolve[T](value: Awaitable[T]) -> T: ...


@overload
async def resolve[T](value: T) -> T: ...


async def resolve(value: object) -> object:
    if isinstance(value, Awaitable):
        return await value
    return value


class _AsyncRepositoryFacade:
    def __init__(self, repository: AnyVocabularyRepository) -> None:
        self.repository = repository

    def __getattr__(self, name: str) -> Any:
        async def call(*args: object, **kwargs: object) -> object:
            method = getattr(self.repository, name)
            return await resolve(method(*args, **kwargs))

        return call


def as_async_repository(repository: AnyVocabularyRepository) -> AsyncVocabularyRepository:
    return cast(AsyncVocabularyRepository, _AsyncRepositoryFacade(repository))
