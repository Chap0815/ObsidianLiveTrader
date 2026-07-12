import asyncio
import hashlib
import html as _html
import json
import logging
import os
import re as _re
import time as _time
import defusedxml.ElementTree as _ET  # hardened parser: news feeds are untrusted
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.analysis.context import build_market_snapshot, snapshot_to_api_dict
from app.config import Settings, get_settings
from app.db.repo import Database
from app.exchange_factory import create_exchange_client, exchange_ready
from app.hyperliquid.errors import HyperliquidError
from app.llm.client import LlmError, analyze_with_llm, build_llm_context, reevaluate_with_llm

# Back-compat alias
GrokError = LlmError
from app.mexc.client import empty_account
from app.mexc.errors import MexcError
from app.models import (
    AnalyzeRequest,
    CancelRequest,
    ClosePositionRequest,
    ConfirmRequest,
    ModifySLRequest,
    OrderTicket,
    ReevaluateRequest,
)
from app.orders.service import OrderError, OrderService, estimate_same_side_risk_usdt
from app.orders.tokens import PreviewStore
from app.risk.sizing import suggest_vol
from app.security import (
    AUTH_COOKIE_NAME,
    build_csp,
    loopback_or_token_middleware,
    normalize_symbol,
    require_local_token,
)

ExchangeError = (MexcError, HyperliquidError)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
STATIC_DIR = BASE / "static"

log = logging.getLogger("app.main")

# F-16 (deployment/concurrency): env var names some process managers use to
# announce a multi-worker/multi-process launch. Checked at startup so a
# non-default deployment (this app's own launcher, scripts/launch.py, never
# sets these) gets a loud warning instead of silently corrupting state.
_MULTI_WORKER_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS")


def _detect_multi_worker_env(env: dict[str, str] | None = None) -> str | None:
    """Return a loud warning string if the environment announces more than
    one worker process, else None. Never raises — see module docstring
    note at the preview-store/trade-lock state below for WHY this matters.
    """
    src = os.environ if env is None else env
    for var in _MULTI_WORKER_ENV_VARS:
        raw = src.get(var)
        if raw is None:
            continue
        try:
            n = int(str(raw).strip())
        except ValueError:
            continue
        if n > 1:
            return (
                f"SINGLE-WORKER REQUIRED: {var}={n} detected, but this "
                "app's preview-token store (app.orders.tokens.PreviewStore) "
                "and its confirm/close trade_lock are IN-PROCESS ONLY "
                "(no shared cache/DB backing). With more than one worker, "
                "each process gets its own copy: a preview token minted by "
                "one worker is invisible to another (Confirm can 404 a "
                "valid token), and the trade_lock no longer serializes "
                "concurrent confirm/close calls across workers (a race can "
                "pass risk gates twice / double-fill). Run with a single "
                "worker (the bundled launcher, scripts/launch.py, never "
                "passes --workers; do not add --workers > 1 or a "
                f"multi-process WSGI/ASGI manager without removing {var})."
            )
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    client = create_exchange_client(s)
    db = Database(s.database_path)
    await db.init()
    # F-16: PreviewStore is an in-process, in-memory TTL store (see
    # app/orders/tokens.py) — it is NOT shared across worker processes.
    # This app MUST run with a single uvicorn worker (the launcher,
    # scripts/launch.py, never passes --workers). See
    # _detect_multi_worker_env() above for the failure mode if that
    # invariant is ever violated.
    store = PreviewStore()
    app.state.mexc = client  # legacy name: active exchange client
    app.state.exchange = client
    app.state.db = db
    app.state.preview_store = store
    # App-global lock: a new OrderService is built per request, so the lock
    # that serializes confirm/close must live here, not on the instance.
    # F-16: like PreviewStore above, this asyncio.Lock only serializes
    # requests WITHIN this one process — it provides no cross-worker
    # mutual exclusion. Single-worker is required for this to be a real
    # guarantee against concurrent double-fills.
    import asyncio as _asyncio

    app.state.trade_lock = _asyncio.Lock()
    # Singleflight lock for /api/news: concurrent cache-miss callers await
    # one in-flight refresh instead of each firing a full feed-fetch batch.
    app.state.news_lock = _asyncio.Lock()
    multi_worker_warning = _detect_multi_worker_env()
    if multi_worker_warning:
        log.warning(multi_worker_warning)
    try:
        yield
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose:
            await aclose()


app = FastAPI(title="Obsidian Live Trader", version="0.3.0", lifespan=lifespan)
app.middleware("http")(loopback_or_token_middleware)
# DNS-rebinding guard: only loopback host headers are served
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "::1", "testserver"],
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _order_service(request: Request) -> OrderService:
    s = get_settings()
    # Prefer app.state.mexc (tests monkeypatch this); exchange is alias
    client = getattr(request.app.state, "mexc", None) or getattr(
        request.app.state, "exchange", None
    )
    if client is None:
        raise HTTPException(status_code=503, detail="Exchange client not initialized")
    store = getattr(request.app.state, "preview_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Preview store not initialized")
    db = getattr(request.app.state, "db", None)
    lock = getattr(request.app.state, "trade_lock", None)
    return OrderService(client, s, store, db=db, trade_lock=lock)


def _llm_settings(request: Request):
    """Settings with the runtime LLM hot-swap override applied (no restart)."""
    s = get_settings()
    override = getattr(request.app.state, "llm_override", None)
    if override and override != s.llm_provider:
        return s.model_copy(update={"llm_provider": override})
    return s


@app.get("/api/health")
async def health(request: Request):
    s = get_settings()
    eff = _llm_settings(request)
    # F-24: report the ACTUAL armed/mainnet state, not a hardcoded True —
    # monitoring must be able to trust this instead of misreading a disarmed
    # or testnet instance as live-armed.
    on_testnet = s.exchange == "hyperliquid" and s.hl_testnet
    live_trading = bool(s.trading_enabled) and not on_testnet
    return {
        "ok": True,
        "live_trading": live_trading,
        "exchange": s.exchange,
        "hl_testnet": s.hl_testnet if s.exchange == "hyperliquid" else None,
        "trading_enabled": s.trading_enabled,
        "bind": f"{s.host}:{s.port}",
        "exchange_configured": exchange_ready(s),
        "mexc_configured": s.mexc_ready,
        "hl_configured": s.hl_ready,
        "llm_provider": eff.llm_provider,
        "llm_configured": eff.llm_ready,
        "claude_configured": s.claude_ready,
        "xai_configured": s.xai_ready,
        "default_symbol": s.default_symbol,
        "max_leverage": s.max_leverage,
        "max_risk_pct": s.max_risk_pct,
        "min_rrr": s.min_rrr,
        "strict_rrr": s.strict_rrr,
        "allow_unprotected_entry": s.allow_unprotected_entry,
    }


