import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

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
from app.llm.client import LlmError, analyze_with_llm, build_llm_context

# Back-compat alias
GrokError = LlmError
from app.mexc.client import empty_account
from app.mexc.errors import MexcError
from app.models import (
    AnalyzeRequest,
    CancelRequest,
    ClosePositionRequest,
    ConfirmRequest,
    OrderTicket,
)
from app.orders.service import OrderError, OrderService
from app.orders.tokens import PreviewStore
from app.risk.sizing import suggest_vol
from app.security import (
    loopback_or_token_middleware,
    normalize_symbol,
    require_local_token,
)

ExchangeError = (MexcError, HyperliquidError)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
STATIC_DIR = BASE / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    client = create_exchange_client(s)
    db = Database(s.database_path)
    await db.init()
    store = PreviewStore()
    app.state.mexc = client  # legacy name: active exchange client
    app.state.exchange = client
    app.state.db = db
    app.state.preview_store = store
    # App-global lock: a new OrderService is built per request, so the lock
    # that serializes confirm/close must live here, not on the instance.
    import asyncio as _asyncio

    app.state.trade_lock = _asyncio.Lock()
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
    return {
        "ok": True,
        "live_trading": True,
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
    return templates.TemplateResponse(request, "setup.html", {})


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

    context = build_llm_context(market_api, acct, s)

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
    """Suggest contract vol for MAX_RISK_PCT given SL/entry."""
    s = get_settings()
    client: MexcClient | None = getattr(request.app.state, "mexc", None)
    if client is None:
        raise HTTPException(status_code=503, detail="MEXC client not initialized")
    symbol = normalize_symbol(ticket.symbol)
    try:
        contract = await client.contract_meta(symbol)
        ticker = await client.ticker(symbol)
        last = float(ticker.last_price or 0)
    except ExchangeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    entry = float(ticket.entry or ticket.price or last or 0)
    if (ticket.order_type or "").lower() == "market" and last > 0:
        entry = last
    stop = float(ticket.stop_loss or 0)
    if entry <= 0 or stop <= 0:
        raise HTTPException(status_code=400, detail="entry and stop_loss required")

    equity = 0.0
    if exchange_ready(s):
        try:
            snap = await client.account_snapshot()
            equity = float(snap.get("equity_usdt") or 0)
        except ExchangeError:
            equity = 0.0
    if equity <= 0:
        raise HTTPException(status_code=400, detail="equity unavailable for sizing")

    vol = suggest_vol(
        equity,
        s.max_risk_pct,
        contract.contract_size,
        entry,
        stop,
        contract.vol_unit,
        contract.min_vol,
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
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "default_symbol": s.default_symbol,
            "trading_enabled": s.trading_enabled,
            "max_leverage": s.max_leverage,
            "exchange": s.exchange,
            "hl_testnet": s.hl_testnet if s.exchange == "hyperliquid" else False,
            # Browser UI must send X-Local-Token when LOCAL_API_TOKEN is set
            "local_api_token": s.local_api_token or "",
        },
    )


