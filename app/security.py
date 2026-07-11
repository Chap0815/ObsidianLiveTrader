"""Local request guards: symbol validation, optional local API token, loopback."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Callable
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request

from app.config import get_settings


def _token_matches(got: str | None, expected: str) -> bool:
    """Constant-time compare via SHA-256 digests (works for unequal lengths)."""
    if not got or not expected:
        return False
    a = hashlib.sha256(got.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_origin(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return host in _LOOPBACK_HOSTS

# MEXC: BTC_USDT | Hyperliquid: BTC or BTC_USDT (coin part used on HL)
SYMBOL_RE_MEXC = re.compile(r"^[A-Z0-9]{2,32}_[A-Z0-9]{2,16}$")
SYMBOL_RE_HL = re.compile(r"^[A-Z0-9]{2,20}(_[A-Z0-9]{2,16})?$")


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace("-", "_")
    try:
        ex = get_settings().exchange
    except Exception:
        ex = "mexc"
    if ex == "hyperliquid":
        if not SYMBOL_RE_HL.match(s):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid symbol {symbol!r}. Expected e.g. BTC or BTC_USDT",
            )
        # Prefer bare coin for HL APIs
        return s.split("_")[0]
    if not SYMBOL_RE_MEXC.match(s):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid symbol {symbol!r}. Expected e.g. BTC_USDT",
        )
    return s


def require_local_token(
    x_local_token: str | None = Header(default=None, alias="X-Local-Token"),
) -> None:
    """If LOCAL_API_TOKEN is set, require matching header on private routes."""
    s = get_settings()
    expected = (s.local_api_token or "").strip()
    if not expected:
        return
    if not _token_matches(x_local_token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Local-Token")


async def loopback_or_token_middleware(request: Request, call_next: Callable):
    """Block non-loopback clients on private API paths when no LAN intended."""
    path = request.url.path

    # CSRF / drive-by guard: a browser always sends Origin (and/or Referer) on a
    # cross-origin mutating request. Block any non-loopback origin for mutating
    # /api/* paths regardless of the token (empty token is the default). Requests
    # without Origin AND without Referer (curl, server-side, TestClient) pass.
    if request.method in _MUTATING_METHODS and path.startswith("/api/"):
        probe = request.headers.get("origin")
        if probe is None:
            probe = request.headers.get("referer")
        if probe is not None and not _is_loopback_origin(probe):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=403,
                content={"detail": "cross-origin request blocked"},
            )

    private_prefixes = (
        "/api/account",
        "/api/orders",
        "/api/analyze",
        "/api/sizing",
        "/api/history",
        "/api/llm",
        "/api/setup",
        "/api/scan",
        "/api/fills",
        "/api/reevaluate",
    )
    if any(path.startswith(p) for p in private_prefixes):
        client = request.client.host if request.client else ""
        loopbacks = {"127.0.0.1", "::1", "localhost", "testclient"}
        # Starlette TestClient uses "testclient"
        if client and client not in loopbacks and not client.startswith("127."):
            s = get_settings()
            # Allow only if local token presented and matches
            token = request.headers.get("X-Local-Token")
            expected = (s.local_api_token or "").strip()
            if not expected or not _token_matches(token, expected):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Private API only from loopback (or valid X-Local-Token)"
                    },
                )
    return await call_next(request)