def _llm_status(request: Request) -> dict:
    s = get_settings()
    eff = _llm_settings(request)
    return {
        "provider": eff.llm_provider,
        "providers": [
            {"id": "claude", "label": "Claude Opus", "configured": s.claude_ready},
            {"id": "xai", "label": "Grok", "configured": s.xai_ready},
            {"id": "openai", "label": "Codex", "configured": s.openai_ready},
            {"id": "ollama", "label": "Ollama (lokal)", "configured": s.ollama_ready},
        ],
    }


@app.get("/api/llm")
async def llm_get(request: Request, _: None = Depends(require_local_token)):
    """Current KI provider + availability (for the hot-swap dropdown)."""
    return _llm_status(request)


@app.post("/api/llm")
async def llm_set(
    request: Request,
    body: dict,
    _: None = Depends(require_local_token),
):
    """Hot-swap the KI provider for this session (env stays the default)."""
    provider = str((body or {}).get("provider") or "").strip().lower()
    aliases = {"anthropic": "claude", "grok": "xai", "codex": "openai", "local": "ollama"}
    provider = aliases.get(provider, provider)
    ready = {
        "claude": lambda s: s.claude_ready,
        "xai": lambda s: s.xai_ready,
        "openai": lambda s: s.openai_ready,
        "ollama": lambda s: s.ollama_ready,
    }
    if provider not in ready:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: claude, xai, openai, ollama",
        )
    s = get_settings()
    if not ready[provider](s):
        raise HTTPException(
            status_code=400,
            detail=f"{provider} is not configured (missing API key in .env)",
        )
    request.app.state.llm_override = provider
    return _llm_status(request)


# ── First-run setup (env generator) ─────────────────────────────────
# Only usable while NO .env exists — once configured, the page locks so
# no local process can rewrite keys through the browser.
from app.config import ROOT as _ROOT  # noqa: E402

ENV_PATH = _ROOT / ".env"


def _setup_needed() -> bool:
    return not ENV_PATH.exists()


