import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.local.database import SchemaMismatchError
from english_vocab_trainer.web.app import create_app, create_app_from_env
from english_vocab_trainer.web.auth import AuthService
from english_vocab_trainer.web.container import (
    AppContainer,
    ConfigurationError,
    trusted_hosts_from_env,
)


def client(tmp_path: Path) -> TestClient:
    database = tmp_path / "v.db"
    auth = AuthService(
        PasswordHasher().hash("test-password"),
        b"s" * 32,
        SQLiteLoginAttemptLimiter(database),
        secure_cookies=False,
    )
    return TestClient(
        create_app(
            AppContainer(
                database,
                FilesystemAudioStore(tmp_path),
                "production",
                auth=auth,
            )
        )
    )


def test_login_protects_api_and_enforces_csrf_logout(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        unauthenticated = app.get("/api/v1/card")
        assert (
            unauthenticated.status_code == 401
            and unauthenticated.headers["cache-control"] == "no-store"
        )
        assert app.post("/api/v1/reviews", json={}).status_code == 401
        bad = app.post("/login", data={"password": "wrong"})
        assert (
            bad.status_code == 401
            and "Sign in failed" in bad.text
            and "test-password" not in bad.text
        )
        login = app.post("/login", data={"password": "test-password"}, follow_redirects=False)
        assert login.status_code == 303
        assert "vocab-session" in login.headers["set-cookie"]
        assert app.get("/api/v1/card").status_code == 200
        assert app.post("/auth/logout").status_code == 403
        csrf = app.cookies.get("vocab-csrf")
        assert csrf is not None
        logout = app.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert logout.status_code == 204 and logout.headers["cache-control"] == "no-store"
        assert app.get("/api/v1/card").status_code == 401


def test_login_origin_and_global_throttle(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        assert (
            app.post(
                "/login",
                data={"password": "test-password"},
                headers={"Origin": "https://bad.example"},
            ).status_code
            == 403
        )
        for _ in range(5):
            assert app.post("/login", data={"password": "wrong"}).status_code == 401
        assert app.post("/login", data={"password": "test-password"}).status_code == 401


def test_malformed_cookie_and_csrf_never_become_server_errors(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        for session in ("bad*.x", "e30.e30", "W10.e30", "bnVsbA.e30"):
            response = app.get("/api/v1/card", headers={"Cookie": f"vocab-session={session}"})
            assert response.status_code == 401
        login = app.post("/login", data={"password": "test-password"}, follow_redirects=False)
        assert login.status_code == 303
        response = app.post("/auth/logout", headers={"X-CSRF-Token": "not-a-token*"})
        assert response.status_code == 403


def test_login_size_cap_and_production_cookie_attributes(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        assert 'maxlength="1024"' in app.get("/login").text
        assert app.post("/login", data={"password": "x" * 5_000}).status_code == 401

    database = tmp_path / "secure.db"
    auth = AuthService(
        PasswordHasher().hash("test-password"),
        b"s" * 32,
        SQLiteLoginAttemptLimiter(database),
        secure_cookies=True,
    )
    with TestClient(
        create_app(
            AppContainer(
                database,
                FilesystemAudioStore(tmp_path),
                "production",
                auth=auth,
            )
        )
    ) as app:
        response = app.post("/login", data={"password": "test-password"}, follow_redirects=False)
        assert response.status_code == 303
        session, csrf = response.headers.get_list("set-cookie")
        assert session.startswith("__Host-vocab-session=")
        assert "HttpOnly" in session and "Secure" in session and "SameSite=strict" in session
        assert "Path=/" in session and "Domain=" not in session
        assert csrf.startswith("__Host-vocab-csrf=")
        assert "HttpOnly" not in csrf and "Secure" in csrf and "SameSite=strict" in csrf
        assert "Path=/" in csrf and "Domain=" not in csrf


def test_production_host_security_headers_and_readiness_are_public(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        liveness = app.get("/healthz")
        readiness = app.get("/readyz")
        assert liveness.status_code == readiness.status_code == 200
        assert liveness.json() == {"status": "ok"} and readiness.json() == {"status": "ready"}
        assert liveness.headers["strict-transport-security"].startswith("max-age=")
        assert liveness.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in liveness.headers["content-security-policy"]
        assert "object-src 'none'" in liveness.headers["content-security-policy"]
        assert app.get("/healthz", headers={"Host": "evil.example"}).status_code == 400

    with TestClient(
        create_app(
            AppContainer(
                tmp_path / "missing" / "v.db",
                FilesystemAudioStore(tmp_path),
                "production",
                auth=AuthService(
                    PasswordHasher().hash("test-password"),
                    b"s" * 32,
                    SQLiteLoginAttemptLimiter(tmp_path / "unavailable.db"),
                    secure_cookies=False,
                ),
            )
        )
    ) as unavailable:
        assert unavailable.get("/readyz").status_code == 503


def test_startup_and_readiness_reject_an_incompatible_database_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "old.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE reviews("
            "id TEXT PRIMARY KEY,word_id TEXT,action TEXT,reviewed_at TEXT,legacy TEXT)"
        )
    app = create_app(AppContainer(database, FilesystemAudioStore(tmp_path), "test"))
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"detail": "database schema is incompatible"}
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("VOCAB_DB_PATH", str(database))
    monkeypatch.setenv("AUDIO_BACKEND", "filesystem")
    monkeypatch.setenv("AUDIO_ROOT", str(tmp_path))
    with pytest.raises(SchemaMismatchError, match="recreate"):
        create_app_from_env()


def test_trusted_host_configuration_rejects_untrusted_host_syntax() -> None:
    assert trusted_hosts_from_env({"ALLOWED_HOSTS": "vocab.example.test,*.fly.dev"}) == (
        "vocab.example.test",
        "*.fly.dev",
    )
    assert trusted_hosts_from_env({"FLY_APP_NAME": "vocab-study"}) == ("vocab-study.fly.dev",)
    for invalid in (
        "*",
        "https://bad.example",
        "bad.example:8080",
        "bad example",
        "bad..example",
        " vocab.example",
    ):
        with pytest.raises(ConfigurationError):
            trusted_hosts_from_env({"ALLOWED_HOSTS": invalid})
    for invalid_app in ("Vocab", "vocab_study", "-vocab", "vocab-", "日本語"):
        with pytest.raises(ConfigurationError):
            trusted_hosts_from_env({"FLY_APP_NAME": invalid_app})
