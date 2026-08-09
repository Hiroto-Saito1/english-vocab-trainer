from pathlib import Path

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from english_vocab_trainer.adapters.local.audio import FilesystemAudioStore
from english_vocab_trainer.adapters.local.auth import SQLiteLoginAttemptLimiter
from english_vocab_trainer.adapters.local.provider import SQLiteRepositoryProvider
from english_vocab_trainer.web.app import create_app
from english_vocab_trainer.web.auth import AuthService
from english_vocab_trainer.web.container import AppContainer


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
                SQLiteRepositoryProvider(database),
                FilesystemAudioStore(tmp_path),
                "production",
                auth=auth,
            )
        )
    )


def test_login_protects_api_and_enforces_csrf_logout(tmp_path: Path) -> None:
    with client(tmp_path) as app:
        unauthenticated = app.get("/api/v1/progress")
        assert (
            unauthenticated.status_code == 401
            and unauthenticated.headers["cache-control"] == "no-store"
        )
        bad = app.post("/login", data={"password": "wrong"})
        assert (
            bad.status_code == 401
            and "Sign in failed" in bad.text
            and "test-password" not in bad.text
        )
        login = app.post("/login", data={"password": "test-password"}, follow_redirects=False)
        assert login.status_code == 303
        assert "vocab-session" in login.headers["set-cookie"]
        assert app.get("/api/v1/progress").status_code == 200
        assert app.post("/auth/logout").status_code == 403
        csrf = app.cookies.get("vocab-csrf")
        assert csrf is not None
        logout = app.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert logout.status_code == 204 and logout.headers["cache-control"] == "no-store"
        assert app.get("/api/v1/progress").status_code == 401


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
        for session in ("v1.bad*.x", "v1.e30.e30", "v1.W10.e30", "v1.bnVsbA.e30"):
            response = app.get("/api/v1/progress", headers={"Cookie": f"vocab-session={session}"})
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
                SQLiteRepositoryProvider(database),
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
