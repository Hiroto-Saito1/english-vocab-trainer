from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from english_vocab_trainer.ports.auth import LoginAttemptLimiter

SESSION_TTL = timedelta(days=30)
MAX_TOKEN_BYTES = 2048
MAX_PASSWORD_BYTES = 1024
Clock = Callable[[], datetime]


class AuthenticationError(ValueError):
    pass


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if (
        not value
        or len(value) > MAX_TOKEN_BYTES
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        raise AuthenticationError("invalid token")
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        raise AuthenticationError("invalid token")
    return decoded


def csrf_hash(token: str | None) -> str:
    if token is None:
        raise AuthenticationError("invalid csrf")
    try:
        decoded = _unb64(token)
    except (ValueError, AuthenticationError) as exc:
        raise AuthenticationError("invalid csrf") from exc
    if len(decoded) != 32:
        raise AuthenticationError("invalid csrf")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthCookies:
    session: str
    csrf: str


@dataclass(slots=True)
class AuthService:
    password_hash: str
    signing_secret: bytes
    limiter: LoginAttemptLimiter
    user_id: str = "primary"
    secure_cookies: bool = True
    clock: Clock = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if len(self.signing_secret) < 32:
            raise ValueError("session signing secret must be at least 32 bytes")
        parameters = extract_parameters(self.password_hash)
        if (
            parameters.type is not Type.ID
            or parameters.memory_cost < 19_456
            or parameters.time_cost < 2
            or parameters.parallelism < 1
            or parameters.hash_len < 16
            or parameters.salt_len < 16
        ):
            raise ValueError("password hash must use sufficiently strong Argon2id parameters")

    @property
    def session_cookie_name(self) -> str:
        return "__Host-vocab-session" if self.secure_cookies else "vocab-session"

    @property
    def csrf_cookie_name(self) -> str:
        return "__Host-vocab-csrf" if self.secure_cookies else "vocab-csrf"

    def authenticate(self, password: str) -> AuthCookies | None:
        now = self.clock().astimezone(UTC)
        if not self.limiter.reserve(now):
            return None
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            return None
        try:
            valid: bool = PasswordHasher().verify(self.password_hash, password)
        except (InvalidHashError, VerificationError):
            valid = False
        if not valid:
            return None
        self.limiter.clear()
        csrf = _b64(secrets.token_bytes(32))
        payload = {
            "v": 1,
            "u": self.user_id,
            "iat": int(now.timestamp()),
            "exp": int((now + SESSION_TTL).timestamp()),
            "c": csrf_hash(csrf),
        }
        encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _b64(
            hmac.new(self.signing_secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return AuthCookies(f"v1.{encoded}.{signature}", csrf)

    def user_from_session(
        self, session: str | None, csrf: str | None = None, *, require_csrf: bool = False
    ) -> str:
        if session is None or len(session) > MAX_TOKEN_BYTES:
            raise AuthenticationError("missing session")
        parts = session.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise AuthenticationError("invalid session")
        try:
            encoded_payload = _unb64(parts[1])
            _unb64(parts[2])
            payload = json.loads(encoded_payload)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, AuthenticationError) as exc:
            raise AuthenticationError("invalid session") from exc
        expected = _b64(
            hmac.new(self.signing_secret, parts[1].encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, parts[2]) or not isinstance(payload, dict):
            raise AuthenticationError("invalid session")
        if (
            set(payload) != {"v", "u", "iat", "exp", "c"}
            or payload["v"] != 1
            or payload["u"] != self.user_id
            or not isinstance(payload["c"], str)
            or len(payload["c"]) != 64
            or any(character not in "0123456789abcdef" for character in payload["c"])
        ):
            raise AuthenticationError("invalid session")
        if not all(type(payload[key]) is int for key in ("iat", "exp")):
            raise AuthenticationError("invalid session")
        now = int(self.clock().astimezone(UTC).timestamp())
        if (
            payload["iat"] > now + 60
            or payload["exp"] <= now
            or payload["exp"] - payload["iat"] != int(SESSION_TTL.total_seconds())
        ):
            raise AuthenticationError("expired session")
        if require_csrf:
            try:
                binding = csrf_hash(csrf)
            except AuthenticationError as exc:
                raise AuthenticationError("invalid csrf") from exc
            if not hmac.compare_digest(payload["c"], binding):
                raise AuthenticationError("invalid csrf")
        return self.user_id
