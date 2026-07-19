"""Local request guards: symbol validation, optional local API token, loopback."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Callable
from urllib.parse import urlparse

from fastapi import Cookie, Header, HTTPException, Request

from app.config import get_settings

# F-19: name of the HttpOnly session cookie that carries the local auth token.
# The dashboard sets it so the token no longer lives in the page DOM; the
# header (X-Local-Token) remains a valid fallback for API/WS clients.
AUTH_COOKIE_NAME = "local_auth"


def _token_matches(got: str | None, expected: str) -> bool:
    """Constant-time compare via SHA-256 digests (works for unequal lengths)."""
    if not got or not expected:
        return False
    a = hashlib.sha256(got.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def build_csp(script_nonce: str | None = None) -> str:
    """F-20: conservative Content-Security-Policy for the app's HTML pages.

    script-src is 'self' with NO 'unsafe-inline' — this is the key control:
    an injected inline <script> is blocked even if an escaping gap appears.
    The vendored chart lib and app.js load from /static (self). A page that
    still has a legitimate inline <script> (the setup wizard) passes a
    per-response nonce so only that exact block runs.

    style-src keeps 'unsafe-inline' because lightweight-charts injects inline
    styles dynamically (a nonce cannot cover those). Fonts are now vendored
    locally under /static/fonts (IBM Plex WOFF2), so no external font host is
    allowed — font-src is 'self' only and no Google Fonts origin appears in
    style-src (T49: no external request on first-start/setup). connect-src
    'self' covers same-origin fetch and the same-origin ws:// handshake.
    """
    script_src = "'self'"
    if script_nonce:
        script_src += f" 'nonce-{script_nonce}'"
    directives = [
        "default-src 'self'",
        f"script-src {script_src}",
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ]
    return "; ".join(directives)


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_origin(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _origin_matches_request(value: str, request) -> bool:
    """True only if the Origin/Referer is the SAME ORIGIN as the server the
    request actually hit — host AND port must match. A page on another
    localhost port (e.g. 127.0.0.1:9999) is a DIFFERENT origin in the browser's
    model and must be blocked for mutating /api/* calls; matching by hostname
    alone (the old check) let a cross-port drive-by through (audit CSRF-port)."""
    o = urlparse(value)
    o_host = (o.hostname or "").lower()
    o_port = o.port or (443 if o.scheme == "https" else 80)
    req_host = (request.url.hostname or "").lower()
    req_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return o_host == req_host and o_port == req_port

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
    local_auth: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> None:
    """If LOCAL_API_TOKEN is set, require a matching credential on private routes.

    F-19: the HttpOnly session cookie (set on the dashboard) and the
    X-Local-Token header are both accepted — additive, so browser (cookie),
    API and WebSocket (header) clients all keep working.
    """
    s = get_settings()
    expected = (s.local_api_token or "").strip()
    if not expected:
        return
    if _token_matches(x_local_token, expected) or _token_matches(local_auth, expected):
        return
    raise HTTPException(
        status_code=401,
        detail="Invalid or missing local auth token (X-Local-Token header or local_auth cookie)",
    )


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
        if probe is not None and not _origin_matches_request(probe, request):
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
        "/api/journal",
        "/api/llm",
        "/api/setup",
        "/api/settings",
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
            # Allow only if a matching credential is presented — header token
            # or the HttpOnly session cookie (F-19, additive).
            token = request.headers.get("X-Local-Token")
            cookie_tok = request.cookies.get(AUTH_COOKIE_NAME)
            expected = (s.local_api_token or "").strip()
            if not expected or not (
                _token_matches(token, expected)
                or _token_matches(cookie_tok, expected)
            ):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Private API only from loopback (or valid X-Local-Token)"
                    },
                )
    return await call_next(request)
