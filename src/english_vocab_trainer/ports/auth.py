from __future__ import annotations

from datetime import datetime
from typing import Protocol


class LoginAttemptLimiter(Protocol):
    """Atomically reserve an expensive password-verification attempt."""

    def reserve(self, now: datetime) -> bool: ...

    def clear(self) -> None: ...
