import base64
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from english_vocab_trainer import worker


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def assertion(claims: dict[str, Any], header: dict[str, Any] | None = None) -> str:
    return ".".join(
        [
            b64(json.dumps(header or {"alg": "RS256", "kid": "key"}).encode()),
            b64(json.dumps(claims).encode()),
            b64(b"signature"),
        ]
    )


ENV = SimpleNamespace(
    ACCESS_TEAM_DOMAIN="team.cloudflareaccess.com",
    ACCESS_AUD="aud",
    OWNER_EMAIL="owner@example.com",
    APP_ORIGIN="https://vocab.example.com",
    HEALTHCHECK_SERVICE_TOKEN_ID="probe.access",
)
CLAIMS = {
    "iss": "https://team.cloudflareaccess.com",
    "aud": ["aud"],
    "exp": 2000,
    "nbf": 900,
    "email": "owner@example.com",
}


@pytest.mark.asyncio
async def test_assertion_validation_and_key_rotation() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> list[dict[str, Any]]:
        calls.append(url)
        return [{"kid": "old" if len(calls) == 1 else "key", "kty": "RSA"}]

    async def verify(key: dict[str, Any], signature: bytes, signed: bytes) -> bool:
        return signature == b"signature" and key["kid"] == "key"

    validator = worker.AccessValidator(fetch, verify, lambda: 1000)
    with pytest.raises(worker.AccessError):
        await validator.validate(assertion(CLAIMS), ENV, "GET", "/")
    await validator.validate(assertion(CLAIMS), ENV, "GET", "/")
    assert len(calls) == 2
    for changes in (
        {"exp": 1000},
        {"nbf": 1001},
        {"aud": ["wrong"]},
        {"iss": "https://evil.example"},
        {"email": "other@example.com"},
    ):
        with pytest.raises(worker.AccessError):
            await validator.validate(assertion(CLAIMS | changes), ENV, "GET", "/")
    with pytest.raises(worker.AccessError):
        await validator.validate(assertion(CLAIMS, {"alg": "none", "kid": "key"}), ENV, "GET", "/")
    probe = CLAIMS | {"email": None, "common_name": "probe.access"}
    await validator.validate(assertion(probe), ENV, "GET", "/healthz")
    with pytest.raises(worker.AccessError):
        await validator.validate(assertion(probe), ENV, "GET", "/api/v1/session")


def test_session_and_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(*args: object) -> None:
        return None

    monkeypatch.setattr(worker.validator, "validate", allowed)

    class EnvMiddleware:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            scope["env"] = ENV
            await self.app(scope, receive, send)

    client = TestClient(cast(Any, EnvMiddleware(worker.app)), base_url="https://vocab.example.com")
    response = client.get("/api/v1/session")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/v1/session").json()["csrf_token"] == token
    assert client.post("/anything").status_code == 403
    assert (
        client.post(
            "/anything", headers={"Origin": ENV.APP_ORIGIN, "X-CSRF-Token": token}
        ).status_code
        != 403
    )
    assert (
        client.post(
            "/anything", headers={"Origin": "https://evil.example", "X-CSRF-Token": token}
        ).status_code
        == 403
    )


def test_static_assets_are_fallback_and_api_paths_do_not_fall_through() -> None:
    from fastapi import FastAPI

    class Asset:
        status = 200
        headers = {"content-type": "text/html"}

        async def bytes(self) -> bytes:
            return b"asset"

    class Assets:
        async def fetch(self, url: str) -> Asset:
            assert url == "https://assets.local/"
            return Asset()

    class EnvMiddleware:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            scope["env"] = SimpleNamespace(ASSETS=Assets())
            await self.app(scope, receive, send)

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    worker.install_asset_fallback(application)
    client = TestClient(cast(Any, EnvMiddleware(application)))
    assert client.get("/").content == b"asset"
    assert client.get("/api/v1/missing").status_code == 404
    assert client.get("/docs").status_code == 404


@pytest.mark.asyncio
async def test_readiness_requires_complete_catalog() -> None:
    class Statement:
        def __init__(self, row: dict[str, object] | None) -> None:
            self.row = row

        async def first(self) -> dict[str, object] | None:
            return self.row

    class Database:
        def __init__(self, row: dict[str, object] | None) -> None:
            self.row = row

        def prepare(self, sql: str) -> Statement:
            assert "schema_metadata" in sql
            return Statement(self.row)

    def request(row: dict[str, object] | None) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/readyz",
                "headers": [],
                "env": SimpleNamespace(DB=Database(row)),
            }
        )

    ready = await worker.readiness(
        request(
            {
                "version": 1,
                "catalog_sha256": "a" * 64,
                "catalog_count": 2000,
                "words": 2000,
            }
        )
    )
    assert isinstance(ready, JSONResponse) and ready.status_code == 200
    unavailable = await worker.readiness(
        request(
            {
                "version": 1,
                "catalog_sha256": "invalid",
                "catalog_count": 2000,
                "words": 2000,
            }
        )
    )
    assert unavailable.status_code == 503
