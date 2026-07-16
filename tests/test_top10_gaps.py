"""Task 21 — die gefährlichsten ungetesteten Pfade aus dem Querschnitts-Audit.

Nur `tests/` — keine Produktionsänderung. Diese Datei schließt die Lücken, die
NICHT bereits von einem früheren Task abgedeckt sind. Bereits abgedeckte Lücken
sind hier bewusst NICHT dupliziert; der Verweis steht jeweils im Docstring:

  * Gap 3  (HL-Upstream-Timeout-Vertrag) → tests/test_hl_timeout_executor.py
           (test_hl_session_has_request_timeout / _missing_session_raises_loudly).
  * Gap 5  (Singleflight analyze_lock/news_lock) →
           tests/test_analyze_cache.py::test_concurrent_identical_cache_miss_calls_llm_once
           und tests/test_api_news.py::test_concurrent_cache_miss_only_refreshes_once.
  * Gap 6  (/api/sizing/suggest fail-closed strict=True → 400) →
           tests/test_sizing_endpoint.py::test_sizing_suggest_missing_liq_strict_still_400.
  * Gap 9  (Multi-Worker --workers-Footgun / File-Lock) →
           tests/test_multi_worker_guard.py::test_file_lock_detects_second_instance.

Neu abgedeckt hier: Gap 1, 2, 4 (nur Origin-Reject; MEXC-Poll-Backoff steckt
schon in tests/test_mexc_poll_backoff.py), 7 (nur der Endpoint-502-Pfad;
build_scan_contexts-Partial steckt in tests/test_scanner.py), 8, 10.
"""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import app
from app.models import ContractMeta, OrderTicket, Ticker
from app.orders.service import OrderService
from app.orders.tokens import PreviewStore


# ── Shared minimal helpers (self-contained; do not import test modules) ──────


def _settings(**kwargs) -> Settings:
    base = dict(
        trading_enabled=True,
        max_leverage=20,
        max_risk_pct=1.0,
        min_rrr=2.0,
        strict_rrr=True,
        risk_slippage_pct=0.05,
        allow_unprotected_entry=False,
        max_notional_usdt=5000.0,
        max_price_drift_pct=0.5,
        market_entry_slippage_pct=0.15,
        allow_cross_margin=False,
        allow_manual_trigger=True,
        auto_flatten_if_sl_unverified=True,
        preview_token_ttl_seconds=60,
        sl_verify_attempts=1,
        sl_verify_delay_s=0.0,
        close_verify_attempts=1,
        close_verify_delay_s=0.0,
        local_api_token="test-token",
    )
    base.update(kwargs)
    return Settings(**base)


def _contract(**kwargs) -> ContractMeta:
    base = dict(
        symbol="BTC_USDT",
        contract_size=0.0001,
        price_unit=0.1,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1_000_000.0,
        max_leverage=125,
        min_leverage=1,
        api_allowed=True,
    )
    base.update(kwargs)
    return ContractMeta(**base)


def _ticket(**kwargs) -> OrderTicket:
    base = dict(
        symbol="BTC_USDT",
        side="long",
        order_type="limit",
        vol=1.0,
        leverage=5,
        price=100_000.0,
        entry=100_000.0,
        stop_loss=99_000.0,
        take_profit=102_000.0,
        open_type=1,
    )
    base.update(kwargs)
    return OrderTicket(**base)


def _happy_client(place_response):
    client = MagicMock()
    client.contract_meta = AsyncMock(return_value=_contract())
    client.ticker = AsyncMock(
        return_value=Ticker(symbol="BTC_USDT", last_price=100_000.0)
    )
    client.assets = AsyncMock(
        return_value=[
            {"currency": "USDT", "equity": 10_000.0, "availableBalance": 9_000.0}
        ]
    )
    client.positions = AsyncMock(side_effect=[[], [], [], [], []])
    client.open_stop_orders = AsyncMock(return_value=[])
    client.set_leverage = AsyncMock(return_value={})
    client.place_order = AsyncMock(return_value=place_response)
    client.close_position_market = AsyncMock(return_value={"orderId": 99})
    client.cancel_order = AsyncMock(return_value={"success": True})
    return client