def build_env_content(p: dict) -> str:
    """Build a complete .env from the setup form payload. Raises ValueError."""
    import re as _re
    import secrets as _secrets

    def _san(name: str, raw) -> str:
        """Strip + reject control chars: a value with an embedded newline
        could otherwise inject extra .env lines (e.g. TRADING_ENABLED=true)."""
        v = str(raw or "").strip()
        if any(ord(ch) < 32 or ch == "\x7f" for ch in v):
            raise ValueError(f"{name} enthält ungültige Steuerzeichen")
        return v

    ex = str(p.get("exchange") or "hl-testnet")
    if ex not in ("hl-testnet", "hl-mainnet", "mexc"):
        raise ValueError("Ungültige Börsen-Auswahl")
    is_mexc = ex == "mexc"
    hl_testnet = ex == "hl-testnet"

    hl_key = _san("Private Key", p.get("hl_private_key"))
    hl_addr = _san("Wallet-Adresse", p.get("hl_account_address"))
    mexc_key = _san("MEXC API Key", p.get("mexc_api_key"))
    mexc_sec = _san("MEXC API Secret", p.get("mexc_api_secret"))
    if is_mexc and (not mexc_key or not mexc_sec):
        raise ValueError("MEXC API Key und Secret werden benötigt")
    if not is_mexc and not _re.fullmatch(r"0x[0-9a-fA-F]{64}", hl_key):
        raise ValueError(
            "Hyperliquid Private Key muss 0x + 64 Hex-Zeichen sein "
            "(API/Agent-Wallet-Key aus der Hyperliquid-UI)"
        )
    if hl_addr and not _re.fullmatch(r"0x[0-9a-fA-F]{40}", hl_addr):
        raise ValueError("Wallet-Adresse muss 0x + 40 Hex-Zeichen sein")

    llm = str(p.get("llm_provider") or "claude").strip().lower()
    if llm not in ("claude", "xai", "openai", "ollama", "none"):
        raise ValueError("Ungültiger KI-Anbieter")
    llm_key = _san("KI API Key", p.get("llm_api_key"))
    if llm in ("claude", "xai", "openai") and not llm_key:
        raise ValueError(f"API Key für {llm} fehlt")
    ollama_model = _san("Ollama-Modell", p.get("ollama_model")) or "llama3.1"
    include_account = p.get("include_account_in_llm", True)
    include_account = "true" if (include_account is not False) else "false"

    def _num(name: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(p.get(name, default))
        except (TypeError, ValueError):
            raise ValueError(f"{name} ist keine Zahl") from None
        if not (lo <= v <= hi):
            raise ValueError(f"{name} muss zwischen {lo} und {hi} liegen")
        return v

    risk_pct = _num("max_risk_pct", 1.0, 0.1, 10)
    max_lev = int(_num("max_leverage", 20, 1, 125))
    min_rrr = _num("min_rrr", 2.0, 1, 10)
    max_notional = _num("max_notional_usdt", 500, 10, 1_000_000)

    token = _secrets.token_urlsafe(24)
    default_symbol = "BTC_USDT" if is_mexc else "BTC"
    provider_for_env = "claude" if llm == "none" else llm

    lines = [
        "# Generiert vom Setup-Assistenten — Werte jederzeit hier änderbar.",
        "HOST=127.0.0.1",
        "PORT=8788",
        "",
        f"EXCHANGE={'mexc' if is_mexc else 'hyperliquid'}",
        f"HL_TESTNET={'true' if hl_testnet else 'false'}",
        f"HL_PRIVATE_KEY={hl_key}",
        f"HL_ACCOUNT_ADDRESS={hl_addr}",
        f"MEXC_API_KEY={mexc_key}",
        f"MEXC_API_SECRET={mexc_sec}",
        "MEXC_BASE_URL=https://contract.mexc.com",
        "",
        "# KI (Hot-Swap im UI möglich)",
        f"LLM_PROVIDER={provider_for_env}",
        f"ANTHROPIC_API_KEY={llm_key if llm == 'claude' else ''}",
        "ANTHROPIC_MODEL=claude-opus-4-8",
        f"XAI_API_KEY={llm_key if llm == 'xai' else ''}",
        "XAI_MODEL=grok-4",
        f"OPENAI_API_KEY={llm_key if llm == 'openai' else ''}",
        "OPENAI_MODEL=gpt-5.1",
        "OLLAMA_BASE_URL=http://127.0.0.1:11434/v1",
        f"OLLAMA_MODEL={ollama_model}",
        f"INCLUDE_ACCOUNT_IN_LLM={include_account}",
        "",
        "# Risiko-Gates (serverseitig erzwungen)",
        f"MAX_RISK_PCT={risk_pct}",
        f"MAX_LEVERAGE={max_lev}",
        f"MIN_RRR={min_rrr}",
        # RRR below MIN_RRR is a warning, not a hard block — the trader decides
        # (SL is still mandatory; MAX_RISK_PCT / notional / leverage still hard).
        "STRICT_RRR=false",
        f"MAX_NOTIONAL_USDT={max_notional}",
        "RISK_SLIPPAGE_PCT=0.05",
        "MAX_PRICE_DRIFT_PCT=0.5",
        "MARKET_ENTRY_SLIPPAGE_PCT=0.15",
        "ALLOW_CROSS_MARGIN=false",
        "ALLOW_UNPROTECTED_ENTRY=false",
        "AUTO_FLATTEN_IF_SL_UNVERIFIED=true",
        "",
        "# Sicherheit — Trading bleibt aus, bis DU es hier einschaltest",
        "TRADING_ENABLED=false",
        f"LOCAL_API_TOKEN={token}",
        "REQUIRE_LOOPBACK_WHEN_ARMED=true",
        "STRICT_AVAILABLE_MARGIN=true",
        "",
        f"DEFAULT_SYMBOL={default_symbol}",
        "PREVIEW_TOKEN_TTL_SECONDS=60",
        "DATABASE_PATH=data/trader.db",
        "KLINE_LIMIT_HINT=500",
        "",
    ]
    return "\n".join(lines)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """First-run wizard; locked once a .env exists."""
    if not _setup_needed():
        return RedirectResponse("/", status_code=303)
    # F-20: the setup page has one legitimate inline <script>; give it a
    # per-response nonce so script-src can stay 'self' + nonce (no unsafe-inline).
    import secrets as _secrets

    nonce = _secrets.token_urlsafe(16)
    resp = templates.TemplateResponse(request, "setup.html", {"csp_nonce": nonce})
    resp.headers["Content-Security-Policy"] = build_csp(script_nonce=nonce)
    return resp


@app.post("/api/setup")
async def setup_save(request: Request, body: dict):
    """Write .env from the wizard and hot-apply settings (no restart)."""
    if not _setup_needed():
        raise HTTPException(
            status_code=403,
            detail=".env existiert bereits — Setup gesperrt. Zum Neu-Einrichten die .env löschen.",
        )
    try:
        content = build_env_content(body or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Validate by parsing before it becomes the real .env
    tmp = ENV_PATH.with_name(".env.setup-tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        Settings(_env_file=str(tmp))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Konfiguration ungültig: {e}") from e
    import os as _os

    _os.replace(tmp, ENV_PATH)

    # Hot-apply: fresh settings + fresh exchange client, no restart needed
    get_settings.cache_clear()
    s = get_settings()
    old = getattr(request.app.state, "mexc", None)
    client = create_exchange_client(s)
    request.app.state.mexc = client
    request.app.state.exchange = client
    request.app.state.symbols_cache = None
    request.app.state.llm_override = None
    if old is not None:
        try:
            await old.aclose()
        except Exception:
            pass
    return {"ok": True, "exchange": s.exchange, "llm_provider": s.llm_provider}


# Majors fallback so the dropdown is usable even during an exchange outage
FALLBACK_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK",
    "SUI", "LTC", "TON", "ARB", "OP", "APT", "NEAR", "DOT", "PEPE",
    "WIF", "HYPE",
]


def _fallback_symbols() -> list[str]:
    s = get_settings()
    if s.exchange == "mexc":
        return [c + "_USDT" for c in FALLBACK_COINS]
    return list(FALLBACK_COINS)


@app.get("/api/symbols")
async def symbols(request: Request):
    """Tradeable symbols of the active exchange (cached 10 min).

    On exchange outage: serve the stale cache, else a majors fallback —
    the dropdown must never be empty.
    """
    client = getattr(request.app.state, "mexc", None)
    if client is None:
        return {"symbols": _fallback_symbols(), "error": "Exchange client not initialized"}
    import time as _time

    cache = getattr(request.app.state, "symbols_cache", None)
    if cache and _time.monotonic() - cache[0] < 600:
        return {"symbols": cache[1], "error": None}
    try:
        syms = await client.list_symbols()
    except ExchangeError as e:
        if cache:
            return {"symbols": cache[1], "error": None, "stale": True}
        return {"symbols": _fallback_symbols(), "error": str(e), "fallback": True}
    if not syms:
        return {"symbols": _fallback_symbols(), "error": None, "fallback": True}
    request.app.state.symbols_cache = (_time.monotonic(), syms)
    return {"symbols": syms, "error": None}


@app.get("/api/market/{symbol}")
async def market(
    request: Request,
    symbol: str,
    tf: str = Query("15m", description="LTF interval"),
    htf: str = Query("1H", description="HTF interval"),
):
    """Public market snapshot: klines, indicators, structure, funding, contract."""
    symbol = normalize_symbol(symbol)
    client: MexcClient | None = getattr(request.app.state, "mexc", None)
    if client is None:
        raise HTTPException(status_code=503, detail="MEXC client not initialized")
    s = get_settings()
    try:
        snap = await build_market_snapshot(
            symbol, tf, htf, client, limit_hint=s.kline_limit_hint
        )
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return snapshot_to_api_dict(snap)


@app.get("/api/mini")
async def mini(
    request: Request,
    symbols: str = Query("", description="Comma-separated coins for the overview grid"),
    tf: str = Query("15m", description="Kline interval"),
    limit: int = Query(96, ge=2, le=200, description="Candles per coin (~24h at 15m)"),
):
    """Lightweight multi-coin candle snapshot for the overview mini-charts.

    Public data only: OHLC candles + last price + window change. No indicators,
    structure, funding or private data — deliberately cheaper than /api/market
    so the overview can load a dozen coins at once. Touches no risk gates.
    """
    raw = [s for s in (symbols or "").split(",") if s.strip()]
    syms: list[str] = []
    invalid_errors: list[str] = []
    for s in raw:
        try:
            n = normalize_symbol(s)
        except HTTPException as e:
            # Skip an invalid symbol instead of failing the whole overview
            # request — other requested symbols may still be valid.
            invalid_errors.append(f"{s}: {e.detail}")
            continue
        if n and n not in syms:
            syms.append(n)
    syms = syms[:24]  # hard cap: overview never fans out unbounded
    if not syms:
        return {"results": [], "errors": invalid_errors}

    client = getattr(request.app.state, "mexc", None) or getattr(
        request.app.state, "exchange", None
    )
    if client is None:
        raise HTTPException(status_code=503, detail="Exchange client not initialized")

    async def one(sym: str):
        candles = await client.klines(sym, tf, limit_hint=limit)
        cs = candles[-limit:]
        out = [
            {
                "time": c.time,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
            }
            for c in cs
        ]
        last = out[-1]["close"] if out else None
        first = out[0]["close"] if out else None
        change = (
            round((last - first) / first * 100.0, 2)
            if last is not None and first not in (None, 0)
            else None
        )
        return {"symbol": sym, "last_price": last, "change_pct": change, "candles": out}

    gathered = await asyncio.gather(
        *(one(s) for s in syms), return_exceptions=True
    )
    results: list[dict] = []
    errors: list[str] = list(invalid_errors)
    for sym, r in zip(syms, gathered):
        if isinstance(r, Exception):
            errors.append(f"{sym}: {r}")
        else:
            results.append(r)
    return {"results": results, "errors": errors}


# --- Crypto news (public RSS, no API key) ---------------------------------
# Free, key-less RSS/Atom feeds. Exchange-independent (MEXC/HL-agnostic).
NEWS_FEEDS: list[tuple[str, str]] = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]
NEWS_CACHE_TTL = 300.0      # 5 min — news moves slowly; spare the upstreams
NEWS_FEED_TIMEOUT = 6.0     # per-feed hard timeout
NEWS_MAX_ITEMS = 40
NEWS_MAX_FEED_BYTES = 2 * 1024 * 1024  # 2 MB cap per feed response (F-14, DoS)
_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = _re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Feed content is UNTRUSTED: drop tags, unescape entities. The frontend
    escapes again (defence in depth)."""
    if not s:
        return ""
    return _html.unescape(_TAG_RE.sub("", s)).strip()


def _parse_news_date(raw: str) -> tuple[str, float]:
    """RFC-822 (RSS pubDate) or ISO-8601 (Atom) -> (UTC ISO display, epoch sort key)."""
    raw = (raw or "").strip()
    if not raw:
        return "", 0.0
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            # Unparseable date: still strip the raw feed string (untrusted)
            # so no feed-derived value reaches the payload un-stripped.
            return _strip_html(raw), 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(), dt.timestamp()


def _parse_feed(xml_text: str, source: str) -> list[dict]:
    """Parse an RSS <item> or Atom <entry> document into news dicts.

    Raises xml.etree.ElementTree.ParseError on malformed XML — the caller
    isolates that per feed."""
    root = _ET.fromstring(xml_text)
    nodes = root.findall(".//item")
    is_atom = False
    if not nodes:
        nodes = root.findall(f".//{_ATOM}entry")
        is_atom = True
    out: list[dict] = []
    for node in nodes:
        if is_atom:
            title = node.findtext(f"{_ATOM}title", "")
            link_el = node.find(f"{_ATOM}link")
            url = link_el.get("href", "") if link_el is not None else ""
            published = node.findtext(f"{_ATOM}updated") or node.findtext(f"{_ATOM}published") or ""
            summary = node.findtext(f"{_ATOM}summary") or ""
        else:
            title = node.findtext("title", "")
            url = node.findtext("link", "")
            published = node.findtext("pubDate", "")
            summary = node.findtext("description", "")
        title = _strip_html(title)
        if not title:
            continue
        iso, sort_key = _parse_news_date(published)
        out.append(
            {
                "title": title,
                "source": source,
                "url": (url or "").strip(),
                "published": iso,
                "summary": _strip_html(summary)[:280],
                "_sort": sort_key,
            }
        )
    return out


async def _fetch_feed_body(client: httpx.AsyncClient, url: str) -> str:
    """Stream a feed response, aborting once it exceeds NEWS_MAX_FEED_BYTES
    instead of reading an unbounded body fully into memory (F-14, DoS)."""
    async with client.stream(
        "GET", url, headers={"User-Agent": "ObsidianLiveTrader/1.0"}
    ) as resp:
        resp.raise_for_status()
        total = 0
        chunks: list[bytes] = []
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > NEWS_MAX_FEED_BYTES:
                raise ValueError(
                    f"feed response exceeds {NEWS_MAX_FEED_BYTES} byte cap"
                )
            chunks.append(chunk)
        encoding = resp.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")


async def _refresh_news(request: Request) -> dict:
    """Do the actual multi-feed fetch + merge. Caller must hold news_lock and
    is responsible for the cache read/write around this."""
    cache = getattr(request.app.state, "news_cache", None)

    async def one(source: str, url: str, client: httpx.AsyncClient) -> list[dict]:
        text = await _fetch_feed_body(client, url)
        return _parse_feed(text, source)

    # Known stable HTTPS feed endpoints — a feed that suddenly issues a
    # redirect (e.g. a compromised/hijacked host pointing at loopback, LAN or
    # cloud-metadata targets) must fail isolated for that feed, not be
    # followed (F-13, SSRF).
    async with httpx.AsyncClient(
        timeout=NEWS_FEED_TIMEOUT, follow_redirects=False
    ) as client:
        gathered = await asyncio.gather(
            *(one(src, url, client) for src, url in NEWS_FEEDS),
            return_exceptions=True,
        )

    items: list[dict] = []
    errors: list[str] = []
    for (src, _url), r in zip(NEWS_FEEDS, gathered):
        if isinstance(r, Exception):
            errors.append(f"{src}: {r}")
        else:
            items.extend(r)

    items.sort(key=lambda it: it.get("_sort", 0.0), reverse=True)
    for it in items:
        it.pop("_sort", None)
    items = items[:NEWS_MAX_ITEMS]

    payload = {"items": items, "errors": errors}
    # Only overwrite the cache when we actually got items; otherwise keep
    # serving the last good payload (stale) instead of an empty page.
    if items or not cache:
        request.app.state.news_cache = (_time.monotonic(), payload)
        return payload
    stale = dict(cache[1])
    stale["stale"] = True
    return stale


@app.get("/api/news")
async def news(request: Request):
    """Merged crypto headlines from public RSS/Atom feeds. Public + read-only.

    No API key, no new dependency. Per-feed failures are isolated (a dead feed
    never breaks the page), each feed has a hard timeout, each response is
    size-capped, and results are cached 5 min in-memory. Concurrent cache-miss
    callers share one in-flight refresh (singleflight) instead of each firing
    a full fetch batch. On a total fetch failure the last good payload is
    served stale. HTML is stripped server-side; the client escapes again."""
    cache = getattr(request.app.state, "news_cache", None)
    if cache and _time.monotonic() - cache[0] < NEWS_CACHE_TTL:
        return cache[1]

    lock = getattr(request.app.state, "news_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.news_lock = lock

    async with lock:
        # Re-check: another caller may have already refreshed while we were
        # waiting for the lock (singleflight — only one refresh in flight).
        cache = getattr(request.app.state, "news_cache", None)
        if cache and _time.monotonic() - cache[0] < NEWS_CACHE_TTL:
            return cache[1]
        return await _refresh_news(request)


@app.get("/api/account")
async def account(request: Request, _: None = Depends(require_local_token)):
    """Private balance + open positions. Always 200; errors in body for UI."""
    s = get_settings()
    if not exchange_ready(s):
        return empty_account(
            error="Exchange keys not configured (MEXC API or HL_PRIVATE_KEY)"
        )

    client = getattr(request.app.state, "mexc", None) or getattr(
        request.app.state, "exchange", None
    )
    if client is None:
        return empty_account(error="Exchange client not initialized")

    try:
        return await client.account_snapshot()
    except ExchangeError as e:
        return empty_account(error=str(e))


@app.get("/api/fills")
async def fills(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(require_local_token),
):
    """Recent account executions for the chart trade markers. Read-only.

    Only exchanges with a fill-history API are supported (Hyperliquid
    userFills); others report supported=False and the UI hides the markers.
    Soft errors (rate limit etc.) return 200 with an error string.
    """
    client = getattr(request.app.state, "mexc", None) or getattr(
        request.app.state, "exchange", None
    )
    if client is None or not hasattr(client, "user_fills"):
        return {"fills": [], "supported": False, "error": None}
    if symbol:
        symbol = normalize_symbol(symbol)
    try:
        rows = await client.user_fills(symbol=symbol, limit=limit)
    except ExchangeError as e:
        return {"fills": [], "supported": True, "error": str(e)}
    return {"fills": rows, "supported": True, "error": None}


@app.post("/api/analyze")
async def analyze(
    request: Request,
    body: AnalyzeRequest,
    _: None = Depends(require_local_token),
):
    """Build market context, call Claude (or xAI), return validated TradeProposal.

    Advisory only — does not place orders and never bypasses risk gates.
    """
    s = _llm_settings(request)  # honors the hot-swap dropdown
    if not s.llm_ready:
        raise HTTPException(
            status_code=400,
            detail=(
                "LLM not configured. Set ANTHROPIC_API_KEY (Claude, default) "
                "or XAI_API_KEY with LLM_PROVIDER=xai."
            ),
        )

    client: MexcClient | None = getattr(request.app.state, "mexc", None)
    if client is None:
        raise HTTPException(status_code=503, detail="MEXC client not initialized")

    symbol = normalize_symbol(body.symbol or s.default_symbol)
    tf = body.tf or "15m"
    htf = body.htf or "1H"

    try:
        snap = await build_market_snapshot(
            symbol, tf, htf, client, limit_hint=s.kline_limit_hint
        )
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=f"MEXC market error: {e}") from e

    market_api = snapshot_to_api_dict(snap)

    # Soft account snapshot for context (optional; keys may be missing)
    if exchange_ready(s) and s.include_account_in_llm:
        try:
            acct = await client.account_snapshot()
        except ExchangeError as e:
            acct = empty_account(error=str(e))
    else:
        acct = empty_account(
            error=None
            if not s.include_account_in_llm
            else "MEXC keys not configured"
        )

    context = build_llm_context(market_api, acct, s, scanner_verdict=body.scanner_verdict)

    try:
        proposal = await analyze_with_llm(context, s)
    except LlmError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    annotations = {
        "rrr_computed": proposal.rrr,
        "advisory_only": True,
        "gates_not_bypassed": True,
        "tf": tf,
        "htf": htf,
    }
    # Sanity read for the UI: how actionable is the proposal right now?
    last_px = market_api.get("last_price")
    ltf_last = ((market_api.get("ltf") or {}).get("indicators") or {}).get("last") or {}
    atr = ltf_last.get("atr14")
    try:
        if proposal.entry_price and last_px:
            annotations["entry_vs_last_pct"] = round(
                (float(proposal.entry_price) - float(last_px)) / float(last_px) * 100.0, 3
            )
        if proposal.entry_price and proposal.stop_loss and atr:
            annotations["sl_distance_atr"] = round(
                abs(float(proposal.entry_price) - float(proposal.stop_loss)) / float(atr), 2
            )
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    proposal_dict = proposal.model_dump()

    # Audit trail (Task 8) — soft-fail so analyze still returns on DB issues
    db: Database | None = getattr(request.app.state, "db", None)
    if db is not None:
        ctx_fingerprint = {
            "symbol": symbol,
            "tf": tf,
            "htf": htf,
            "last_price": market_api.get("last_price"),
        }
        context_hash = hashlib.sha256(
            json.dumps(ctx_fingerprint, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        try:
            await db.insert_proposal(
                symbol=symbol,
                proposal_json=proposal_dict,
                annotations_json=annotations,
                context_hash=context_hash,
            )
        except Exception:
            # Do not break advisory analyze if SQLite is unavailable
            pass

    return {
        "symbol": symbol,
        "tf": tf,
        "htf": htf,
        "proposal": proposal_dict,
        "annotations": annotations,
        "last_price": market_api.get("last_price"),
    }


def _classify_unlabeled_trigger(
    trigger_price: float, side: str | None, entry_price: float | None
) -> str | None:
    """Classify an unlabeled trigger order as 'sl' or 'tp' by position side +
    entry: a stop sits on the loss side of entry, a take-profit on the profit
    side. Returns None ("unknown") when side/entry aren't known or the
    trigger sits exactly on entry — callers must NEVER default to SL in that
    case (F-12: a fabricated SL can mask an actually-unprotected position)."""
    side_n = (side or "").strip().lower()
    if side_n not in ("long", "short"):
        return None
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    if trigger_price == entry:
        return None
    if side_n == "long":
        return "sl" if trigger_price < entry else "tp"
    return "sl" if trigger_price > entry else "tp"


def _extract_position_sl_tp(
    stops: list[dict], *, side: str | None = None, entry_price: float | None = None
) -> tuple[float | None, float | None]:
    """Best-effort current SL/TP from open trigger orders for one symbol.

    Mirrors the frontend's `findPositionProtection` heuristic (app.js): an
    explicit stopLossPrice/takeProfitPrice field wins; otherwise fall back to
    triggerPrice/price + an orderType label. An unlabeled trigger is
    classified by position side + entry (loss side = SL, profit side = TP);
    if that can't be determined it counts as neither (unknown), never a
    fabricated SL. Read-only — never places/cancels anything.
    """
    sl: float | None = None
    tp: float | None = None
    for row in stops or []:
        try:
            sl_field = float(row.get("stopLossPrice"))
        except (TypeError, ValueError):
            sl_field = None
        if sl_field and sl_field > 0:
            sl = sl_field
            continue
        try:
            tp_field = float(row.get("takeProfitPrice"))
        except (TypeError, ValueError):
            tp_field = None
        if tp_field and tp_field > 0:
            tp = tp_field
            continue
        raw_trg = row.get("triggerPrice")
        if raw_trg is None:
            raw_trg = row.get("price")
        try:
            trg = float(raw_trg)
        except (TypeError, ValueError):
            trg = None
        if not trg or trg <= 0:
            continue
        label = str(row.get("orderType") or "").lower()
        if label.startswith("tp") or "take" in label:
            tp = trg
        elif label.startswith("sl") or "stop" in label:
            sl = trg
        else:
            # Unlabeled trigger: never assume SL. Classify by side + entry;
            # if that's not resolvable, it's unknown protection (neither).
            kind = _classify_unlabeled_trigger(trg, side, entry_price)
            if kind == "sl":
                sl = trg
            elif kind == "tp":
                tp = trg
    return sl, tp


@app.post("/api/reevaluate")
async def reevaluate(
    request: Request,
    body: ReevaluateRequest,
    _: None = Depends(require_local_token),
):
    """Advisory reevaluation of an ALREADY OPEN position: HOLD / MOVE_SL_BE /
    PARTIAL_CLOSE / CLOSE, with a reason.

    Same auth/shape as /api/analyze. Never places, moves or closes anything
    itself and never bypasses risk gates — the human applies the
    recommendation via the app's existing SL/close controls.
    """
    s = _llm_settings(request)  # honors the hot-swap dropdown
    if not s.llm_ready:
        raise HTTPException(
            status_code=400,
            detail=(
                "LLM not configured. Set ANTHROPIC_API_KEY (Claude, default) "
                "or XAI_API_KEY with LLM_PROVIDER=xai."
            ),
        )

    client: MexcClient | None = getattr(request.app.state, "mexc", None)
    if client is None:
        raise HTTPException(status_code=503, detail="MEXC client not initialized")

    symbol = normalize_symbol(body.symbol or s.default_symbol)
    tf = body.tf or "15m"
    htf = body.htf or "1H"

    try:
        acct = await client.account_snapshot()
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=f"MEXC account error: {e}") from e

    position = next(
        (
            p
            for p in (acct.get("positions") or [])
            if str(p.get("symbol") or "").upper() == symbol.upper()
            and abs(float(p.get("hold_vol") or 0)) > 0
        ),
        None,
    )
    if position is None:
        raise HTTPException(status_code=404, detail=f"No open position for {symbol}")

    try:
        snap = await build_market_snapshot(
            symbol, tf, htf, client, limit_hint=s.kline_limit_hint
        )
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=f"MEXC market error: {e}") from e

    market_api = snapshot_to_api_dict(snap)

    # Current SL/TP protection (soft-fail like /api/orders/open: an error
    # means UNKNOWN, not "no stop", so the LLM context says so honestly).
    stops_error: str | None = None
    try:
        stops = await client.open_stop_orders(symbol)
    except ExchangeError as e:
        stops = []
        stops_error = str(e)
    current_sl, current_tp = _extract_position_sl_tp(
        stops, side=position.get("side"), entry_price=position.get("entry_price")
    )

    pnl = position.get("unrealized_pnl")
    im = position.get("im")
    roe_pct: float | None = None
    try:
        if pnl is not None and im not in (None, 0):
            roe_pct = round(float(pnl) / float(im) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        roe_pct = None

    position_ctx = {
        "symbol": symbol,
        "side": position.get("side"),
        "entry_price": position.get("entry_price"),
        "hold_vol": position.get("hold_vol"),
        "leverage": position.get("leverage"),
        "open_type": position.get("open_type"),
        "current_price": market_api.get("last_price"),
        "unrealized_pnl": pnl,
        "roe_pct": roe_pct,
        "liquidate_price": position.get("liquidate_price"),
        "im": im,
        "margin_ratio": position.get("margin_ratio"),
        "stop_loss": current_sl,
        "take_profit": current_tp,
        "stop_orders_known": stops_error is None,
    }

    # Same market context builder as /api/analyze, plus the position on top.
    context = build_llm_context(market_api, acct, s)
    context["position"] = position_ctx

    try:
        result = await reevaluate_with_llm(context, s)
    except LlmError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return {
        "symbol": symbol,
        "tf": tf,
        "htf": htf,
        "position": position_ctx,
        "reevaluation": result.model_dump(),
        "annotations": {
            "advisory_only": True,
            "gates_not_bypassed": True,
            "stop_orders_known": stops_error is None,
        },
    }


@app.get("/api/history")
async def history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    _: None = Depends(require_local_token),
):
    """Recent proposals + orders from SQLite (audit history)."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        return {"proposals": [], "orders": [], "error": "database not initialized"}
    try:
        data = await db.history(limit=limit)
        data["limit"] = limit
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"history read failed: {e}") from e


