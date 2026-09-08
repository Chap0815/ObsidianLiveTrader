"""One-time preview tokens (in-memory TTL store).

SQLite may store a hash of the token for audit; the raw token is never logged.
"""

from __future__ import annotations

import copy
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

    F-16 — SINGLE-WORKER REQUIRED: this store is process-local (a plain
    dict guarded by a threading.Lock), not backed by a shared cache or DB.
    One instance lives on app.state, created once in app.main's lifespan.
    If the app is ever run with more than one uvicorn/gunicorn worker
    process, each worker gets its OWN store: a preview token minted by
    worker A is invisible to worker B, so Confirm can 404 a perfectly
    valid token depending on which worker happens to handle the request.
    The bundled launcher (scripts/launch.py) never passes --workers, which
    is required for correctness, not just performance. See
    app.main._detect_multi_worker_env() for the startup warning this
    triggers if a common multi-worker env var is detected.
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
        stored_payload = copy.deepcopy(payload)
        # TTL is elapsed time. A wall-clock correction must neither extend nor
        # revive a confirm window, so keep this process-local deadline monotonic.
        expires_at = time.monotonic() + max(1, int(ttl))
        with self._lock:
            self._purge_unlocked()
            # One open preview intent only — multi-tab cannot accumulate
            # confirmable tokens and double-place with different externalOids.
            self._items.clear()
            self._items[token] = {
                "payload": stored_payload,
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
            if time.monotonic() >= float(item["expires_at"]):
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
            if time.monotonic() >= float(item["expires_at"]):
                return None
            return copy.deepcopy(item["payload"])

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def discard(self, token: str) -> None:
        """Remove only this token; a newer single-slot token must survive."""
        with self._lock:
            self._items.pop(token, None)

    def _purge_unlocked(self) -> None:
        now = time.monotonic()
        dead = [
            t
            for t, it in self._items.items()
            if it["used"] or now >= float(it["expires_at"])
        ]
        for t in dead:
            del self._items[t]
