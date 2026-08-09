from pathlib import Path

from .sqlite import SQLiteVocabularyRepository


class SQLiteRepositoryProvider:
    def __init__(self, path: Path) -> None:
        self.path = path

    def for_user(self, user_id: str) -> SQLiteVocabularyRepository:
        return SQLiteVocabularyRepository(self.path, user_id)