@app.post("/api/history/clear")
async def history_clear(
    request: Request,
    _: None = Depends(require_local_token),
):
    """Delete all local audit history (proposals + orders). Exchange fills are untouched."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    try:
        deleted = await db.clear_history()
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"history clear failed: {e}") from e


@app.post("/api/orders/preview")
async def orders_preview(
    request: Request,
    ticket: OrderTicket,
    _: None = Depends(require_local_token),
):
    """Validate ticket (risk gates) and issue one-time preview token if ok."""
    ticket = ticket.model_copy(update={"symbol": normalize_symbol(ticket.symbol)})
    svc = _order_service(request)
    try:
        return await svc.preview(ticket)
    except OrderError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors, "message": str(e)}) from e
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/orders/confirm")
async def orders_confirm(
    request: Request,
    body: ConfirmRequest,
    _: None = Depends(require_local_token),
):
    """Consume preview token and place live order (requires TRADING_ENABLED)."""
    svc = _order_service(request)
    try:
        return await svc.confirm(body.token)
    except OrderError as e:
        raise HTTPException(
            status_code=400,
            detail={"errors": e.errors, "message": str(e)},
        ) from e
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/orders/cancel")
async def orders_cancel(
    request: Request,
    body: CancelRequest,
    _: None = Depends(require_local_token),
):
    """Cancel open order by id via MEXC cancel endpoint."""
    svc = _order_service(request)
    oid = body.resolved_order_id()
    try:
        return await svc.cancel(order_id=oid, symbol=body.symbol)
    except OrderError as e:
        raise HTTPException(
            status_code=400,
            detail={"errors": e.errors, "message": str(e)},
        ) from e
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/orders/close")
async def orders_close(
    request: Request,
    body: ClosePositionRequest,
    _: None = Depends(require_local_token),
):
    """Market-close an open position (full or partial). Requires TRADING_ENABLED."""
    svc = _order_service(request)
    symbol = normalize_symbol(body.symbol)
    try:
        return await svc.close_position(
            symbol=symbol, side=body.side, vol=body.vol, fraction=body.fraction
        )
    except OrderError as e:
        raise HTTPException(
            status_code=400,
            detail={"errors": e.errors, "message": str(e)},
        ) from e
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/orders/modify-sl")
async def orders_modify_sl(
    request: Request,
    body: ModifySLRequest,
    _: None = Depends(require_local_token),
):
    """Move the stop-loss of an open position (place new → verify → cancel old).

    Fail-safe: the position is never unprotected during the move. Requires
    TRADING_ENABLED.
    """
    svc = _order_service(request)
    symbol = normalize_symbol(body.symbol)
    try:
        return await svc.modify_stop_loss(
            symbol=symbol, side=body.side, new_sl=body.new_sl
        )
    except OrderError as e:
        raise HTTPException(
            status_code=400, detail={"errors": e.errors, "message": str(e)}
        ) from e
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/api/orders/open")
async def orders_open(
    request: Request,
    symbol: str | None = None,
    _: None = Depends(require_local_token),
):
    """List open orders + open SL/TP trigger orders (for UI list and chart lines)."""
    s = get_settings()
    if not exchange_ready(s):
        return {"orders": [], "stop_orders": [], "error": "Exchange keys not configured"}
    client: MexcClient | None = getattr(request.app.state, "mexc", None)
    if client is None:
        return {"orders": [], "stop_orders": [], "error": "Exchange client not initialized"}
    if symbol:
        symbol = normalize_symbol(symbol)
    try:
        rows = await client.open_orders(symbol)
    except ExchangeError as e:
        return {"orders": [], "stop_orders": [], "error": str(e)}
    stops_error: str | None = None
    try:
        stops = await client.open_stop_orders(symbol)
    except ExchangeError as e:
        # Distinguish "no SL/TP" from "stop-order endpoint broken": an empty
        # list with stops_error=None means genuinely no triggers; a set
        # stops_error means the lookup failed and the UI must NOT show "no SL".
        stops = []
        stops_error = str(e)
    return {
        "orders": rows,
        "stop_orders": stops,
        "stops_error": stops_error,
        "error": None,
    }


@app.post("/api/sizing/suggest")
async def sizing_suggest(
    request: Request,
    ticket: OrderTicket,
    _: None = Depends(require_local_token),
):
    """Suggest contract vol for MAX_RISK_PCT given SL/entry.

    F-09: this must size CONSISTENTLY with the real gate the Preview/Confirm
    path enforces (app.risk.gates.validate_order) — otherwise a suggested
    size can be too large (or geometrically invalid) at the actual gate.
    So the same inputs the gate uses are threaded through here: the market
    adverse-fill slippage buffer, the RISK_SLIPPAGE_PCT buffer on SL
    distance, directional SL geometry, existing same-side risk, available
    margin and the equity-relative notional cap. See suggest_vol() in
    app/risk/sizing.py for the shared clamp math.
    """
    s = get_settings()
    client: MexcClient | None = getattr(request.app.state, "mexc", None)
    if client is None:
        raise HTTPException(status_code=503, detail="MEXC client not initialized")
    symbol = normalize_symbol(ticket.symbol)
    side_l = (ticket.side or "").lower()
    try:
        contract = await client.contract_meta(symbol)
        ticker = await client.ticker(symbol)
        last = float(ticker.last_price or 0)
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    entry = float(ticket.entry or ticket.price or last or 0)
    if (ticket.order_type or "").lower() == "market" and last > 0:
        entry = last
        # Same adverse-fill slippage buffer validate_order applies to
        # entry_for_risk for market orders (G3) — a market suggestion must
        # not assume a friendlier fill than the gate will.
        slip = float(getattr(s, "market_entry_slippage_pct", 0.0) or 0.0)
        if slip > 0 and side_l in ("long", "short"):
            if side_l == "long":
                entry = entry * (1.0 + slip / 100.0)
            else:
                entry = entry * (1.0 - slip / 100.0)
    stop = float(ticket.stop_loss or 0)
    if entry <= 0 or stop <= 0:
        raise HTTPException(status_code=400, detail="entry and stop_loss required")

    equity = 0.0
    available = 0.0
    existing_risk = 0.0
    if exchange_ready(s):
        try:
            snap = await client.account_snapshot()
            equity = float(snap.get("equity_usdt") or 0)
            available = float(snap.get("available_usdt") or 0)
        except ExchangeError:
            equity = 0.0
            available = 0.0
        if equity > 0 and side_l in ("long", "short"):
            try:
                existing_risk = estimate_same_side_risk_usdt(
                    snap.get("positions") or [],
                    symbol=symbol,
                    side=side_l,
                    contract_size=contract.contract_size,
                )
            except ValueError as e:
                # Fail-closed, same as preview/confirm: unknown same-side
                # exposure must never be silently treated as 0 risk.
                raise HTTPException(status_code=400, detail=str(e)) from e
    if equity <= 0:
        raise HTTPException(status_code=400, detail="equity unavailable for sizing")

    # Honor the client-requested risk %, but never let it exceed the gate's
    # max_risk_pct — the suggestion must never label a size as "X% risk"
    # while actually sizing for more than X% (or more than the hard cap).
    requested_risk = ticket.risk_pct
    if requested_risk is None or requested_risk <= 0:
        effective_risk = s.max_risk_pct
    else:
        effective_risk = min(float(requested_risk), s.max_risk_pct)

    vol = suggest_vol(
        equity,
        effective_risk,
        contract.contract_size,
        entry,
        stop,
        contract.vol_unit,
        contract.min_vol,
        side=side_l,
        slippage_pct=s.risk_slippage_pct,
        existing_risk_usdt=existing_risk,
        available_usdt=available,
        leverage=ticket.leverage,
        max_notional_pct_of_equity=s.max_notional_pct_of_equity,
    )
    notional = vol * contract.contract_size * entry
    coin = vol * contract.contract_size
    return {
        "vol": vol,
        "notional_usdt": notional,
        "base_amount": coin,
        "contract_size": contract.contract_size,
        "entry": entry,
        "stop_loss": stop,
        "equity_usdt": equity,
        "available_usdt": available,
        "existing_same_side_risk_usdt": existing_risk,
        "risk_pct": effective_risk,
        "max_risk_pct": s.max_risk_pct,
    }


@app.websocket("/ws/market")
async def ws_market(
    websocket: WebSocket,
    symbol: str = Query("BTC"),
    tf: str = Query("15m"),
):
    """Realtime market stream (Hyperliquid public WS proxied to browser)."""
    # Origin check: a browser always sends Origin. Reject any cross-origin
    # website so an arbitrary page cannot open this socket and drive the
    # Hyperliquid proxy. Non-browser clients (no Origin header) are allowed.
    origin = websocket.headers.get("origin")
    if origin is not None:
        from urllib.parse import urlparse

        origin_host = (urlparse(origin).hostname or "").lower()
        if origin_host not in ("127.0.0.1", "localhost", "::1"):
            await websocket.close(code=1008)
            return
    await websocket.accept()
    s = get_settings()
    try:
        symbol = normalize_symbol(symbol)
    except HTTPException as e:
        await websocket.send_json(
            {"type": "status", "status": "error", "error": str(e.detail)}
        )
        await websocket.close()
        return

    if s.exchange == "hyperliquid":
        from app.realtime.hl_proxy import proxy_hyperliquid_market

        try:
            await proxy_hyperliquid_market(
                websocket, s, symbol=symbol, tf=tf
            )
        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await websocket.send_json(
                    {"type": "status", "status": "error", "error": str(e)}
                )
            except Exception:
                pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
        return

    # MEXC: fast REST poll fallback until native WS is wired
    await websocket.send_json(
        {
            "type": "status",
            "status": "poll_fallback",
            "exchange": "mexc",
            "message": "MEXC native WS not wired — use HTTP poll",
        }
    )
    client = getattr(websocket.app.state, "mexc", None) or getattr(
        websocket.app.state, "exchange", None
    )
    try:
        while True:
            if client is None:
                await websocket.send_json(
                    {"type": "status", "status": "error", "error": "no client"}
                )
                break
            try:
                t = await client.ticker(symbol)
                await websocket.send_json(
                    {
                        "type": "mid",
                        "coin": symbol,
                        "px": float(t.last_price),
                        "time": t.timestamp,
                    }
                )
            except Exception as e:
                await websocket.send_json(
                    {"type": "status", "status": "error", "error": str(e)}
                )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass


@app.post("/api/scan")
async def market_scan(
    request: Request,
    body: dict | None = None,
    _: None = Depends(require_local_token),
):
    """Screen the top-volume coins for tradeable setups (cheap model, 1 call).

    Deep per-coin analysis stays on /api/analyze with LLM_PROVIDER.
    """
    from app.llm.scanner import build_scan_contexts, scan_with_llm

    s = get_settings()
    client = getattr(request.app.state, "mexc", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Exchange client not initialized")
    body = body or {}
    tf = str(body.get("tf") or "15m")
    htf = str(body.get("htf") or "1H")

    try:
        overview = await client.market_overview(s.scanner_max_coins)
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=f"market overview failed: {e}") from e
    if not overview:
        raise HTTPException(status_code=502, detail="no market overview data")

    contexts, fetch_errors = await build_scan_contexts(client, overview, tf, htf)
    if not contexts:
        raise HTTPException(
            status_code=502,
            detail={"message": "no coin data for scan", "errors": fetch_errors},
        )

    try:
        results, model_used = await scan_with_llm(contexts, s)
    except LlmError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return {
        "results": [r.model_dump() for r in results],
        "scanned": [c["symbol"] for c in contexts],
        "model_used": model_used,
        "tf": tf,
        "htf": htf,
        "errors": fetch_errors,
        "note": "Screening only — open a coin for the full analysis before trading.",
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if _setup_needed():
        return RedirectResponse("/setup", status_code=303)
    s = get_settings()
    resp = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "default_symbol": s.default_symbol,
            "trading_enabled": s.trading_enabled,
            "max_leverage": s.max_leverage,
            "exchange": s.exchange,
            "hl_testnet": s.hl_testnet if s.exchange == "hyperliquid" else False,
        },
    )
    # F-19: carry the local auth token in an HttpOnly, SameSite=Strict cookie
    # instead of the page DOM. Same-origin fetch/WebSocket send it
    # automatically; the X-Local-Token header stays a valid fallback. Secure
    # is not required on plain-http 127.0.0.1.
    token = (s.local_api_token or "").strip()
    if token:
        resp.set_cookie(
            AUTH_COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            path="/",
        )
    # F-20: strict CSP. The dashboard has no inline <script> (F-19 removed the
    # token script), so script-src is a plain 'self' — no nonce needed.
    resp.headers["Content-Security-Policy"] = build_csp()
    return resp