# ── Gap 1: Audit-DB-Write-Fehler NACH Live-Placement darf NIE bubbeln ────────


@pytest.mark.asyncio
async def test_confirm_survives_audit_db_write_failure_after_live_place():
    """Nach dem Live-Placement wird der Order-Audit-Row geschrieben. Schlägt
    dieser db.insert_order-Write fehl (SQLite weg/gesperrt), DARF die Exception
    NICHT aus confirm() heraus bubbeln — die Order IST live, ein Fehler würde
    den Nutzer zum gefährlichen Re-Preview verleiten. Erwartung: ok=True,
    korrekter Status ('placed'), und der Audit-Fehler nur in post_errors."""
    client = _happy_client({"orderId": 1, "slTriggerOid": 555})
    db = MagicMock()
    db.insert_preview = AsyncMock()
    db.mark_preview_used = AsyncMock()
    # Der POST-PLACE-Audit-Write kracht — nichts davor.
    db.insert_order = AsyncMock(side_effect=RuntimeError("sqlite write boom"))
    svc = OrderService(client, _settings(), PreviewStore(), db=db)

    prev = await svc.preview(_ticket())
    assert prev["ok"], prev.get("errors")

    out = await svc.confirm(prev["token"])  # must NOT raise
    assert out["ok"] is True
    assert out["status"] == "placed"
    assert out["sl_verified"] is True
    # Live-Order steht — der Place lief genau einmal.
    client.place_order.assert_awaited_once()
    # Der Audit-Fehler ist eingefangen und nur weich gemeldet.
    assert any("audit log failed" in e for e in out["post_errors"])


# ── Gap 2: Lifespan-Shutdown räumt sauber auf ───────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_shutdown_cancels_resolver_and_closes_client(
    tmp_path, monkeypatch
):
    """Beim Shutdown muss lifespan den resolver_task canceln UND awaiten und
    danach client.aclose() aufrufen."""
    from fastapi import FastAPI

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "trader.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    aclose_called = {"v": False}

    class _FakeClient:
        async def aclose(self):
            aclose_called["v"] = True

    monkeypatch.setattr("app.main.create_exchange_client", lambda s: _FakeClient())

    resolver_state = {"started": False, "cancelled": False}

    async def _fake_loop(_app):
        resolver_state["started"] = True
        try:
            await asyncio.Event().wait()  # laufen bis gecancelt
        except asyncio.CancelledError:
            resolver_state["cancelled"] = True
            raise

    monkeypatch.setattr("app.journal.resolver.run_resolver_loop", _fake_loop)

    from app.main import lifespan

    test_app = FastAPI()
    async with lifespan(test_app):
        await asyncio.sleep(0.02)  # resolver-Task anlaufen lassen
        assert resolver_state["started"] is True

    # Nach dem Shutdown: resolver gecancelt+geawaited, Client geschlossen.
    assert resolver_state["cancelled"] is True
    assert aclose_called["v"] is True
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_lifespan_shutdown_survives_resolver_error(tmp_path, monkeypatch):
    """Ein Fehler im resolver_task darf den Shutdown NICHT abbrechen — die
    aclose()/close()-Aufräumung muss trotzdem laufen (das finally fängt sowohl
    CancelledError als auch Exception ab)."""
    from fastapi import FastAPI

    from app.config import get_settings

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "trader.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    aclose_called = {"v": False}

    class _FakeClient:
        async def aclose(self):
            aclose_called["v"] = True

    monkeypatch.setattr("app.main.create_exchange_client", lambda s: _FakeClient())

    async def _boom_loop(_app):
        raise RuntimeError("resolver crashed on startup")

    monkeypatch.setattr("app.journal.resolver.run_resolver_loop", _boom_loop)

    from app.main import lifespan

    test_app = FastAPI()
    # Der Kontextmanager darf trotz Resolver-Fehler sauber ein- und aussteigen.
    async with lifespan(test_app):
        await asyncio.sleep(0.02)
    assert aclose_called["v"] is True
    get_settings.cache_clear()


