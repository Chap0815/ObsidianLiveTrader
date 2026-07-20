"""Order preview/confirm flow with MOCKED MexcClient — no real keys."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings, get_settings
from app.models import ContractMeta, OrderTicket, Ticker
from app.orders.service import (
    OrderError,
    OrderService,
    scale_out_errors,
    ticket_to_mexc_body,
)
from app.orders.tokens import PreviewStore, TokenError


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
        preview_token_ttl_seconds=60,
        mexc_api_key="test-key",
        mexc_api_secret="test-secret",
        local_api_token="test-token",
    )
    base.update(kwargs)
    return Settings(**base)


def _contract() -> ContractMeta:
    return ContractMeta(
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


def _good_ticket(**kwargs) -> OrderTicket:
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


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.contract_meta = AsyncMock(return_value=_contract())
    client.ticker = AsyncMock(
        return_value=Ticker(symbol="BTC_USDT", last_price=100_000.0)
    )
    client.assets = AsyncMock(
        return_value=[
            {
                "currency": "USDT",
                "equity": 10_000.0,
                "availableBalance": 9_000.0,
            }
        ]
    )
    client.positions = AsyncMock(return_value=[])
    client.open_stop_orders = AsyncMock(
        return_value=[{"stopLossPrice": 99_000.0, "symbol": "BTC_USDT"}]
    )
    client.open_orders = AsyncMock(return_value=[])
    client.set_leverage = AsyncMock(return_value={"success": True})
    client.place_order = AsyncMock(return_value={"orderId": 12345, "stopLossPrice": 99_000.0})
    client.cancel_order = AsyncMock(return_value={"success": True})
    client.close_position_market = AsyncMock(return_value={"orderId": 999})
    return client


@pytest.fixture
def store() -> PreviewStore:
    return PreviewStore()


@pytest.fixture
def client() -> MagicMock:
    return _mock_client()


def test_ticket_to_mexc_body_side_and_type():
    t = _good_ticket(side="long", order_type="limit")
    body = ticket_to_mexc_body(
        t,
        rounded_vol=1.0,
        rounded_price=100_000.0,
        external_oid="mlt-test",
        stop_loss=99_000.0,
        take_profit=102_000.0,
    )
    assert body["side"] == 1
    assert body["type"] == 1
    assert body["vol"] == 1.0
    assert body["price"] == 100_000.0
    assert body["stopLossPrice"] == 99_000.0
    assert body["externalOid"] == "mlt-test"

    t2 = _good_ticket(side="short", order_type="market")
    body2 = ticket_to_mexc_body(
        t2,
        rounded_vol=2.0,
        rounded_price=None,
        external_oid="mlt-2",
    )
    assert body2["side"] == 3
    assert body2["type"] == 5
    assert body2["price"] == 0


@pytest.mark.asyncio
async def test_over_leverage_rejected_on_preview(client, store):
    svc = OrderService(client, _settings(), store)
    out = await svc.preview(_good_ticket(leverage=50))
    assert out["ok"] is False
    assert out["token"] is None
    assert any("MAX_LEVERAGE" in e for e in out["errors"])
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_risk_over_max_rejected_on_preview(client, store):
    svc = OrderService(client, _settings(max_risk_pct=1.0), store)
    out = await svc.preview(_good_ticket(vol=50_000, take_profit=200_000))
    assert out["ok"] is False
    assert any("MAX_RISK_PCT" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_low_rrr_strict_rejected_on_preview(client, store):
    svc = OrderService(client, _settings(strict_rrr=True, min_rrr=2.0), store)
    out = await svc.preview(_good_ticket(take_profit=100_600.0))
    assert out["ok"] is False
    assert any("RRR" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_unprotected_without_sl_rejected(client, store):
    svc = OrderService(
        client, _settings(allow_unprotected_entry=False), store
    )
    out = await svc.preview(
        _good_ticket(stop_loss=None, take_profit=None)
    )
    assert out["ok"] is False
    assert any("stop_loss" in e.lower() or "UNPROTECTED" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_preview_then_confirm_places_once(client, store):
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    assert prev["ok"] is True
    assert prev["token"]
    assert "risk_usdt" in prev["summary"]

    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    client.place_order.assert_called_once()
    body = client.place_order.call_args[0][0]
    assert body["side"] == 1
    assert body["externalOid"].startswith("mlt-")
    assert "stopLossPrice" in body
    client.set_leverage.assert_called()


@pytest.mark.asyncio
async def test_double_confirm_fails(client, store):
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    token = prev["token"]
    await svc.confirm(token)
    with pytest.raises(OrderError) as ei:
        await svc.confirm(token)
    assert "token" in str(ei.value).lower() or "used" in str(ei.value).lower() or "invalid" in str(ei.value).lower()
    assert client.place_order.call_count == 1


@pytest.mark.asyncio
async def test_confirm_when_trading_enabled_false_fails(client, store):
    # Issue token while armed, then disarm before confirm
    armed = _settings(trading_enabled=True)
    svc_armed = OrderService(client, armed, store)
    prev = await svc_armed.preview(_good_ticket())
    assert prev["ok"] is True

    disarmed = _settings(trading_enabled=False)
    svc_off = OrderService(client, disarmed, store)
    with pytest.raises(OrderError) as ei:
        await svc_off.confirm(prev["token"])
    assert "DISARMED" in str(ei.value) or "TRADING_ENABLED" in str(ei.value)
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_preview_disarmed_no_token(client, store):
    svc = OrderService(client, _settings(trading_enabled=False), store)
    out = await svc.preview(_good_ticket())
    assert out["ok"] is False
    assert out["token"] is None
    assert any("DISARMED" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_expired_token_fails(client, store):
    svc = OrderService(
        client, _settings(trading_enabled=True, preview_token_ttl_seconds=60), store
    )
    # Manually inject expired token
    token = store.create({"ticket": _good_ticket().model_dump(), "gate": {}}, ttl=60)
    # Force expiry
    with store._lock:
        store._items[token]["expires_at"] = 0
    with pytest.raises(OrderError):
        await svc.confirm(token)
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_calls_client(client, store):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)
    out = await svc.cancel(order_id=12345, symbol="BTC_USDT")
    assert out["ok"] is True
    client.cancel_order.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_unknown_order_blocked(client, store):
    client.open_orders = AsyncMock(return_value=[])
    svc = OrderService(client, _settings(trading_enabled=False), store)
    with pytest.raises(OrderError) as ei:
        await svc.cancel(order_id=99999, symbol="BTC_USDT")
    assert "not found" in str(ei.value).lower()
    client.cancel_order.assert_not_called()


def test_token_consume_once():
    store = PreviewStore()
    t = store.create({"a": 1}, ttl=30)
    assert store.consume(t)["a"] == 1
    with pytest.raises(TokenError):
        store.consume(t)


# --- FastAPI route smoke (mocked client on app.state) ---


def test_api_preview_confirm_route(client, store, tmp_path, monkeypatch):
    """Route-level preview→confirm with mocked MEXC; temp DB; armed."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    # Armed trading now requires a non-empty LOCAL_API_TOKEN (fail-closed).
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEXC_API_KEY", "test-key")
    monkeypatch.setenv("MEXC_API_SECRET", "test-secret")
    get_settings.cache_clear()
    _hdr = {"X-Local-Token": "test-token"}

    from fastapi.testclient import TestClient

    from app.db.repo import Database
    from app.main import app

    monkeypatch.setenv("EXCHANGE", "mexc")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        app.state.mexc = client
        app.state.exchange = client
        app.state.preview_store = store
        app.state.db = Database(str(tmp_path / "test.db"))

        r = tc.post(
            "/api/orders/preview",
            json={
                "symbol": "BTC_USDT",
                "side": "long",
                "order_type": "limit",
                "vol": 1,
                "leverage": 5,
                "price": 100000,
                "entry": 100000,
                "stop_loss": 99000,
                "take_profit": 102000,
            },
            headers=_hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        token = body["token"]
        assert token

        r2 = tc.post("/api/orders/confirm", json={"token": token}, headers=_hdr)
        assert r2.status_code == 200, r2.text
        client.place_order.assert_called()

    get_settings.cache_clear()


# ── T2: preview token expiry + single-slot ──────────────────────────────────


def test_preview_token_expiry_and_single_slot():
    from app.orders.tokens import TokenError

    store = PreviewStore()
    first = store.create({"a": 1}, ttl=60)
    second = store.create({"a": 2}, ttl=60)
    # single slot: creating a second token invalidates the first
    with pytest.raises(TokenError):
        store.consume(first)
    assert store.consume(second)["a"] == 2
    # expiry: a past expires_at raises on consume
    third = store.create({"a": 3}, ttl=60)
    with store._lock:
        store._items[third]["expires_at"] = 0
    with pytest.raises(TokenError):
        store.consume(third)


# ── T4: same-side risk aggregates multiple open positions ───────────────────


def test_estimate_same_side_risk_aggregates_multiple():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 2.0,
         "entry_price": 100.0, "liquidate_price": 90.0},
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": 80.0},
        {"symbol": "BTC_USDT", "side": "short", "hold_vol": 5.0,
         "entry_price": 100.0, "liquidate_price": 120.0},  # other side ignored
        {"symbol": "ETH_USDT", "side": "long", "hold_vol": 9.0,
         "entry_price": 100.0, "liquidate_price": 90.0},   # other symbol ignored
    ]
    # pos_risk_cap_pct high enough that the liq-distance cap never binds, so
    # this stays a pure aggregation test.
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0,
        strict=False, pos_risk_cap_pct=100.0,
    )
    # |100-90|*1*2 + |100-80|*1*1 = 20 + 20 = 40
    assert total == pytest.approx(40.0)
    assert warnings == []


