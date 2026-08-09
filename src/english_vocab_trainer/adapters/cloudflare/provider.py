from english_vocab_trainer.adapters.cloudflare.d1 import D1VocabularyRepository


class D1RepositoryProvider:
    def __init__(self, db: object) -> None:
        self.db = db

    def for_user(self, user_id: str) -> D1VocabularyRepository:
        return D1VocabularyRepository(self.db, user_id)