# ── Gap 4: /ws/market Origin-Reject (Code 1008) ─────────────────────────────
# Der MEXC-Poll-Branch + Backoff ist bereits in tests/test_mexc_poll_backoff.py
# abgedeckt; hier fehlt nur die Cross-Origin-Ablehnung des WebSockets.


def test_ws_market_rejects_cross_origin_with_1008(monkeypatch):
    """Ein Browser sendet immer Origin. Eine fremde Website darf den Socket
    NICHT öffnen — der Server schließt vor accept() mit Code 1008."""
    from app.config import get_settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    get_settings.cache_clear()
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect) as ei:
            with tc.websocket_connect(
                "/ws/market?symbol=BTC",
                headers={"origin": "https://evil.example.com"},
            ):
                pass
    assert ei.value.code == 1008


def test_ws_market_accepts_loopback_origin_mexc_poll_branch(monkeypatch):
    """Gegenprobe: ein loopback-Origin wird akzeptiert und landet im
    MEXC-Poll-Fallback-Branch (erste Nachricht: poll_fallback-Status)."""
    from app.config import get_settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    get_settings.cache_clear()
    poll_client = MagicMock()
    poll_client.ticker = AsyncMock(
        return_value=Ticker(symbol="BTC_USDT", last_price=1.0)
    )
    with TestClient(app) as tc:
        tc.app.state.mexc = poll_client
        tc.app.state.exchange = poll_client
        with tc.websocket_connect(
            "/ws/market?symbol=BTC_USDT", headers={"origin": "http://localhost"}
        ) as ws:
            msg = ws.receive_json()
    assert msg["type"] == "status"
    assert msg["status"] == "poll_fallback"
    assert msg["exchange"] == "mexc"


# ── Gap 7: /api/scan — keine Kontexte → 502 mit errors-Payload ──────────────
# build_scan_contexts-Partial-Failure ist in tests/test_scanner.py abgedeckt;
# hier fehlt der Endpoint-Pfad: wenn ALLE Coins Klines-Fehler werfen, muss der
# Endpoint 502 mit den fetch_errors liefern (statt eine leere Liste zu scannen).