def test_estimate_same_side_risk_fail_closed_without_liq():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": None},
    ]
    with pytest.raises(ValueError):
        estimate_same_side_risk_usdt(
            positions, symbol="BTC_USDT", side="long", contract_size=1.0,
            strict=True, pos_risk_cap_pct=2.0,
        )


# ── T8 (R-01): SL-distance / cap / non-strict fallback ──────────────────────


def test_aggregate_uses_sl_distance_when_present():
    from app.orders.service import estimate_same_side_risk_usdt

    # SL is much nearer than liq → risk must come from SL distance, not liq.
    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 2.0,
         "entry_price": 100.0, "liquidate_price": 90.0, "stop_loss": 95.0},
    ]
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0,
        strict=False, pos_risk_cap_pct=100.0,
    )
    # |100-95|*1*2 = 10  (liq distance would have been |100-90|*1*2 = 20)
    assert total == pytest.approx(10.0)
    assert warnings == []


def test_aggregate_caps_liq_distance():
    from app.orders.service import estimate_same_side_risk_usdt

    # No SL, very wide liq distance → capped at entry * cap_pct/100.
    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": 50.0},
    ]
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0,
        strict=False, pos_risk_cap_pct=2.0,
    )
    # min(|100-50|=50, 100*2/100=2) * 1 * 1 = 2
    assert total == pytest.approx(2.0)
    assert warnings == []


