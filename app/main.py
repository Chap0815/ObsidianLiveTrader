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
from app.llm.client import (
    LlmError,
    _sanitize_scanner_verdict,
    analyze_with_llm,
    build_llm_context,
    reevaluate_with_llm,
)

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
from app.orders.protection import classify_protection
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

# /api/analyze in-memory result cache TTL (LLM-credit saver). Advisory only —
# never consulted by the order/gate path (see analyze() below).
ANALYZE_CACHE_TTL_S = 120.0
# Simple size cap so a long-running process (many symbols/tf/htf/provider/
# verdict combinations) can't grow this dict unbounded — the oldest entry
# (by insertion order) is dropped once the cache exceeds this many entries.
ANALYZE_CACHE_MAX_ENTRIES = 256

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


# Q-02: exclusive startup lock on the data directory. This is a runtime
# check (rather than the env-var heuristic above) so it also catches a bare
# `--workers N` / `gunicorn -w N` launch, which sets none of those vars.
# Implemented as a PID lockfile created with O_CREAT|O_EXCL (atomic
# create-if-absent on both POSIX and Windows via Python's os.open) instead
# of an OS advisory lock, specifically so a lock left behind by a process
# that was killed (SIGKILL/taskkill, no chance to run the `finally` below)
# can be told apart from one held by a still-running instance and reclaimed
# instead of blocking every future start — see _pid_is_alive() below.
_INSTANCE_LOCK_FILENAME = "instance.lock"


def _pid_is_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness check for a PID read from a
    lockfile. os.kill(pid, 0) (the usual POSIX idiom) is not meaningful on
    Windows, so branch on platform."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by another user
    except OSError:
        return False
    return True