def test_scan_endpoint_no_contexts_returns_502_with_errors(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    get_settings.cache_clear()

    client = MagicMock()
    client.market_overview = AsyncMock(
        return_value=[
            {"symbol": "BTC", "volume24": 1e9, "funding": 0.0, "last": 100.0},
            {"symbol": "ETH", "volume24": 5e8, "funding": 0.0, "last": 50.0},
        ]
    )
    # Jeder Coin scheitert beim Klines-Fetch → build_scan_contexts liefert
    # ([], errors) → Endpoint muss 502 mit errors-Payload werfen.
    client.klines = AsyncMock(side_effect=RuntimeError("exchange down"))

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post("/api/scan", json={"tf": "15m", "htf": "1H"})
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["message"] == "no coin data for scan"
    assert detail["errors"]  # fetch_errors durchgereicht, nicht verschluckt


# ── Gap 8: hl_proxy Disconnect-mid-Stream ───────────────────────────────────


@pytest.mark.asyncio
async def test_hl_proxy_client_disconnect_cancels_pending_upstream():
    """Trennt der Browser mitten im Stream, muss der noch laufende
    Upstream-Pump-Task gecancelt werden und KEINE Exception aus
    proxy_hyperliquid_market herausdringen."""
    import app.realtime.hl_proxy as hl_proxy

    up_state = {"cancelled": False}

    class _FakeUpstream:
        def __init__(self):
            self.sent: list = []

        async def send(self, data):
            self.sent.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                await asyncio.Event().wait()  # blockiert für immer
            except asyncio.CancelledError:
                up_state["cancelled"] = True
                raise

    class _FakeConnect:
        def __init__(self, up):
            self._up = up

        async def __aenter__(self):
            return self._up

        async def __aexit__(self, *a):
            return False

    up = _FakeUpstream()
    monkeypatch_connect = lambda url, **kw: _FakeConnect(up)
    hl_proxy_connect_orig = hl_proxy.websockets.connect
    hl_proxy.websockets.connect = monkeypatch_connect  # type: ignore[assignment]

    class _FakeClientWS:
        def __init__(self):
            self.sent: list = []

        async def send_json(self, d):
            self.sent.append(d)

        async def receive_text(self):
            # Browser sofort getrennt → beendet den client-pump-Task.
            raise WebSocketDisconnect()

    client_ws = _FakeClientWS()
    settings = Settings(hl_testnet=True)
    try:
        # Darf NICHT werfen — WebSocketDisconnect wird intern behandelt.
        await hl_proxy.proxy_hyperliquid_market(
            client_ws, settings, symbol="BTC", tf="15m"
        )
        # Dem gecancelten Upstream-Task eine Runde zum Abwickeln geben.
        await asyncio.sleep(0.05)
    finally:
        hl_proxy.websockets.connect = hl_proxy_connect_orig  # type: ignore[assignment]

    assert up_state["cancelled"] is True
    # Status-Frames connecting + live wurden an den Browser geschickt.
    statuses = [m.get("status") for m in client_ws.sent if m.get("type") == "status"]
    assert "connecting" in statuses and "live" in statuses


# ── Gap 10: DB-Lock-Contention (WAL / busy_timeout) ─────────────────────────


@pytest.mark.asyncio
async def test_db_wal_and_busy_timeout_are_configured(tmp_path):
    """Regressionsmarker: init() muss WAL + busy_timeout setzen — die Grundlage
    dafür, dass eine kurz gesperrte DB wartet statt sofort 'database is locked'
    zu werfen und einen Order-Audit-Row zu verlieren."""
    from app.db.repo import Database

    db = Database(str(tmp_path / "trader.db"))
    await db.init()
    await db.open()
    try:
        async with db._acquire() as conn:
            cur = await conn.execute("PRAGMA journal_mode;")
            (mode,) = await cur.fetchone()
            cur = await conn.execute("PRAGMA busy_timeout;")
            (busy,) = await cur.fetchone()
    finally:
        await db.close()
    assert str(mode).lower() == "wal"
    assert int(busy) == 30000


@pytest.mark.asyncio
async def test_db_briefly_locked_does_not_lose_order_audit_row(tmp_path):
    """Eine kurz von einem zweiten Writer gesperrte DB darf keinen
    Order-Audit-Row verlieren: busy_timeout lässt insert_order warten, bis der
    andere Writer commited — der Row landet danach vollständig in der DB."""
    import aiosqlite

    from app.db.repo import Database

    db = Database(str(tmp_path / "trader.db"))
    await db.init()
    await db.open()

    # Zweite Verbindung hält kurz den Write-Lock (BEGIN IMMEDIATE).
    blocker = await aiosqlite.connect(str(db.path), timeout=30.0)
    await blocker.execute("PRAGMA busy_timeout=30000;")
    await blocker.execute("BEGIN IMMEDIATE;")

    async def _release_soon():
        await asyncio.sleep(0.15)
        await blocker.rollback()  # Lock freigeben

    async def _insert():
        return await db.insert_order(
            symbol="BTC_USDT",
            side="long",
            request_json={"symbol": "BTC_USDT"},
            response_json={"status": "placed"},
            status="placed",
            error=None,
        )

    try:
        # insert_order blockiert auf busy_timeout, bis der Blocker loslässt.
        _, row_id = await asyncio.gather(_release_soon(), _insert())
        assert row_id > 0
        rows = await db.recent_orders(limit=5)
    finally:
        await blocker.close()
        await db.close()

    assert any(r["status"] == "placed" for r in rows)