def test_missing_liq_non_strict_warns_not_raises():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": None},
    ]
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0,
        strict=False, pos_risk_cap_pct=2.0,
    )
    # fallback = 100 * 2/100 * 1 * 1 = 2
    assert total == pytest.approx(2.0)
    assert len(warnings) == 1
    assert "liquidate_price" in warnings[0]


def test_missing_liq_strict_still_raises():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": None},
    ]
    with pytest.raises(ValueError):
        estimate_same_side_risk_usdt(
            positions, symbol="BTC_USDT", side="long", contract_size=1.0,
            strict=True, pos_risk_cap_pct=2.0,
        )


# ── T5: confirm while disarmed consumes the token (no reuse after arming) ────


@pytest.mark.asyncio
async def test_confirm_when_disarmed_consumes_token(client, store):
    from app.orders.tokens import TokenError

    armed = _settings(trading_enabled=True)
    svc_armed = OrderService(client, armed, store)
    prev = await svc_armed.preview(_good_ticket())
    token = prev["token"]
    assert token

    disarmed = _settings(trading_enabled=False)
    svc_off = OrderService(client, disarmed, store)
    with pytest.raises(OrderError) as ei:
        await svc_off.confirm(token)
    assert "DISARMED" in str(ei.value) or "TRADING_ENABLED" in str(ei.value)
    client.place_order.assert_not_called()
    # Token was consumed -> cannot be reused after arming without a fresh preview
    with pytest.raises(TokenError):
        store.consume(token)