def _acquire_instance_lock(data_dir: Path) -> Path | None:
    """Try to take the exclusive startup lock in data_dir.

    Returns the lockfile Path if THIS process now holds it, or None if a
    still-live process already holds it. A lock left behind by a dead PID
    (crashed process) is treated as orphaned/stale and silently reclaimed —
    it must never permanently block a normal single-instance start.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / _INSTANCE_LOCK_FILENAME
    my_pid = os.getpid()

    if lock_path.exists():
        other_pid = -1
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
            if raw:
                other_pid = int(raw)
        except (OSError, ValueError):
            other_pid = -1
        if other_pid > 0 and other_pid != my_pid and _pid_is_alive(other_pid):
            return None  # held by another live process
        # Orphaned (dead PID) or unreadable content: reclaim it.
        try:
            lock_path.unlink()
        except OSError:
            pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        # Lost a race right after the staleness check above.
        return None
    try:
        os.write(fd, str(my_pid).encode("ascii"))
    finally:
        os.close(fd)
    return lock_path


def _release_instance_lock(lock_path: Path) -> None:
    """Release a lock THIS process holds. Verifies the PID recorded inside
    still matches ours before deleting, so a lock some other process may
    have reclaimed is never deleted out from under it."""
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        if raw and int(raw) == os.getpid():
            lock_path.unlink()
    except (OSError, ValueError):
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()

    # W3-01: Mainnet-Echtgeld-Schutz (fail-closed). Der Wechsel von Testnet auf
    # Echtgeld ist sonst ein stilles HL_TESTNET=false, während TRADING_ENABLED=
    # true aus der Testnet-Phase scharf bleibt. Bis der Nutzer den Wechsel
    # einmalig ausdrücklich bestätigt, wird ein scharfer Mainnet-Start abgelehnt.
    # Bewusst hier (Startup) statt als pydantic-Validator: Settings() muss für
    # disarmte Inspektion/health/Tests konstruierbar bleiben. Nur Hyperliquid hat
    # ein Testnet — bei MEXC (kein Testnet) ist dieses Gate ein No-op. Raise vor
    # jeder Ressourcen-Akquise (Lock/DB), daher kein Cleanup nötig.
    _hl_mainnet = s.exchange == "hyperliquid" and not s.hl_testnet
    if _hl_mainnet and s.trading_enabled and not s.mainnet_ack:
        raise RuntimeError(
            "Mainnet + scharfes Trading erkannt (HL_TESTNET=false, "
            "TRADING_ENABLED=true) — zur Bestätigung einmalig MAINNET_ACK=true "
            "in die .env setzen. Empfohlen vorher: Trading disarmen "
            "(TRADING_ENABLED=false), Agent-Key-Scope prüfen und mit einer "
            "Micro-Probe testen, dann wieder armen. Siehe README, Abschnitt "
            "„Wechsel auf Mainnet“."
        )
    # Loud real-money startup notice: armed on mainnet (HL live) or any armed
    # MEXC (MEXC has no testnet). Mirrors the /api/health live_trading flag.
    if s.trading_enabled and not (s.exchange == "hyperliquid" and s.hl_testnet):
        log.warning(
            "MAINNET · ECHTGELD AKTIV: scharfes Trading auf %s — Orders bewegen "
            "echtes Kapital.",
            s.exchange,
        )

    client = create_exchange_client(s)
    db = Database(s.database_path)

    # Q-02: exclusive startup lock, independent of the env-var heuristic
    # further down (_detect_multi_worker_env) — this also catches a bare
    # `--workers N` / `gunicorn -w N` launch, which sets none of those env
    # vars. A second LIVE instance gets a loud WARNING; if THIS instance is
    # armed (TRADING_ENABLED=true) it aborts fail-closed instead of quietly
    # running two copies of the in-process preview_store/trade_lock state
    # described below. The existing env-var guard stays in place too.
    instance_lock_path = _acquire_instance_lock(db.path.parent)
    if instance_lock_path is None:
        log.warning(
            "MULTI-INSTANCE DETECTED: another live process already holds "
            "the startup lock in %s (in-process preview-token store and "
            "confirm/close trade_lock are NOT shared across processes — "
            "see the single-worker note on _detect_multi_worker_env() "
            "above). Running two live instances against the same data/ is "
            "unsafe.",
            db.path.parent,
        )
        if s.trading_enabled:
            raise RuntimeError(
                "Refusing to start: TRADING_ENABLED=true and another live "
                f"instance already holds the startup lock in {db.path.parent}."
            )

    # Ab hier haelt dieser Prozess ggf. den Instanz-Lock: jede Exception vor
    # dem yield (z.B. db.init()) erreicht das finally unten nie und muss den
    # Lock explizit freigeben — sonst bleibt nur die implizite
    # Staleness-Reclaim-Heilung beim naechsten Start.
    try:
        await db.init()
        # Q-03: open the long-lived shared aiosqlite connection AFTER init and
        # under the same lock-release guard. Single-worker invariant makes one
        # shared connection safe; methods fall back to per-call connections if
        # this is ever skipped (see Database._acquire). Closed in the finally.
        await db.open()
    except BaseException:
        await db.close()
        if instance_lock_path is not None:
            _release_instance_lock(instance_lock_path)
        raise
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
    # /api/analyze result cache (see analyze() below) — fresh/empty on every
    # process start, same as the other in-memory caches on this state object.
    app.state.analyze_cache = {}
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
    # Singleflight lock for /api/analyze: mirrors news_lock above — two
    # concurrent identical cache-miss requests must trigger only ONE LLM
    # call, not two (see analyze() below).
    app.state.analyze_lock = _asyncio.Lock()
    multi_worker_warning = _detect_multi_worker_env()
    if multi_worker_warning:
        log.warning(multi_worker_warning)

    # Loud startup notice when the CONFIGURED analysis provider isn't usable and
    # a different one is silently doing the work (e.g. LLM_PROVIDER=claude with no
    # ANTHROPIC_API_KEY → analysis actually runs on xai/Grok). Without this the
    # user believes they are on the configured model when they are not.
    try:
        resolved = s.resolved_llm_provider
        if resolved != s.llm_provider:
            log.warning(
                "LLM-FALLBACK AKTIV: LLM_PROVIDER=%s ist nicht einsatzbereit "
                "(kein API-Key / nicht konfiguriert) — die KI-Analyse läuft "
                "stattdessen auf '%s'. Trage den passenden API-Key in die .env "
                "ein, um den gewünschten Provider (z.B. Sonnet) zu nutzen.",
                s.llm_provider,
                resolved,
            )
    except Exception:  # noqa: BLE001 — a notice must never break startup
        pass

    # Journal shadow-outcome resolver: single background task, cancelled on
    # shutdown. Advisory/measurement only — never places/moves/cancels orders.
    # Guarded so a resolver failure can never break app startup.
    resolver_task = None
    if getattr(s, "journal_enabled", True):
        try:
            from app.journal.resolver import run_resolver_loop

            resolver_task = _asyncio.create_task(run_resolver_loop(app))
        except Exception:
            log.warning("journal resolver failed to start", exc_info=True)
    try:
        yield
    finally:
        if resolver_task is not None:
            resolver_task.cancel()
            try:
                await resolver_task
            except (_asyncio.CancelledError, Exception):
                pass
        aclose = getattr(client, "aclose", None)
        if aclose:
            await aclose()
        # Q-03: close the shared DB connection opened above.
        await db.close()
        if instance_lock_path is not None:
            _release_instance_lock(instance_lock_path)


app = FastAPI(title="Obsidian Live Trader", version="0.3.0", lifespan=lifespan)
app.middleware("http")(loopback_or_token_middleware)
# DNS-rebinding guard: only loopback host headers are served
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "::1", "testserver"],
)

# B-01: private JSON responses must never be cached (browser/proxy disk
# cache), MIME-sniffed, or leak the request path via Referer to a
# downstream link. Appended AFTER the auth/CSRF and TrustedHost middleware
# above (nothing reordered) — this only sets headers on the way out and
# never influences an auth/origin decision. HTML pages keep their own CSP
# (see index()/setup_page() below) — untouched here.
_API_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def api_response_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        for name, value in _API_NO_STORE_HEADERS.items():
            response.headers[name] = value
    return response


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _exchange_client(request: Request):
    """Q-07: single resolution of the active exchange client.

    Some call-sites only checked app.state.mexc while others fell back to
    app.state.exchange (the same object under lifespan's legacy alias, see
    lifespan() above) — harmless today, but a divergence waiting to bite the
    day those two are ever set differently. Every call-site now goes through
    this one helper.
    """
    return getattr(request.app.state, "mexc", None) or getattr(
        request.app.state, "exchange", None
    )


def _order_service(request: Request) -> OrderService:
    s = get_settings()
    client = _exchange_client(request)
    if client is None:
        raise HTTPException(status_code=503, detail="Exchange client not initialized")
    store = getattr(request.app.state, "preview_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Preview store not initialized")
    db = getattr(request.app.state, "db", None)
    lock = getattr(request.app.state, "trade_lock", None)
    return OrderService(client, s, store, db=db, trade_lock=lock)


def _llm_settings(request: Request):
    """Settings with the runtime LLM hot-swap override applied (no restart),
    else auto-resolved to a CONFIGURED provider — so a 'claude' default with no
    Anthropic key doesn't fail the analysis when another provider is set up."""
    s = get_settings()
    override = getattr(request.app.state, "llm_override", None)
    provider = override or s.resolved_llm_provider
    if provider != s.llm_provider:
        return s.model_copy(update={"llm_provider": provider})
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
        # Surface a silent fallback: the user CONFIGURED one provider (e.g.
        # claude) but it has no key, so analysis actually runs on another
        # (resolved) provider. Without this the dashboard would imply the
        # configured model is in use when it is not.
        "llm_provider_configured": s.llm_provider,
        "llm_fallback_active": eff.llm_provider != s.llm_provider,
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
        "provider_configured": s.llm_provider,
        "fallback_active": eff.llm_provider != s.llm_provider,
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


