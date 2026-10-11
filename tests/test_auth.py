from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.web.app import container_from_env
from english_vocab_trainer.web.auth import AuthenticationError, AuthService
from english_vocab_trainer.web.container import ConfigurationError


class Limiter:
    def __init__(self) -> None:
        self.allowed = True
        self.cleared = False

    def reserve(self, _: datetime) -> bool:
        return self.allowed

    def clear(self) -> None:
        self.cleared = True


def test_session_is_signed_expiring_and_csrf_bound() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    limiter = Limiter()
    service = AuthService(
        PasswordHasher().hash("correct horse battery staple"),
        b"x" * 32,
        limiter,
        secure_cookies=False,
        clock=lambda: now,
    )
    cookies = service.authenticate("correct horse battery staple")
    assert cookies is not None and limiter.cleared
    assert cookies.session.count(".") == 1
    service.validate_session(cookies.session)
    service.validate_session(cookies.session, cookies.csrf, require_csrf=True)
    with pytest.raises(AuthenticationError):
        service.validate_session(cookies.session + "x")
    with pytest.raises(AuthenticationError):
        service.validate_session(cookies.session, "wrong", require_csrf=True)
    service.clock = lambda: now + timedelta(days=31)
    with pytest.raises(AuthenticationError):
        service.validate_session(cookies.session)


def test_login_failure_or_limiter_never_issues_a_cookie() -> None:
    limiter = Limiter()
    service = AuthService(PasswordHasher().hash("password"), b"x" * 32, limiter)
    assert service.authenticate("wrong") is None
    limiter.allowed = False
    assert service.authenticate("password") is None


def test_auth_rejects_weak_config_and_malformed_csrf() -> None:
    limiter = Limiter()
    with pytest.raises(ValueError):
        AuthService(PasswordHasher(type=Type.I).hash("password"), b"x" * 32, limiter)
    with pytest.raises(ValueError):
        AuthService(PasswordHasher().hash("password"), b"x" * 31, limiter)
    service = AuthService(PasswordHasher().hash("password"), b"x" * 32, limiter)
    cookies = service.authenticate("password")
    assert cookies is not None
    with pytest.raises(AuthenticationError):
        service.validate_session(cookies.session, "日本語", require_csrf=True)


@pytest.mark.parametrize(
    "session",
    [
        "日本語.x",
        ".x",
        "bad*.x",
        "e30.e30",  # a signed-looking empty JSON object
        "W10.e30",  # a JSON list rather than a session object
        "bnVsbA.e30",  # JSON null
        "eyJpYXQiOnRydWV9.e30",  # bool is not an integer timestamp
    ],
)
def test_malformed_session_strings_always_raise_authentication_error(session: str) -> None:
    service = AuthService(PasswordHasher().hash("password"), b"x" * 32, Limiter())
    with pytest.raises(AuthenticationError):
        service.validate_session(session)


def test_sqlite_limiter_and_production_configuration(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    limiter = SQLiteLoginAttemptLimiter(tmp_path / "v.db")
    assert [limiter.reserve(now) for _ in range(5)] == [True] * 5
    assert not limiter.reserve(now)
    limiter.clear()
    assert limiter.reserve(now + timedelta(minutes=16))
    password_hash = PasswordHasher().hash("password")
    container = container_from_env(
        {
            "APP_ENV": "production",
            "VOCAB_DB_PATH": str(tmp_path / "production.db"),
            "AUDIO_BACKEND": "filesystem",
            "AUDIO_ROOT": str(tmp_path),
            "APP_PASSWORD_HASH": password_hash,
            "SESSION_SIGNING_SECRET": "c" * 43,
            "ALLOWED_HOSTS": "vocab.example.test",
        }
    )
    assert container.auth is not None and container.database == tmp_path / "production.db"
    non_bypass = container_from_env(
        {
            "APP_ENV": "staging-like-value",
            "VOCAB_DB_PATH": str(tmp_path / "non-bypass.db"),
            "AUDIO_BACKEND": "filesystem",
            "AUDIO_ROOT": str(tmp_path),
            "APP_PASSWORD_HASH": password_hash,
            "SESSION_SIGNING_SECRET": "c" * 43,
            "ALLOWED_HOSTS": "vocab.example.test",
        }
    )
    assert non_bypass.environment == "production" and non_bypass.auth is not None
    with pytest.raises(ConfigurationError):
        container_from_env({"APP_ENV": "production"})