# ── Projekt H / Task 1: modify_stop_loss (money-critical) ────────────────────


def _modify_client(**over) -> MagicMock:
    """Mock client for modify-SL: same-side long hold 0.01 @ mark 100k."""
    c = MagicMock()
    c.exchange_id = "hyperliquid"
    c.ticker = AsyncMock(return_value=Ticker(symbol="BTC_USDT", last_price=100_000.0))
    c.contract_meta = AsyncMock(return_value=_contract())
    c.positions = AsyncMock(
        return_value=[{"symbol": "BTC_USDT", "side": "long",
                       "hold_vol": 0.01, "open_type": 1}]
    )
    old = {"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 98_000.0}
    new = {"orderId": 555, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 99_000.0}
    # call #1 existing-scan -> [old]; call #2/#3 verify -> [new]
    c.open_stop_orders = AsyncMock(side_effect=[[old], [new], [new]])
    c.place_stop_order = AsyncMock(
        return_value={"orderId": 555, "error": None, "requestedTrigger": 99_000.0,
                      "symbol": "BTC"}
    )
    c.cancel_order = AsyncMock(return_value={"success": True})
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _modify_settings(**kw) -> Settings:
    return _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0, **kw)


@pytest.mark.asyncio
async def test_modify_sl_places_new_then_cancels_old(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["ok"] is True
    assert out["status"] == "modify_sl_ok"
    assert out["new_oid"] == 555
    assert out["cancelled_old"] == [111]
    c.place_stop_order.assert_awaited_once()
    c.cancel_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_modify_sl_new_placement_failure_keeps_old(store):
    from app.hyperliquid.errors import HyperliquidError
    c = _modify_client(place_stop_order=AsyncMock(side_effect=HyperliquidError("boom")))
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "old SL left in place" in str(ei.value)
    c.cancel_order.assert_not_called()  # old stop untouched


@pytest.mark.asyncio
async def test_modify_sl_place_rejected_keeps_old(store):
    c = _modify_client(place_stop_order=AsyncMock(
        return_value={"orderId": None, "error": "insufficient margin"}))
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "rejected" in str(ei.value)
    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_verify_timeout_keeps_old(store):
    c = _modify_client()
    # existing-scan -> [old]; verify attempts see NO matching stop.
    c.open_stop_orders = AsyncMock(side_effect=[[{"orderId": 111, "symbol": "BTC_USDT",
                                                  "orderType": "Stop",
                                                  "triggerPrice": 98_000.0}], [], []])
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["verified"] is False
    assert out["status"] == "modify_sl_unverified_old_kept"
    c.cancel_order.assert_not_called()  # never cancel on doubt


@pytest.mark.asyncio
async def test_modify_sl_cancel_old_failure_two_stops(store):
    c = _modify_client(cancel_order=AsyncMock(side_effect=RuntimeError("cancel 500")))
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["status"] == "modify_sl_ok_old_cancel_failed"
    assert 111 in out["failed_cancel"]
    assert any("ÜBER-geschützt" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_modify_sl_small_step_verifies_by_oid_not_price(store):
    # New SL only $100 from the old one (< 0.15% => a price-only verify would
    # accept the OLD, unchanged stop as "the new SL"). OID verify must gate on
    # the concrete new oid, never on price alone.
    old = {"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 99_000.0}
    new = {"orderId": 556, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 99_100.0}
    placed = {"orderId": 556, "error": None, "requestedTrigger": 99_100.0,
              "symbol": "BTC"}

    # (A) new oid rests alongside the price-close old -> old IS cancelled.
    c = _modify_client()
    c.open_stop_orders = AsyncMock(side_effect=[[old], [old, new], [old, new]])
    c.place_stop_order = AsyncMock(return_value=placed)
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_100.0)
    assert out["verified"] is True
    assert out["cancelled_old"] == [111]

    # (B) new oid does NOT rest — only the old, price-close stop is present.
    # Price-only verify would falsely pass; OID verify keeps the old stop.
    c2 = _modify_client()
    c2.open_stop_orders = AsyncMock(side_effect=[[old], [old], [old]])
    c2.place_stop_order = AsyncMock(return_value=placed)
    svc2 = OrderService(c2, _modify_settings(), store)
    out2 = await svc2.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_100.0)
    assert out2["verified"] is False
    assert out2["status"] == "modify_sl_unverified_old_kept"
    c2.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_geometry_long_rejected(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=101_000.0)
    assert "BELOW mark" in str(ei.value)
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_no_position_rejected(store):
    c = _modify_client(positions=AsyncMock(return_value=[]))
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "no open long position" in str(ei.value)


@pytest.mark.asyncio
async def test_modify_sl_positions_lookup_failure_is_distinct_from_no_position(store):
    """F-E1: a positions LOOKUP FAILURE must not be reported as 'no open
    position' — that risks the user assuming they are flat when the true
    state is simply unknown. It must raise a distinct, non-misleading error."""
    from app.hyperliquid.errors import HyperliquidError

    c = _modify_client(positions=AsyncMock(side_effect=HyperliquidError("positions down")))
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    msg = str(ei.value)
    assert "no open long position" not in msg
    assert "lookup failed" in msg or "could not verify" in msg
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_refuses_loosening_new_sl_long(store):
    """C1b defense-in-depth: a caller passing a LOOSER new_sl than the existing
    most-protective resting stop must be REFUSED (never-unprotected): the old,
    tighter stop is left in place, nothing is placed or cancelled."""
    c = _modify_client()  # existing resting stop @ 98_000, mark 100_000, long
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        # 97_000 < existing 98_000 → would LOOSEN protection (still below mark).
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=97_000.0)
    msg = str(ei.value).lower()
    assert "loosen" in msg or "lockern" in msg or "protective" in msg
    c.place_stop_order.assert_not_called()  # old stop untouched
    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_refuses_loosening_new_sl_short(store):
    """C1b (short): for a short, a HIGHER new_sl than the existing stop loosens."""
    c = _modify_client()
    c.positions = AsyncMock(
        return_value=[{"symbol": "BTC_USDT", "side": "short",
                       "hold_vol": 0.01, "open_type": 1}]
    )
    old = {"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 102_000.0}
    c.open_stop_orders = AsyncMock(side_effect=[[old], [old], [old]])
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        # 103_000 > existing 102_000 → would LOOSEN a short (still above mark).
        await svc.modify_stop_loss(symbol="BTC_USDT", side="short", new_sl=103_000.0)
    msg = str(ei.value).lower()
    assert "loosen" in msg or "lockern" in msg or "protective" in msg
    c.place_stop_order.assert_not_called()
    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_tighten_still_allowed_over_existing(store):
    """C1b must NOT block the legitimate TIGHTEN path (new_sl more protective)."""
    c = _modify_client()  # existing 98_000, new 99_000 long → tighter
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["ok"] is True
    c.place_stop_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_modify_sl_disarmed_blocked(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(trading_enabled=False), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "DISARMED" in str(ei.value)
    c.place_stop_order.assert_not_called()


def test_api_modify_sl_route(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEXC_API_KEY", "test-key")
    monkeypatch.setenv("MEXC_API_SECRET", "test-secret")
    monkeypatch.setenv("EXCHANGE", "hyperliquid")
    monkeypatch.setenv("SL_VERIFY_ATTEMPTS", "1")
    monkeypatch.setenv("SL_VERIFY_DELAY_S", "0")
    get_settings.cache_clear()
    _hdr = {"X-Local-Token": "test-token"}

    from fastapi.testclient import TestClient
    from app.db.repo import Database
    from app.main import app

    # Route normalizes BTC_USDT -> BTC for Hyperliquid; position must match.
    c = _modify_client(positions=AsyncMock(return_value=[
        {"symbol": "BTC", "side": "long", "hold_vol": 0.01, "open_type": 1}]))
    with TestClient(app) as tc:
        app.state.mexc = c
        app.state.exchange = c
        app.state.preview_store = store
        app.state.db = Database(str(tmp_path / "test.db"))
        r = tc.post(
            "/api/orders/modify-sl",
            json={"symbol": "BTC_USDT", "side": "long", "new_sl": 99000},
            headers=_hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["status"] == "modify_sl_ok"
    get_settings.cache_clear()


# ── Projekt H / Task 3: scale-out TP ladder ──────────────────────────────────


def test_ticket_to_mexc_body_scale_out_sets_tp2():
    t = _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0, tp1_share=0.5)
    body = ticket_to_mexc_body(
        t,
        rounded_vol=2.0,
        rounded_price=100_000.0,
        external_oid="mlt-so",
        stop_loss=99_000.0,
        take_profit=102_000.0,
    )
    assert body["takeProfitPrice"] == 102_000.0
    assert body["takeProfitPrice2"] == 104_000.0
    assert body["tp1Share"] == 0.5


def test_scale_out_errors_rejects_non_hyperliquid_client():
    c = MagicMock()
    c.exchange_id = "mexc"
    ticket = _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0)
    errs = scale_out_errors(ticket, 100_000.0, c)
    assert any("Hyperliquid" in e for e in errs)


@pytest.mark.asyncio
async def test_scale_out_geometry_rejected(client, store):
    client.exchange_id = "hyperliquid"
    svc = OrderService(client, _settings(), store)
    out = await svc.preview(
        _good_ticket(scale_out=True, take_profit=102_000.0, tp2=101_000.0)
    )  # TP2 < TP1 for long
    assert out["ok"] is False
    assert any("TP2 must be above TP1" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_scale_out_rejected_on_non_hyperliquid_preview(client, store):
    # client fixture has no exchange_id set -> not "hyperliquid" -> rejected
    svc = OrderService(client, _settings(), store)
    out = await svc.preview(
        _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0)
    )
    assert out["ok"] is False
    assert any("Hyperliquid" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_scale_out_preview_confirm_places_two_tps(client, store):
    client.exchange_id = "hyperliquid"
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(
        _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0, tp1_share=0.5)
    )
    assert prev["ok"] is True
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    body = client.place_order.call_args[0][0]
    assert body["takeProfitPrice2"] == 104_000.0


@pytest.mark.asyncio
async def test_modify_stop_loss_rejected_on_non_hyperliquid(store):
    from app.mexc.client import MexcClient

    c = MagicMock(spec=MexcClient)
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "nur auf Hyperliquid" in str(ei.value)


# ── M1: partially-filled resting GTC limit → remainder unprotected + unwarned ─


@pytest.mark.asyncio
async def test_partial_limit_fill_warns_and_not_fully_sl_verified(client, store):
    """A GTC limit that only partially fills has its reduce-only SL sized to the
    ACTUAL fill (HL adapter, F-02). The resting remainder is UNPROTECTED if it
    later fills. Confirm must NOT report the position as fully SL-verified and
    must surface a loud warning — without market-closing the protected fill."""
    # place_order: real SL trigger for the filled part, but fill (6) < req (10).
    client.place_order = AsyncMock(
        return_value={"orderId": 1, "slTriggerOid": 999, "entryFilledSz": 6.0}
    )
    svc = OrderService(
        client,
        _settings(trading_enabled=True, auto_flatten_if_sl_unverified=True),
        store,
    )
    prev = await svc.preview(_good_ticket(vol=10.0))
    assert prev["ok"] is True
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    # The SL for the filled portion is genuinely placed → sl_verified stays True…
    assert conf["sl_verified"] is True
    # …but the position is NOT fully protected: the honest full-coverage flag.
    assert conf["sl_fully_verified"] is False
    assert "partial" in conf["status"].lower()
    assert any(
        "UNGESCHÜTZT" in w or "TEILFÜLLUNG" in w or "TEILGEFÜLLT" in w
        for w in conf["warnings"]
    )
    # Must NOT flatten a partly-protected position (no fill-watcher, honest report).
    client.close_position_market.assert_not_called()


@pytest.mark.asyncio
async def test_full_limit_fill_stays_fully_sl_verified(client, store):
    """Control: a fully-filled limit (fill == requested) reports fully verified."""
    client.place_order = AsyncMock(
        return_value={"orderId": 1, "slTriggerOid": 999, "entryFilledSz": 10.0}
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket(vol=10.0))
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    assert conf["sl_verified"] is True
    assert conf["sl_fully_verified"] is True
    assert "partial" not in conf["status"].lower()


# ── C-1: an errored trigger-aware stop lookup must yield UNKNOWN, not flatten ─


@pytest.mark.asyncio
async def test_stop_lookup_error_yields_unknown_not_flatten(client, store):
    """C-1 fail-safe (service side): if the trigger-aware stop lookup errors, the
    SL status must resolve to UNKNOWN (sl_checked=False), NOT a confident MISSING,
    and auto-flatten must NOT market-close the (possibly protected) position."""
    from app.hyperliquid.errors import HyperliquidError

    # place_order returns NO trigger oid → forces the _verify_sl_attached path.
    client.place_order = AsyncMock(return_value={"orderId": 1})
    # The authoritative stop lookup errors out (transient HL hiccup).
    client.open_stop_orders = AsyncMock(
        side_effect=HyperliquidError("open_stop_orders failed: 503")
    )
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            auto_flatten_if_sl_unverified=True,
            sl_verify_attempts=1,
            sl_verify_delay_s=0.0,
        ),
        store,
    )
    prev = await svc.preview(_good_ticket())
    conf = await svc.confirm(prev["token"])
    assert conf["sl_checked"] is False  # UNKNOWN, not MISSING
    assert conf["sl_verified"] is False
    assert conf["status"] == "placed_sl_unknown"
    client.close_position_market.assert_not_called()