# ── First-run setup ─────────────────────────────────────────────────
# The open, unauthenticated /setup window exists ONLY while setup is
# incomplete (SETUP_COMPLETE marker) — i.e. before any real secret is
# stored. Once complete it locks; the only write path is the
# authenticated /api/settings/* endpoints.
from app.config import ROOT as _ROOT  # noqa: E402
from app.env_builder import build_full_env, normalize_answers  # noqa: E402

ENV_PATH = _ROOT / ".env"


def _setup_needed() -> bool:
    if not ENV_PATH.exists():
        return True
    try:
        return not get_settings().setup_complete
    except Exception:
        # .env EXISTS but can't be parsed: fail CLOSED (audit L-1). Re-opening
        # the unauthenticated /setup write path on a configured-but-broken
        # install would let a local process overwrite keys; a broken .env must
        # be fixed/deleted manually instead.
        return False


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
            detail="Setup bereits abgeschlossen (SETUP_COMPLETE) — gesperrt. KI-Keys über die authentifizierten Einstellungen ändern.",
        )
    try:
        answers = normalize_answers(body or {})
        content = build_full_env(answers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Validate by parsing before it becomes the real .env
    tmp = ENV_PATH.with_name(".env.setup-tmp")
    tmp.write_text(content, encoding="utf-8")
    # B-08: schon die tmp-Datei traegt Secrets — Rechte VOR dem Validieren
    # einschraenken, dann atomar ersetzen (replace erhaelt die ACL der Quelle).
    from app.env_builder import restrict_env_permissions

    restrict_env_permissions(tmp)
    try:
        Settings(_env_file=str(tmp))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Konfiguration ungültig: {e}") from e
    import os as _os

    _os.replace(tmp, ENV_PATH)
    restrict_env_permissions(ENV_PATH)

    # Hot-apply: fresh settings + fresh exchange client, no restart needed
    get_settings.cache_clear()
    s = get_settings()
    old = _exchange_client(request)
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


@app.post("/api/setup/test-provider")
async def setup_test_provider(request: Request, body: dict):
    """Read-only KI-key ping during setup. Transient key, never persisted.

    SSRF-safe: cloud hosts are pinned in app.llm.probe; Ollama uses the
    server-side loopback-validated base URL.
    """
    if not _setup_needed():
        raise HTTPException(status_code=403, detail="Setup abgeschlossen — gesperrt.")
    from app.llm.probe import probe_provider

    provider = str((body or {}).get("provider") or "").strip().lower()
    api_key = str((body or {}).get("api_key") or "").strip()
    model = str((body or {}).get("model") or "").strip()
    return await probe_provider(
        provider,
        api_key=api_key,
        model=model,
        ollama_base_url=get_settings().ollama_base_url,
    )


# ── Post-setup KI-key management (authenticated) ─────────────────────
# /api/settings/* is in private_prefixes (loopback+token) and every route
# below depends on require_local_token. Writes go through the whitelist-only
# atomic patcher; stored secrets are NEVER returned to the client.
_PROVIDER_LABELS = {
    "claude": "Claude Opus",
    "xai": "Grok",
    "openai": "Codex",
    "ollama": "Ollama (lokal)",
}


def _settings_llm_status(request: Request) -> dict:
    from app.env_builder import LLM_KEY_VARS

    s = get_settings()
    eff = _llm_settings(request)
    configured = {
        "claude": s.claude_ready,
        "xai": s.xai_ready,
        "openai": s.openai_ready,
        "ollama": s.ollama_ready,
    }
    models = {
        "claude": s.anthropic_model,
        "xai": s.xai_model,
        "openai": s.openai_model,
        "ollama": s.ollama_model,
    }
    providers = [
        {
            "id": pid,
            "label": _PROVIDER_LABELS[pid],
            "configured": bool(configured[pid]),
            "model": models[pid],
        }
        for pid in LLM_KEY_VARS
    ]
    return {"providers": providers, "active": eff.llm_provider}


@app.get("/api/settings/llm")
async def settings_llm_get(
    request: Request, _: None = Depends(require_local_token)
) -> dict:
    """KI-Keys panel status. No secrets returned — only configured + model."""
    return _settings_llm_status(request)


@app.post("/api/settings/llm-key")
async def settings_llm_key(
    request: Request, body: dict, _: None = Depends(require_local_token)
) -> dict:
    """Append/update ONE provider's key + model. Whitelist-only, atomic.

    Never changes LLM_PROVIDER (that stays the job of POST /api/llm) and never
    echoes the key. api_key empty leaves the existing key line untouched.
    """
    from app.env_builder import (
        DEFAULT_MODELS,
        LLM_KEY_VARS,
        SETTINGS_LLM_WRITABLE,
        patch_env_vars,
    )

    provider = str((body or {}).get("provider") or "").strip().lower()
    aliases = {"anthropic": "claude", "grok": "xai", "codex": "openai", "local": "ollama"}
    provider = aliases.get(provider, provider)
    if provider not in LLM_KEY_VARS:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: claude, xai, openai, ollama",
        )
    key_var, model_var = LLM_KEY_VARS[provider]
    api_key = str((body or {}).get("api_key") or "").strip()
    model = str((body or {}).get("model") or "").strip()

    s = get_settings()
    current_model = {
        "claude": s.anthropic_model,
        "xai": s.xai_model,
        "openai": s.openai_model,
        "ollama": s.ollama_model,
    }[provider]

    updates: dict[str, str] = {}
    if key_var is not None and api_key:
        updates[key_var] = api_key
    if model:
        updates[model_var] = model
    elif not (current_model or "").strip():
        updates[model_var] = DEFAULT_MODELS[provider]

    if updates:
        try:
            patch_env_vars(
                ENV_PATH, updates, allowed=set(SETTINGS_LLM_WRITABLE)
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        get_settings.cache_clear()

    return _settings_llm_status(request)


@app.post("/api/settings/test-provider")
async def settings_test_provider(
    request: Request, body: dict, _: None = Depends(require_local_token)
) -> dict:
    """Authenticated read-only probe. If api_key omitted, use the STORED key
    (server-side; never returned)."""
    from app.env_builder import LLM_KEY_VARS
    from app.llm.probe import probe_provider

    provider = str((body or {}).get("provider") or "").strip().lower()
    aliases = {"anthropic": "claude", "grok": "xai", "codex": "openai", "local": "ollama"}
    provider = aliases.get(provider, provider)
    if provider not in LLM_KEY_VARS:
        raise HTTPException(
            status_code=400,
            detail="provider must be one of: claude, xai, openai, ollama",
        )
    api_key = str((body or {}).get("api_key") or "").strip()
    model = str((body or {}).get("model") or "").strip()
    s = get_settings()
    if not api_key:
        api_key = {
            "claude": s.anthropic_api_key,
            "xai": s.xai_api_key,
            "openai": s.openai_api_key,
            "ollama": "",
        }[provider]
    return await probe_provider(
        provider, api_key=api_key, model=model, ollama_base_url=s.ollama_base_url
    )


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
    client = _exchange_client(request)
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
    client: MexcClient | None = _exchange_client(request)
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

    client = _exchange_client(request)
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

    client = _exchange_client(request)
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
    client = _exchange_client(request)
    if client is None or not hasattr(client, "user_fills"):
        return {"fills": [], "supported": False, "error": None}
    if symbol:
        symbol = normalize_symbol(symbol)
    try:
        rows = await client.user_fills(symbol=symbol, limit=limit)
    except ExchangeError as e:
        return {"fills": [], "supported": True, "error": str(e)}
    return {"fills": rows, "supported": True, "error": None}


def _scanner_verdict_cache_key(raw: dict | None) -> tuple | None:
    """Stable, hashable representation of a scanner_verdict for the analyze
    cache key. Reuses the same sanitizer the LLM context itself uses
    (_sanitize_scanner_verdict) so the key reflects exactly what would be
    sent to the model: absent/unusable verdict -> None (its own cache
    bucket, distinct from any present verdict); a present verdict -> a
    tuple of its sanitized (bias, setup, key_level, score), rounded so
    float jitter doesn't fragment the cache."""
    verdict = _sanitize_scanner_verdict(raw)
    if verdict is None:
        return None
    try:
        key_level = round(float(verdict.get("key_level") or 0), 6)
    except (TypeError, ValueError):
        key_level = 0.0
    try:
        score = round(float(verdict.get("score") or 0), 3)
    except (TypeError, ValueError):
        score = 0.0
    return (verdict.get("bias"), verdict.get("setup"), key_level, score)


_JOURNAL_LONG_ACTIONS = frozenset({"BUY", "STRONG_BUY"})
_JOURNAL_SHORT_ACTIONS = frozenset({"SELL", "STRONG_SHORT"})


def _journal_direction(action: str | None) -> str | None:
    """long for BUY/STRONG_BUY, short for SELL/STRONG_SHORT, None for STAY_OUT."""
    if action in _JOURNAL_LONG_ACTIONS:
        return "long"
    if action in _JOURNAL_SHORT_ACTIONS:
        return "short"
    return None


def _journal_scanner_summary(raw: dict | None) -> str | None:
    """Compact 'bias/setup/score' string from the sanitized scanner_verdict."""
    verdict = _sanitize_scanner_verdict(raw)
    if not verdict:
        return None
    parts = [
        str(verdict.get("bias") or "?"),
        str(verdict.get("setup") or "?"),
        str(verdict.get("score")) if verdict.get("score") is not None else "?",
    ]
    return "/".join(parts)


def _journal_model_for_provider(s: Settings) -> str | None:
    """Resolve the model string for the effective provider (advisory context)."""
    provider = (s.llm_provider or "").strip().lower()
    return {
        "claude": s.anthropic_model,
        "anthropic": s.anthropic_model,
        "xai": s.xai_model,
        "grok": s.xai_model,
        "openai": s.openai_model,
        "codex": s.openai_model,
        "ollama": s.ollama_model,
        "local": s.ollama_model,
    }.get(provider)


def _journal_status_for(action: str | None, entry, sl, tp1) -> str:
    """SKIPPED for STAY_OUT (never resolvable) or a non-STAY_OUT proposal that
    is missing entry/sl/tp1 (degenerate — cannot be shadow-resolved). Else
    PENDING (the resolver will pick it up)."""
    if action == "STAY_OUT":
        return "SKIPPED"
    if entry is None or sl is None or tp1 is None:
        return "SKIPPED"
    return "PENDING"


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

    client: MexcClient | None = _exchange_client(request)
    if client is None:
        raise HTTPException(status_code=503, detail="MEXC client not initialized")

    symbol = normalize_symbol(body.symbol or s.default_symbol)
    tf = body.tf or "15m"
    htf = body.htf or "1H"

    # Analysis caching (LLM-credit saver): key includes the RESOLVED provider
    # (so hot-swapping the KI dropdown never serves a stale other-provider
    # result) AND the sanitized scanner_verdict (so a verdict-less manual
    # analyze and a scan-triggered analyze for the same coin/tf/htf/provider
    # never share a cache entry — that would silently serve a
    # verdict-mismatched proposal for up to the TTL, re-opening the
    # scanner<->analyzer B1 handoff gap). Advisory only — this cache never
    # touches order/gate paths; confirm always re-runs its own gates on live
    # price. Only SUCCESSFUL proposals are cached (errors are never cached);
    # STAY_OUT is a valid, cacheable result.
    verdict_key = _scanner_verdict_cache_key(body.scanner_verdict)
    cache_key = (symbol, tf, htf, s.llm_provider, verdict_key)

    def _cache_lookup() -> dict | None:
        cache: dict = getattr(request.app.state, "analyze_cache", None) or {}
        entry = cache.get(cache_key)
        if entry is None:
            return None
        cached_at, cached_response = entry
        age = _time.monotonic() - cached_at
        if age >= ANALYZE_CACHE_TTL_S:
            return None
        resp = dict(cached_response)
        resp["cached"] = True
        resp["cached_age_s"] = int(age)
        return resp

    if not body.force:
        hit = _cache_lookup()
        if hit is not None:
            return hit

    async def _run_analyze() -> dict:
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
            proposal_id: int | None = None
            try:
                proposal_id = await db.insert_proposal(
                    symbol=symbol,
                    proposal_json=proposal_dict,
                    annotations_json=annotations,
                    context_hash=context_hash,
                )
            except Exception:
                # Do not break advisory analyze if SQLite is unavailable
                pass

            # Journal (KI shadow book) — SAME soft-fail posture. A journal write
            # must NEVER break analyze: it is advisory/measurement only and never
            # touches the order/gate/confirm path. STAY_OUT and level-less rows
            # are logged as SKIPPED (see _journal_status_for); everything else
            # starts PENDING for the background resolver.
            if getattr(s, "journal_enabled", True):
                try:
                    action = proposal_dict.get("action")
                    status = _journal_status_for(
                        action,
                        proposal.entry_price,
                        proposal.stop_loss,
                        proposal.tp1,
                    )
                    await db.insert_journal_entry(
                        symbol=symbol,
                        tf=tf,
                        htf=htf,
                        action=str(action),
                        direction=_journal_direction(action),
                        setup_confidence=str(proposal_dict.get("setup_confidence") or "medium"),
                        entry_price=proposal.entry_price,
                        stop_loss=proposal.stop_loss,
                        tp1=proposal.tp1,
                        rrr=proposal.rrr,
                        provider=s.llm_provider,
                        model=_journal_model_for_provider(s),
                        scanner_summary=_journal_scanner_summary(body.scanner_verdict),
                        last_price_t0=market_api.get("last_price"),
                        status=status,
                        proposal_id=proposal_id,
                    )
                except Exception:
                    # Journal is advisory — never break analyze on a write error.
                    pass

        response = {
            "symbol": symbol,
            "tf": tf,
            "htf": htf,
            "proposal": proposal_dict,
            "annotations": annotations,
            "last_price": market_api.get("last_price"),
        }
        cache = getattr(request.app.state, "analyze_cache", None)
        if cache is None:
            cache = {}
            request.app.state.analyze_cache = cache
        cache[cache_key] = (_time.monotonic(), dict(response))
        # Simple size cap (O-ish growth bound): drop the oldest entry (by
        # insertion order) once the cache exceeds the cap.
        if len(cache) > ANALYZE_CACHE_MAX_ENTRIES:
            oldest_key = next(iter(cache))
            if oldest_key != cache_key:
                del cache[oldest_key]
        response["cached"] = False
        return response

    if body.force:
        return await _run_analyze()

    # Singleflight on a cache MISS (mirrors /api/news's news_lock): two
    # concurrent identical requests must trigger only ONE LLM call. Acquire
    # the lock, then RE-CHECK the cache — another caller may have already
    # populated it while this one was waiting.
    lock = getattr(request.app.state, "analyze_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.analyze_lock = lock
    async with lock:
        hit = _cache_lookup()
        if hit is not None:
            return hit
        return await _run_analyze()


def _extract_position_sl_tp(
    stops: list[dict], *, side: str | None = None, entry_price: float | None = None
) -> tuple[float | None, float | None]:
    """Best-effort current SL/TP from open trigger orders for one symbol.

    Thin wrapper over the shared backend classifier
    (`app.orders.protection.classify_protection`) — the single source of truth
    both this reevaluate path and the auto-flatten verifier
    (`OrderService._verify_sl_attached`) share (Q-05). Read-only.
    """
    return classify_protection(stops, side=side, entry=entry_price)


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

    client: MexcClient | None = _exchange_client(request)
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
    except Exception:
        # B-03: never leak the raw exception (can contain file paths/SQL) to
        # the client; the detail goes to the server log only.
        log.exception("history read failed")
        raise HTTPException(status_code=500, detail="internal error") from None


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
    except Exception:
        log.exception("history clear failed")
        raise HTTPException(status_code=500, detail="internal error") from None


# ── Journal + feedback-loop (KI shadow book) ────────────────────────────
# Read-only measurement endpoints. Private (loopback + require_local_token),
# same posture as /api/history. NEVER touches the order/gate/confirm path.


@app.get("/api/journal")
async def journal_list(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(require_local_token),
):
    """Recent journal entries (KI shadow book), newest first."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        return {"entries": [], "limit": limit, "error": "database not initialized"}
    try:
        rows = await db.recent_journal(limit=limit)
        return {"entries": rows, "limit": limit}
    except Exception:
        log.exception("journal read failed")
        raise HTTPException(status_code=500, detail="internal error") from None


@app.get("/api/journal/stats")
async def journal_stats_endpoint(
    request: Request,
    _: None = Depends(require_local_token),
):
    """Aggregate shadow-outcome stats: win rate + Wilson CI + avg realized R,
    STAY_OUT rate, and breakdowns by confidence/action/provider."""
    from app.journal.stats import build_stats_response

    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        return build_stats_response({}, min_sample=get_settings().journal_min_sample)
    try:
        raw = await db.journal_stats()
        return build_stats_response(raw, min_sample=get_settings().journal_min_sample)
    except Exception:
        log.exception("journal stats failed")
        raise HTTPException(status_code=500, detail="internal error") from None


@app.post("/api/journal/clear")
async def journal_clear(
    request: Request,
    _: None = Depends(require_local_token),
):
    """Delete all journal_entries (KI shadow book). Deliberately SEPARATE from
    /api/history/clear — the journal is the measurement dataset and survives
    a history reset; this is the explicit opt-in to wipe it."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    try:
        deleted = await db.clear_journal()
        return {"ok": True, "deleted": deleted}
    except Exception:
        log.exception("journal clear failed")
        raise HTTPException(status_code=500, detail="internal error") from None


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
    client: MexcClient | None = _exchange_client(request)
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
    client: MexcClient | None = _exchange_client(request)
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
    existing_risk_warnings: list[str] = []
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
                existing_risk, existing_risk_warnings = estimate_same_side_risk_usdt(
                    snap.get("positions") or [],
                    symbol=symbol,
                    side=side_l,
                    contract_size=contract.contract_size,
                    strict=bool(getattr(s, "strict_aggregate_risk", False)),
                    pos_risk_cap_pct=float(
                        getattr(s, "aggregate_pos_risk_cap_pct", 2.0)
                    ),
                )
            except ValueError as e:
                # Fail-closed (strict only): unknown same-side exposure must
                # never be silently treated as 0 risk. In non-strict mode a
                # conservative fallback + warning is used instead (R-01).
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
        "warnings": existing_risk_warnings,
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
            log.warning("ws_market HL proxy failed for %s: %s", symbol, e)
            try:
                await websocket.send_json(
                    {"type": "status", "status": "error", "error": "interner Fehler"}
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
    # Symmetrie zum HL-Zweig oben: unerwartete Fehler melden (best-effort)
    # und den Socket immer schliessen, statt die Exception zum Framework
    # durchschlagen zu lassen.
    try:
        await _mexc_poll_fallback(websocket, client, symbol)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws_market MEXC poll failed for %s: %s", symbol, e)
        try:
            await websocket.send_json(
                {"type": "status", "status": "error", "error": "interner Fehler"}
            )
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# Q-06(a): MEXC has no native public WS wired yet, so /ws/market falls back
# to REST polling. A flat 1s cadence would hammer the exchange (and spam
# our own logs) if the ticker call starts failing repeatedly — e.g. rate
# limit or a transient outage. Back off exponentially on consecutive
# errors, capped, and reset to the fast cadence the moment a poll succeeds
# again. No separate idle-/session-cap: the loop already exits cleanly on
# WebSocketDisconnect the instant the browser tab closes/navigates away, so
# an additional cap would add complexity without a clear failure mode it
# guards against.
MEXC_POLL_BASE_DELAY_S = 1.0
MEXC_POLL_MAX_DELAY_S = 30.0


async def _mexc_ping_pong(websocket: WebSocket) -> None:
    """Echo client app-pings with a pong (Task 4/E3-01 client watchdog).

    Mirrors ``hl_proxy._pump_client``: tolerant JSON parsing, ignores any
    frame that isn't valid JSON or not a well-formed ``{"type": "ping"}``.
    A genuine disconnect propagates as WebSocketDisconnect so the caller's
    FIRST_COMPLETED race ends the whole poll loop, same as the send side.
    """
    while True:
        raw = await websocket.receive_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "ping":
            await websocket.send_json({"type": "pong"})


async def _mexc_poll_send_loop(websocket: WebSocket, client, symbol: str) -> None:
    delay = MEXC_POLL_BASE_DELAY_S
    while True:
        if client is None:
            await websocket.send_json(
                {"type": "status", "status": "error", "error": "no client"}
            )
            break
        # Backoff gilt NUR fuer Ticker-Fehler. Sends an den Browser stehen
        # bewusst AUSSERHALB des try: ein Send-Fehler (Client weg) muss
        # sofort propagieren (-> WebSocketDisconnect beendet die Schleife),
        # statt als "Ticker-Fehler" einen Backoff-Schlaf zu verursachen.
        mid_message: dict | None = None
        err_message: dict | None = None
        try:
            t = await client.ticker(symbol)
            mid_message = {
                "type": "mid",
                "coin": symbol,
                "px": float(t.last_price),
                "time": t.timestamp,
            }
            delay = MEXC_POLL_BASE_DELAY_S
        except Exception as e:
            # Detail nur ins Server-Log (B-03-Muster) — der Client bekommt
            # eine generische Meldung, keine rohen Provider-/Stacktexte.
            log.warning("MEXC-Poll ticker error for %s: %s", symbol, e)
            err_message = {
                "type": "status",
                "status": "error",
                "error": "Ticker nicht erreichbar — neuer Versuch folgt",
            }
            delay = min(delay * 2, MEXC_POLL_MAX_DELAY_S)
        await websocket.send_json(mid_message if mid_message else err_message)
        await asyncio.sleep(delay)


async def _mexc_poll_fallback(websocket: WebSocket, client, symbol: str) -> None:
    """Drive the MEXC REST-poll fallback, plus (Task 4/E3-01) echo the
    client's app-ping so its pong-watchdog does not false-trigger a
    reconnect on the MEXC branch (Hyperliquid's proxy already answers ping
    via ``hl_proxy._pump_client`` — this was the missing half, see
    docs/superpowers/reviews/2026-07-16-audit3-realtime.md E3-03).

    Real ``WebSocket`` instances always expose ``receive_text``; some unit
    tests (test_mexc_poll_backoff.py) drive this with a minimal send-only
    double that only exercises the poll/backoff logic and has no
    ``receive_text`` — skip the ping-echo task for those instead of raising.
    """
    try:
        if hasattr(websocket, "receive_text"):
            send_task = asyncio.create_task(
                _mexc_poll_send_loop(websocket, client, symbol)
            )
            ping_task = asyncio.create_task(_mexc_ping_pong(websocket))
            done, pending = await asyncio.wait(
                {send_task, ping_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(
                    exc, (WebSocketDisconnect, asyncio.CancelledError)
                ):
                    raise exc
        else:
            await _mexc_poll_send_loop(websocket, client, symbol)
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
    client = _exchange_client(request)
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
    #
    # B-05 (audit, LOW): browser cookies are NOT port-scoped — this cookie is
    # sent by the browser to ANY server on 127.0.0.1, not just this app's
    # port, so a malicious local process listening on another loopback port
    # could also receive it. HttpOnly + SameSite=Strict already block the
    # realistic remote/XSS-exfil and cross-site-request vectors; the
    # residual risk needs a co-resident malicious LOCAL process, which could
    # typically read LOCAL_API_TOKEN out of .env directly anyway — hence LOW,
    # and deliberately not "fixed" by swapping in a separate session token
    # (that would touch the whole auth flow — cookie + header preflight +
    # SameSite=Strict — for a low-severity, already-mitigated risk; see
    # docs/superpowers/reviews/2026-07-16-audit-backend.md B-05). API/script
    # clients that don't need the browser convenience should prefer sending
    # X-Local-Token explicitly rather than relying on this cookie — the
    # header IS scoped to exactly the request the caller intends.
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


