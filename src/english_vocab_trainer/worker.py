"""Cloudflare Worker entry point. Every request is authenticated before routing."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from english_vocab_trainer.web.routes.audio import router as audio_router
from english_vocab_trainer.web.routes.study import router as study_router


class AccessError(ValueError):
    """Invalid Access assertion or unavailable verifier."""


def _decode(value: str) -> bytes:
    if not value or len(value) > 16384 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AccessError("invalid base64url")
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AccessError("invalid base64url") from exc
    if base64.urlsafe_b64encode(result).rstrip(b"=").decode() != value:
        raise AccessError("noncanonical base64url")
    return result


def _json(value: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise AccessError("duplicate JSON member")
            result[key] = item
        return result

    try:
        obj = json.loads(
            value,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(AccessError("invalid JSON")),
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise AccessError("invalid JSON") from exc
    if not isinstance(obj, dict):
        raise AccessError("invalid JSON object")
    return obj


def _config(env: Any, name: str) -> str:
    value = getattr(env, name, None)
    if not isinstance(value, str) or not value or value != value.strip():
        raise AccessError("missing configuration")
    return value


def _issuer(env: Any) -> str:
    domain = _config(env, "ACCESS_TEAM_DOMAIN")
    if domain.startswith("https://"):
        domain = domain[8:]
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.cloudflareaccess\.com", domain):
        raise AccessError("invalid Access domain")
    return "https://" + domain.lower()


async def _fetch_jwks(url: str) -> list[dict[str, Any]]:
    from js import fetch

    response = await fetch(url)
    if not response.ok:
        raise AccessError("JWKS unavailable")
    body = _json((await response.text()).encode())
    keys = body.get("keys")
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        raise AccessError("invalid JWKS")
    return keys


async def _verify_rsa(key: dict[str, Any], signature: bytes, signed: bytes) -> bool:
    from js import Object, Uint8Array, crypto
    from pyodide.ffi import to_js

    def convert(obj: object) -> Any:
        return to_js(obj, dict_converter=Object.fromEntries)

    imported = await crypto.subtle.importKey(
        "jwk",
        convert(key),
        convert({"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}),
        False,
        to_js(["verify"]),
    )
    return bool(
        await crypto.subtle.verify(
            "RSASSA-PKCS1-v1_5",
            imported,
            Uint8Array.new(to_js(list(signature))),
            Uint8Array.new(to_js(list(signed))),
        )
    )


class AccessValidator:
    def __init__(
        self,
        fetch_jwks: Callable[[str], Awaitable[list[dict[str, Any]]]] = _fetch_jwks,
        verify: Callable[[dict[str, Any], bytes, bytes], Awaitable[bool]] = _verify_rsa,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.fetch_jwks, self.verify, self.clock = fetch_jwks, verify, clock
        self.cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def validate(self, token: str | None, env: Any, method: str, path: str) -> None:
        issuer = _issuer(env)
        audience = _config(env, "ACCESS_AUD")
        owner = _config(env, "OWNER_EMAIL")
        if not token or len(token) > 24000:
            raise AccessError("missing assertion")
        parts = token.split(".")
        if len(parts) != 3:
            raise AccessError("invalid assertion")
        header, claims = _json(_decode(parts[0])), _json(_decode(parts[1]))
        signature = _decode(parts[2])
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not kid
            or header.get("crit") is not None
        ):
            raise AccessError("invalid assertion header")
        now = self.clock()
        aud = claims.get("aud")
        if (
            claims.get("iss") != issuer
            or not (
                aud == audience
                or isinstance(aud, list)
                and all(isinstance(a, str) for a in aud)
                and audience in aud
            )
            or type(claims.get("exp")) not in (int, float)
            or not math.isfinite(claims["exp"])
            or claims["exp"] <= now
            or (
                "nbf" in claims
                and (
                    type(claims["nbf"]) not in (int, float)
                    or not math.isfinite(claims["nbf"])
                    or claims["nbf"] > now
                )
            )
        ):
            raise AccessError("invalid assertion claims")
        service = getattr(env, "HEALTHCHECK_SERVICE_TOKEN_ID", None)
        is_probe = (
            method in {"GET", "HEAD"}
            and path in {"/healthz", "/readyz"}
            and isinstance(service, str)
            and bool(service)
            and claims.get("common_name") == service
        )
        if ("common_name" in claims and not is_probe) or (
            claims.get("email") != owner and not is_probe
        ):
            raise AccessError("identity not allowed")
        cached = self.cache.get(issuer)
        if cached is None or cached[0] <= now:
            keys = await self.fetch_jwks(issuer + "/cdn-cgi/access/certs")
            self.cache[issuer] = (now + 300, keys)
        else:
            keys = cached[1]
        matches = [key for key in keys if key.get("kid") == kid]
        if not matches and cached is not None and cached[0] > now:
            keys = await self.fetch_jwks(issuer + "/cdn-cgi/access/certs")
            self.cache[issuer] = (now + 300, keys)
            matches = [key for key in keys if key.get("kid") == kid]
        if (
            len(matches) != 1
            or matches[0].get("kty") != "RSA"
            or matches[0].get("alg", "RS256") != "RS256"
            or matches[0].get("use", "sig") != "sig"
            or not await self.verify(
                matches[0], signature, (parts[0] + "." + parts[1]).encode("ascii")
            )
        ):
            raise AccessError("invalid assertion signature")


validator = AccessValidator()
app = FastAPI(title="English Vocab Trainer", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(study_router)
app.include_router(audio_router)


@app.middleware("http")
async def access_and_csrf(request: Request, call_next: Any) -> Response:
    env = request.scope.get("env")
    try:
        if env is None:
            raise AccessError("missing environment")
        await validator.validate(
            request.headers.get("Cf-Access-Jwt-Assertion"), env, request.method, request.url.path
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = _config(env, "APP_ORIGIN")
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise AccessError("invalid origin configuration")
            token = request.headers.get("X-CSRF-Token", "")
            cookie = request.cookies.get("__Host-vocab-csrf", "")
            if (
                request.headers.get("Origin") != origin
                or not token
                or not cookie
                or not hmac.compare_digest(token.encode(), cookie.encode())
            ):
                return JSONResponse({"detail": "csrf validation failed"}, status_code=403)
    except Exception:
        return JSONResponse(
            {"detail": "authentication required"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; img-src 'self'; "
        "media-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    return response


@app.get("/api/v1/session")
async def session(request: Request) -> Response:
    csrf = request.cookies.get("__Host-vocab-csrf", "")
    try:
        valid = len(_decode(csrf)) == 32
    except AccessError:
        valid = False
    if not valid:
        csrf = secrets.token_urlsafe(32)
    response = JSONResponse({"csrf_token": csrf}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        "__Host-vocab-csrf", csrf, secure=True, httponly=True, samesite="strict", path="/"
    )
    return response


@app.get("/healthz")
@app.head("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
@app.head("/readyz")
async def readiness(request: Request) -> Response:
    """Require the current schema and a fully published catalog."""
    try:
        row = (
            await request.scope["env"]
            .DB.prepare(
                "SELECT version,catalog_sha256,catalog_count,"
                "(SELECT count(*) FROM words) AS words FROM schema_metadata WHERE singleton=1"
            )
            .first()
        )
        row = row.to_py() if hasattr(row, "to_py") else row
        if (
            not isinstance(row, dict)
            or row.get("version") != 1
            or not isinstance(row.get("catalog_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["catalog_sha256"])
            or row.get("catalog_count") != row.get("words")
            or row.get("catalog_count") != 2000
        ):
            raise ValueError("catalog is not ready")
    except Exception:
        return JSONResponse({"detail": "database is not ready"}, status_code=503)
    return JSONResponse({"status": "ready"})


# Additional API routers must be registered before this fallback.
def install_asset_fallback(application: FastAPI) -> None:
    @application.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def frontend(path: str, request: Request) -> Response:
        if path.startswith("api/") or path in {"docs", "redoc", "openapi.json"}:
            return Response(status_code=404)
        asset = await request.scope["env"].ASSETS.fetch("https://assets.local/" + path)
        body = await asset.bytes()
        return Response(content=body, status_code=asset.status, headers=asset.headers)


asgi: Any
try:
    from workers import asgi
except ImportError:  # CPython unit tests
    asgi = None

if asgi is not None:
    install_asset_fallback(app)
    Default = asgi.entrypoint(app)