# ── X2-06 / X2-07: close-cloid wiring + MEXC live-hold clamp ─────────────────


def _pos(hold: float) -> list:
    return [{"symbol": "BTC_USDT", "side": "long", "hold_vol": hold, "open_type": 1}]


@pytest.mark.asyncio
async def test_mexc_close_clamps_vol_to_live_hold(client, store):
    """X2-07: on MEXC the positions() read used to size the close happens BEFORE
    the contract_meta() yield; the position can shrink externally in that window.
    The O-09 live-side recheck must re-apply the close fraction to the FRESH
    live_hold and clamp close_vol, so a shrunk position never gets an oversized
    close. Here hold reads 10 (→ 50% = 5) but the live recheck reads 4 (→ 50% = 2);
    the send must be clamped to 2, not 5."""
    # MEXC reads positions() 3x: sizing (10), O-09 live recheck (4), post-close
    # residual verify (2). The clamp must key off the fresh live 4, not stale 10.
    reads = iter([10.0, 4.0])

    async def _positions(_symbol=None):
        try:
            return _pos(next(reads))
        except StopIteration:
            return _pos(2.0)

    client.positions = AsyncMock(side_effect=_positions)
    svc = OrderService(client, _settings(trading_enabled=True), store)
    await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.5)
    sent_vol = client.close_position_market.await_args.kwargs["vol"]
    assert sent_vol == pytest.approx(2.0)  # 4 * 0.5, not the stale 10 * 0.5 = 5


@pytest.mark.asyncio
async def test_close_path_passes_external_oid(client, store):
    """X2-06: the manual-close call-site must pass a non-None external_oid so the
    O-08 close-cloid recovery (client stamps 'close:'+oid) is not dead code."""
    client.positions = AsyncMock(return_value=_pos(5.0))
    svc = OrderService(client, _settings(trading_enabled=True), store)
    await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)
    passed = client.close_position_market.await_args.kwargs.get("external_oid")
    assert passed  # truthy, non-None deterministic close oid
