from __future__ import annotations

from dataclasses import dataclass

from english_vocab_trainer.ports.audio import AudioResult, AudioStore
from english_vocab_trainer.ports.repositories import RepositoryProvider, VocabularyRepository


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppContainer:
    repositories: RepositoryProvider
    audio: AudioStore
    environment: str
    local_user_id: str | None = None


class UnavailableRepositoryProvider:
    def for_user(self, user_id: str) -> VocabularyRepository:
        raise ConfigurationError("repository is not configured")


class UnavailableAudioStore:
    def get(self, key: str, range_header: str | None = None) -> AudioResult:
        raise ConfigurationError("audio store is not configured")
