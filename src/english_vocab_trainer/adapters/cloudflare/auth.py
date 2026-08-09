from __future__ import annotations

from typing import Any

import jwt


def verify_access_jwt(token: str, key: str, issuer: str, audience: str) -> dict[str, Any]:
    """Verify Cloudflare Access signature, issuer and audience at the edge boundary."""
    return jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, audience=audience)
