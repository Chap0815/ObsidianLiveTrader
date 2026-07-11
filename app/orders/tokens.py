"""One-time preview tokens (in-memory TTL store).

SQLite may store a hash of the token for audit; the raw token is never logged.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Any


class TokenError(Exception):
    """Missing, expired, or already-used preview token."""


class PreviewStore:
    """In-memory one-time tokens with TTL.

    create() → raw token string
    consume() → payload dict; raises TokenError if invalid
    peek() → payload without consuming (optional; for diagnostics)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # token -> {payload, expires_at, used}
        self._items: dict[str, dict[str, Any]] = {}

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, payload: dict[str, Any], ttl: int) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + max(1, int(ttl))
        with self._lock:
            self._purge_unlocked()
            # One open preview intent only — multi-tab cannot accumulate
            # confirmable tokens and double-place with different externalOids.
            self._items.clear()
            self._items[token] = {
                "payload": payload,
                "expires_at": expires_at,
                "used": False,
            }
        return token

    def consume(self, token: str) -> dict[str, Any]:
        if not token:
            raise TokenError("preview token missing")
        with self._lock:
            self._purge_unlocked()
            item = self._items.get(token)
            if item is None:
                raise TokenError("preview token invalid or expired")
            if item["used"]:
                raise TokenError("preview token already used")
            if time.time() > float(item["expires_at"]):
                del self._items[token]
                raise TokenError("preview token expired")
            item["used"] = True
            # Drop after use so double-confirm cannot race-reuse
            payload = item["payload"]
            del self._items[token]
            return payload

    def peek(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge_unlocked()
            item = self._items.get(token)
            if item is None or item["used"]:
                return None
            if time.time() > float(item["expires_at"]):
                return None
            return item["payload"]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _purge_unlocked(self) -> None:
        now = time.time()
        dead = [
            t
            for t, it in self._items.items()
            if it["used"] or now > float(it["expires_at"])
        ]
        for t in dead:
            del self._items[t]
