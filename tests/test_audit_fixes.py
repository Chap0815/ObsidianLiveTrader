"""Regression tests for deep-audit money-safety fixes."""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.models import ContractMeta, OrderTicket, Ticker
from app.orders.service import OrderError, OrderService
from app.orders.tokens import PreviewStore
from app.risk.gates import validate_order
from app.mexc.errors import MexcError
from app.mexc.client import MexcClient, normalize_klines  # noqa: F401


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
        # Global default is now fail-closed (False); these order tests exercise
        # the manual path, so opt in explicitly (mirrors an .env that set it).
        allow_manual_trigger=True,
        auto_flatten_if_sl_unverified=True,
        preview_token_ttl_seconds=60,
        sl_verify_attempts=1,
        sl_verify_delay_s=0.0,
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


def test_equity_zero_fail_closed():
    g = validate_order(_ticket(), _contract(), 0.0, _settings(), last_price=100_000.0)
    assert g.ok is False
    assert any("equity" in e.lower() for e in g.errors)


def test_limit_entry_spoof_ignored_for_risk():
    """ticket.entry must not understate risk vs real limit price."""
    # limit 100000, spoofed entry 100500, SL 100200 → would pass if entry trusted
    g = validate_order(
        _ticket(
            order_type="limit",
            price=100_000.0,
            entry=100_500.0,
            stop_loss=100_200.0,
            take_profit=102_000.0,
        ),
        _contract(),
        10_000.0,
        _settings(),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any("stop_loss" in e.lower() or "below entry" in e.lower() for e in g.errors)
    assert g.entry_for_risk == pytest.approx(100_000.0)


def test_strict_rrr_requires_tp():
    g = validate_order(
        _ticket(take_profit=None),
        _contract(),
        10_000.0,
        _settings(strict_rrr=True),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any("take_profit" in e.lower() or "STRICT_RRR" in e for e in g.errors)


def test_price_drift_blocks_confirm():
    g = validate_order(
        _ticket(),
        _contract(),
        10_000.0,
        _settings(max_price_drift_pct=0.5),
        last_price=101_000.0,  # 1% up
        for_confirm=True,
        preview_last_price=100_000.0,
    )
    assert g.ok is False
    assert any("drift" in e.lower() for e in g.errors)


def test_cross_margin_blocked():
    g = validate_order(
        _ticket(open_type=2),
        _contract(),
        10_000.0,
        _settings(allow_cross_margin=False),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any("cross" in e.lower() for e in g.errors)


def test_existing_same_side_risk_aggregates():
    # 1 contract risk = 0.0001 * 1000 = 0.1 USDT; existing 100 USDT → over 1% of 10k? 
    # 1% of 10000 = 100; existing 100 + 0.1 > 100
    g = validate_order(
        _ticket(vol=1),
        _contract(),
        10_000.0,
        _settings(max_risk_pct=1.0),
        last_price=100_000.0,
        existing_same_side_risk_usdt=100.0,
    )
    assert g.ok is False
    assert any("risk" in e.lower() for e in g.errors)


@pytest.mark.asyncio
async def test_set_leverage_failure_blocks_place():
    client = MagicMock()
    client.contract_meta = AsyncMock(return_value=_contract())
    client.ticker = AsyncMock(return_value=Ticker(symbol="BTC_USDT", last_price=100_000.0))
    client.assets = AsyncMock(
        return_value=[{"currency": "USDT", "equity": 10_000.0, "availableBalance": 9_000.0}]
    )
    client.positions = AsyncMock(return_value=[])
    client.open_stop_orders = AsyncMock(return_value=[{"stopLossPrice": 99_000.0}])
    client.set_leverage = AsyncMock(side_effect=MexcError("leverage fail"))
    client.place_order = AsyncMock(return_value={"orderId": 1})
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"] is True
    with pytest.raises(OrderError) as ei:
        await svc.confirm(prev["token"])
    assert "set_leverage" in str(ei.value).lower()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_equity_api_error_no_preview_token():
    client = MagicMock()
    client.contract_meta = AsyncMock(return_value=_contract())
    client.ticker = AsyncMock(return_value=Ticker(symbol="BTC_USDT", last_price=100_000.0))
    client.assets = AsyncMock(side_effect=MexcError("auth fail"))
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"] is False
    assert prev["token"] is None


def test_min_notional_blocks_small_order():
    # notional = 1 * 0.0001 * 50000 = 5 USDT < exchange min 10
    g = validate_order(
        _ticket(price=50_000.0, entry=50_000.0, stop_loss=49_500.0, take_profit=51_000.0),
        _contract(min_notional=10.0),
        10_000.0,
        _settings(),
        last_price=50_000.0,
    )
    assert g.ok is False
    assert any("below exchange minimum" in e for e in g.errors)


def test_round_hl_price_tick_rules():
    from app.hyperliquid.client import round_hl_price

    # 5 significant figures
    assert round_hl_price(1234.5678, 1) == pytest.approx(1234.6)
    assert round_hl_price(0.123456, 1) == pytest.approx(0.12346)
    # 6+ integer digits: integer prices always allowed
    assert round_hl_price(123456.78, 1) == pytest.approx(123457.0)
    # max decimals = 6 - szDecimals
    assert round_hl_price(0.0012345678, 2) == pytest.approx(0.0012)


def _filled_pos(hold_vol: float = 1.0) -> list[dict]:
    return [
        {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "holdVol": hold_vol,
            "holdAvgPrice": 100_000.0,
            "liquidatePrice": 90_000.0,
        }
    ]


def _happy_client(place_response, *, post_hold: float = 1.0):
    """post_hold: same-side hold after place (pre is 0 → new_fill=post_hold)."""
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
    # preview risk, confirm risk, pre_hold, then filled holds for SL/flatten
    client.positions = AsyncMock(
        side_effect=[[], [], [], _filled_pos(post_hold), _filled_pos(post_hold)]
    )
    client.open_stop_orders = AsyncMock(return_value=[])
    client.set_leverage = AsyncMock(return_value={})
    client.place_order = AsyncMock(return_value=place_response)
    client.close_position_market = AsyncMock(return_value={"orderId": 99})
    client.cancel_order = AsyncMock(return_value={"success": True})
    return client


@pytest.mark.asyncio
async def test_sl_echo_in_response_is_not_trusted():
    """A request echo (stopLossPrice in response) must NOT count as verified."""
    client = _happy_client({"orderId": 1, "stopLossPrice": 99_000.0})
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    client.close_position_market.assert_awaited()


@pytest.mark.asyncio
async def test_sl_trigger_oid_counts_as_verified():
    """An explicit exchange trigger oid (HL adapter) is real evidence."""
    client = _happy_client({"orderId": 1, "slTriggerOid": 555})
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is True
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_flatten_closes_only_new_vol_not_whole_position():
    """Auto-flatten closes only (hold_now - pre_hold), never the full prefill."""
    client = _happy_client({"orderId": 1})  # no SL evidence anywhere
    pre = [
        {
            "positionId": 7,
            "symbol": "BTC_USDT",
            "positionType": 1,
            "holdVol": 50.0,
            "holdAvgPrice": 100_000.0,
            "liquidatePrice": 90_000.0,
        }
    ]
    # After fill: pre 50 + new 1 = 51
    post = [
        {
            "positionId": 7,
            "symbol": "BTC_USDT",
            "positionType": 1,
            "holdVol": 51.0,
            "holdAvgPrice": 100_000.0,
            "liquidatePrice": 90_000.0,
        }
    ]
    # preview existing, confirm existing, pre_hold, post-place SL pos, flatten hold
    client.positions = AsyncMock(
        side_effect=[pre, pre, pre, post, post]
    )
    # existing risk from liq distance is large — use high equity / risk budget
    client.assets = AsyncMock(
        return_value=[
            {"currency": "USDT", "equity": 1_000_000.0, "availableBalance": 900_000.0}
        ]
    )
    svc = OrderService(
        client,
        _settings(max_risk_pct=100.0, max_notional_usdt=1_000_000.0),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket(vol=1.0))
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    client.close_position_market.assert_awaited_once()
    kwargs = client.close_position_market.await_args.kwargs
    assert kwargs["vol"] == 1.0  # only the new fill, not 51


@pytest.mark.asyncio
async def test_flatten_unfilled_cancels_resting_not_prefill():
    """If no new fill after place, cancel resting entry — do not close pre_hold."""
    client = _happy_client({"orderId": 777})
    pre = [
        {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "holdVol": 10.0,
            "holdAvgPrice": 100_000.0,
            "liquidatePrice": 90_000.0,
        }
    ]
    # hold unchanged → new_fill=0
    client.positions = AsyncMock(side_effect=[pre, pre, pre, pre, pre])
    client.assets = AsyncMock(
        return_value=[
            {"currency": "USDT", "equity": 1_000_000.0, "availableBalance": 900_000.0}
        ]
    )
    client.cancel_order = AsyncMock(return_value={"success": True})
    svc = OrderService(
        client,
        _settings(max_risk_pct=100.0, max_notional_usdt=1_000_000.0),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket(vol=1.0))
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    client.close_position_market.assert_not_awaited()
    client.cancel_order.assert_awaited()
    assert out["flatten"] and out["flatten"].get("action") == "cancel_resting"


@pytest.mark.asyncio
async def test_same_side_without_liq_price_blocks_preview():
    """Open same-side without liquidate_price must fail-closed (not risk=0)."""
    client = _happy_client({"orderId": 1})
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC_USDT",
                "positionType": 1,
                "holdVol": 10.0,
                "holdAvgPrice": 100_000.0,
                # no liquidatePrice
            }
        ]
    )
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"] is False
    assert prev["token"] is None
    assert any("liquidate" in e.lower() or "aggregate" in e.lower() for e in prev["errors"])


@pytest.mark.asyncio
async def test_transport_timeout_recovery_returns_ok_not_error():
    """Recovered place must not look like failure (avoids blind re-preview)."""
    client = _happy_client({"orderId": 1})
    oid = None

    async def _place(_body):
        raise MexcError("timeout connecting to upstream")

    client.place_order = AsyncMock(side_effect=_place)

    async def _by_ext(symbol, external_oid):
        return {"orderId": 42, "externalOid": external_oid, "symbol": symbol}

    client.order_by_external_oid = AsyncMock(side_effect=_by_ext)
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["ok"] is True
    assert "recovered_placed" in out["status"]
    # Recovery must still run SL path (not early-return as success without check)
    assert out["sl_verified"] is False
    assert out["sl_checked"] is True
    assert any("DO NOT re-preview" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_sl_unknown_does_not_flatten():
    """If the SL-lookup endpoint itself errors, state is UNKNOWN — never flatten."""
    client = _happy_client({"orderId": 1})  # no SL evidence in response
    client.open_stop_orders = AsyncMock(side_effect=MexcError("endpoint 404"))
    # Gate-time + pre_hold succeed; post-place SL position lookup fails → UNKNOWN
    client.positions = AsyncMock(
        side_effect=[
            [],  # preview existing risk
            [],  # confirm existing risk
            [],  # pre_hold
            MexcError("endpoint 404"),  # post-place SL verify
        ]
    )
    svc = OrderService(client, _settings(auto_flatten_if_sl_unverified=True), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    assert out["sl_checked"] is False
    assert out["status"] == "placed_sl_unknown"
    client.close_position_market.assert_not_awaited()  # NOT flattened on unknown


@pytest.mark.asyncio
async def test_tp_field_on_position_is_not_sl_evidence():
    """takeProfitPrice must never count as verified stop-loss protection."""
    client = _happy_client({"orderId": 1})
    tp_only_pos = [
        {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "holdVol": 1.0,
            "holdAvgPrice": 100_000.0,
            "liquidatePrice": 90_000.0,
            "takeProfitPrice": 99_000.0,  # same number as SL, wrong field
        }
    ]
    # preview, confirm, pre_hold empty; post fill with TP-only fields
    client.positions = AsyncMock(
        side_effect=[[], [], [], tp_only_pos, tp_only_pos]
    )
    client.open_stop_orders = AsyncMock(return_value=[])
    svc = OrderService(client, _settings(auto_flatten_if_sl_unverified=True), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    client.close_position_market.assert_awaited()


@pytest.mark.asyncio
async def test_positions_api_error_blocks_preview_token():
    """Positions API failure must fail-closed (no token) — not understate risk as 0."""
    client = _happy_client({"orderId": 1})
    client.positions = AsyncMock(side_effect=MexcError("positions down"))
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"] is False
    assert prev["token"] is None
    assert any("positions" in e.lower() or "same-side" in e.lower() for e in prev["errors"])


def test_sl_rounds_onto_entry_blocked():
    """A stop that rounds onto the wrong side of entry must be rejected."""
    # long entry 100000, sl 99999.96 with price_unit 0.1 → rounds to 100000.0
    g = validate_order(
        _ticket(price=100_000.0, entry=100_000.0, stop_loss=99_999.96, take_profit=102_000.0),
        _contract(price_unit=0.1),
        10_000.0,
        _settings(),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any("tick size" in e for e in g.errors)


@pytest.mark.asyncio
async def test_sl_unverified_triggers_flatten():
    client = _happy_client({"orderId": 1}, post_hold=1.0)
    svc = OrderService(
        client,
        _settings(auto_flatten_if_sl_unverified=True),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    client.close_position_market.assert_awaited()
    client.close_position_market.assert_awaited()


@pytest.mark.asyncio
async def test_sl_unknown_when_stop_endpoint_down_does_not_flatten():
    """Regression: if the authoritative stop-order lookup FAILS (broken/renamed
    endpoint), the SL state is UNKNOWN, not MISSING. A position row without an
    SL field is NOT proof the SL is absent (MEXC does not surface it there), so
    auto-flatten must NOT close a possibly-protected position."""
    client = _happy_client({"orderId": 1}, post_hold=1.0)
    client.open_stop_orders = AsyncMock(
        side_effect=MexcError("stoporder list endpoint unavailable")
    )
    svc = OrderService(
        client,
        _settings(auto_flatten_if_sl_unverified=True),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    # UNKNOWN (checked=False) → never blind-flatten a maybe-protected trade
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_match_against_preexisting_stop_is_unknown_not_verified():
    """F-C1: a same-side position pre-existed with its OWN old stop resting
    near the same price as the new order's expected SL (e.g. re-entering at a
    similar level). No concrete new-trigger oid is returned (place_order
    response carries none, forcing the price-tolerance fallback). The OLD
    stop must NOT be credited to the freshly added size — that would falsely
    mark the new size 'verified' while only the old volume is protected.
    Expected: UNKNOWN (sl_verified False, sl_checked False), not verified."""
    client = _happy_client({"orderId": 1})  # no slTriggerOid → fallback path
    pre = [
        {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "holdVol": 50.0,
            "holdAvgPrice": 100_000.0,
            "liquidatePrice": 90_000.0,
        }
    ]
    # preview existing risk, confirm existing risk, pre_hold, verify-step2 positions
    client.positions = AsyncMock(side_effect=[pre, pre, pre, pre])
    # An OLD stop order already resting, priced exactly at the NEW order's
    # expected SL (99_000.0) — sized for the OLD 50 units only.
    client.open_stop_orders = AsyncMock(
        return_value=[
            {"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
             "triggerPrice": 99_000.0}
        ]
    )
    client.assets = AsyncMock(
        return_value=[
            {"currency": "USDT", "equity": 1_000_000.0, "availableBalance": 900_000.0}
        ]
    )
    svc = OrderService(
        client,
        _settings(max_risk_pct=100.0, max_notional_usdt=1_000_000.0,
                  auto_flatten_if_sl_unverified=True),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket(vol=1.0))
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    assert out["sl_checked"] is False  # UNKNOWN, not a false "verified"
    assert out["status"] == "placed_sl_unknown"
    assert any("UNBEKANNT" in w for w in out["warnings"])
    # UNKNOWN never auto-flattens (fail-safe requires sl_checked=True to flatten)
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_trade_lock_serializes_confirm_across_instances():
    """A new OrderService is built per request, so the lock that serializes
    confirm/close MUST be shared. With a shared lock held, a second confirm
    must block before place_order — proving parallel places can't both run."""
    import asyncio

    shared = asyncio.Lock()
    client = _happy_client({"orderId": 1, "slTriggerOid": 555})
    # Two independent services (mimics per-request construction) share the lock
    svc_a = OrderService(client, _settings(), PreviewStore(), trade_lock=shared)
    svc_b = OrderService(client, _settings(), svc_a.store, trade_lock=shared)
    assert svc_a._trade_lock is svc_b._trade_lock is shared

    prev = await svc_a.preview(_ticket())
    assert prev["ok"]

    await shared.acquire()  # simulate another confirm/close in flight
    task = asyncio.create_task(svc_b.confirm(prev["token"]))
    await asyncio.sleep(0.05)
    # Blocked on the lock — the live place must NOT have happened yet
    client.place_order.assert_not_called()

    shared.release()
    out = await task
    assert out["ok"] is True
    client.place_order.assert_awaited()


def test_order_service_falls_back_to_own_lock_when_none():
    """Without a shared lock (unit-test path) each instance still gets one."""
    client = _happy_client({"orderId": 1})
    svc = OrderService(client, _settings(), PreviewStore())
    import asyncio

    assert isinstance(svc._trade_lock, asyncio.Lock)


@pytest.mark.asyncio
async def test_manual_mode_places_without_exchange_sl():
    """trigger_mode=manual: no stopLossPrice in body, no verify, no flatten."""
    client = _happy_client({"orderId": 1})  # no SL evidence
    client.close_position_market = AsyncMock(return_value={"orderId": 99})
    svc = OrderService(client, _settings(auto_flatten_if_sl_unverified=True), PreviewStore())
    prev = await svc.preview(_ticket(trigger_mode="manual"))
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["status"] == "placed_manual"
    # body must NOT carry an exchange SL trigger
    assert "stopLossPrice" not in out["request"]
    # manual must never auto-flatten
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_mode_scale_out_warns_ladder_not_placed():
    """F-F1: trigger_mode=manual + scale_out=True places NO exchange triggers
    (TP1/TP2 ladder included) — this must be surfaced as a clear warning, not
    silently dropped, so the user knows the ladder was never placed."""
    client = _happy_client({"orderId": 1})
    client.exchange_id = "hyperliquid"  # scale_out is HL-only
    svc = OrderService(client, _settings(auto_flatten_if_sl_unverified=True), PreviewStore())
    prev = await svc.preview(
        _ticket(trigger_mode="manual", scale_out=True, take_profit=102_000.0,
                tp2=104_000.0, tp1_share=0.5)
    )
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    assert out["status"] == "placed_manual"
    assert "takeProfitPrice2" not in out["request"]
    assert any("SCALE-OUT" in w and "IGNORIERT" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_auto_mode_still_attaches_sl():
    """Default (auto) keeps the exchange SL trigger in the body."""
    client = _happy_client({"orderId": 1, "slTriggerOid": 5})
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())  # default trigger_mode=auto
    out = await svc.confirm(prev["token"])
    assert "stopLossPrice" in out["request"]
    assert out["status"] == "placed"


@pytest.mark.asyncio
async def test_partial_close_fraction_rounds_to_lot():
    """fraction closes that share of the CURRENT hold, rounded to vol_unit."""
    client = MagicMock()
    client.exchange_id = "mexc"
    client.contract_meta = AsyncMock(return_value=_contract(vol_unit=1.0, min_vol=1.0))
    # current hold = 7 contracts; 25% = 1.75 → floor to 1
    client.positions = AsyncMock(
        return_value=[{"symbol": "BTC_USDT", "positionType": 1, "holdVol": 7.0,
                       "holdAvgPrice": 100.0}]
    )
    client.close_position_market = AsyncMock(return_value={"orderId": 9})
    svc = OrderService(client, _settings(), PreviewStore())
    out = await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.25)
    assert out["ok"] is True
    assert out["closed_vol"] == 1.0  # 1.75 floored to lot step 1.0
    kwargs = client.close_position_market.await_args.kwargs
    assert kwargs["vol"] == 1.0


@pytest.mark.asyncio
async def test_partial_close_below_min_blocked():
    """A share that rounds below min_vol is rejected with a clear error."""
    client = MagicMock()
    client.exchange_id = "mexc"
    client.contract_meta = AsyncMock(return_value=_contract(vol_unit=1.0, min_vol=2.0))
    client.positions = AsyncMock(
        return_value=[{"symbol": "BTC_USDT", "positionType": 1, "holdVol": 4.0,
                       "holdAvgPrice": 100.0}]
    )
    client.close_position_market = AsyncMock(return_value={"orderId": 9})
    svc = OrderService(client, _settings(), PreviewStore())
    with pytest.raises(OrderError) as ei:
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.25)  # 1 < min 2
    assert "minimum" in str(ei.value).lower()
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_summary_carries_trigger_mode():
    """Confirm modal must read the mode from the token-bound summary, not the
    live UI toggle — so the summary has to include trigger_mode."""
    client = _happy_client({"orderId": 1})
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket(trigger_mode="manual"))
    assert prev["summary"].get("trigger_mode") == "manual"
    prev2 = await svc.preview(_ticket())
    assert prev2["summary"].get("trigger_mode") == "auto"


# ── Fix 1: auto-flatten fill must be bot-safe (bound to OUR order fill) ──────


@pytest.mark.asyncio
async def test_flatten_uses_reported_fill_not_bot_inflated_hold():
    """When the response reports our fill, flatten that amount — NOT the hold
    difference (which a same-side external bot could inflate in the window)."""
    # Our order reports dealVol=0.4; hold jumps 50→60 (bot added ~9 same-side).
    client = _happy_client({"orderId": 1, "dealVol": 0.4})
    pre = _filled_pos(50.0)
    post = _filled_pos(60.0)
    client.positions = AsyncMock(side_effect=[pre, pre, pre, post, post])
    client.assets = AsyncMock(
        return_value=[
            {"currency": "USDT", "equity": 1_000_000.0, "availableBalance": 900_000.0}
        ]
    )
    svc = OrderService(
        client,
        _settings(max_risk_pct=100.0, max_notional_usdt=1_000_000.0),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket(vol=1.0))
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    client.close_position_market.assert_awaited_once()
    kwargs = client.close_position_market.await_args.kwargs
    # Only our reported 0.4 — not the 10-unit hold difference.
    assert kwargs["vol"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_flatten_reported_zero_fill_cancels_resting_despite_bot_hold():
    """Response reports 0 fill while hold rose (external bot) → treat as unfilled
    and cancel the resting entry; never market-close the bot's/our old size."""
    client = _happy_client({"orderId": 777, "dealVol": 0})
    pre = _filled_pos(10.0)
    post = _filled_pos(15.0)  # +5 from an external same-side bot
    client.positions = AsyncMock(side_effect=[pre, pre, pre, post, post])
    client.assets = AsyncMock(
        return_value=[
            {"currency": "USDT", "equity": 1_000_000.0, "availableBalance": 900_000.0}
        ]
    )
    client.cancel_order = AsyncMock(return_value={"success": True})
    svc = OrderService(
        client,
        _settings(max_risk_pct=100.0, max_notional_usdt=1_000_000.0),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket(vol=1.0))
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    client.close_position_market.assert_not_awaited()
    client.cancel_order.assert_awaited()
    assert out["flatten"] and out["flatten"].get("action") == "cancel_resting"


# ── Fix 2: side-aware / directional tick rounding ───────────────────────────


def test_round_to_unit_directions():
    from app.risk.sizing import round_to_unit

    # legacy default is unchanged
    assert round_to_unit(100.06, 0.1) == pytest.approx(100.1)
    assert round_to_unit(100.04, 0.1, "nearest") == pytest.approx(100.0)
    # explicit floor / ceil
    assert round_to_unit(100.06, 0.1, "down") == pytest.approx(100.0)
    assert round_to_unit(100.04, 0.1, "up") == pytest.approx(100.1)


def test_round_trigger_side_aware_conservative():
    from app.risk.sizing import round_trigger_to_unit

    # long stop floored (widened below entry) — risk never understated
    assert round_trigger_to_unit(
        99_999.96, 0.1, side="long", kind="sl"
    ) == pytest.approx(99_999.9)
    # short stop ceiled (widened above entry)
    assert round_trigger_to_unit(
        100_000.04, 0.1, side="short", kind="sl"
    ) == pytest.approx(100_000.1)
    # unknown side falls back to nearest (never worse than legacy)
    assert round_trigger_to_unit(
        100.06, 0.1, side="", kind="sl"
    ) == pytest.approx(100.1)


# ── Fix 3: /api/orders/open must surface a stop-order lookup failure ─────────


def _open_orders_settings():
    return Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s")


def test_orders_open_reports_stops_error(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main.get_settings", _open_orders_settings)
    mock = MagicMock()
    mock.open_orders = AsyncMock(return_value=[{"orderId": 1}])
    mock.open_stop_orders = AsyncMock(side_effect=MexcError("stoporder 404"))
    with TestClient(app) as tc:
        tc.app.state.mexc = mock
        tc.app.state.exchange = mock
        r = tc.get("/api/orders/open")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stop_orders"] == []
    assert body["stops_error"] and "stoporder" in body["stops_error"].lower()
    assert body["error"] is None


def test_orders_open_no_stops_error_on_success(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr("app.main.get_settings", _open_orders_settings)
    mock = MagicMock()
    mock.open_orders = AsyncMock(return_value=[])
    mock.open_stop_orders = AsyncMock(return_value=[])  # genuinely no triggers
    with TestClient(app) as tc:
        tc.app.state.mexc = mock
        tc.app.state.exchange = mock
        r = tc.get("/api/orders/open")
    body = r.json()
    assert body["stop_orders"] == []
    assert body["stops_error"] is None


# ── Fix 5: armed trading requires a non-empty LOCAL_API_TOKEN ────────────────


def test_armed_requires_local_token():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(trading_enabled=True, local_api_token="",
                 mexc_api_key="k", mexc_api_secret="s")
    # disarmed with empty token is allowed
    assert Settings(trading_enabled=False, local_api_token="").trading_enabled is False
    # armed WITH a token is allowed
    assert Settings(trading_enabled=True, local_api_token="secret").trading_enabled is True


# ── Fix 7: manual trigger can be fail-closed via ALLOW_MANUAL_TRIGGER=false ──


@pytest.mark.asyncio
async def test_manual_blocked_when_flag_off():
    client = _happy_client({"orderId": 1})
    svc = OrderService(client, _settings(allow_manual_trigger=False), PreviewStore())
    prev = await svc.preview(_ticket(trigger_mode="manual"))
    assert prev["ok"]
    with pytest.raises(OrderError) as ei:
        await svc.confirm(prev["token"])
    assert "manual" in str(ei.value).lower()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_allowed_by_default_flag_on():
    client = _happy_client({"orderId": 1})
    svc = OrderService(client, _settings(), PreviewStore())  # default True
    prev = await svc.preview(_ticket(trigger_mode="manual"))
    out = await svc.confirm(prev["token"])
    assert out["status"] == "placed_manual"
    client.place_order.assert_awaited()


# ── Fix 4: MEXC stop-order path fallback + externalOid history filter ────────
# NOTE: the concrete MEXC paths still need LIVE verification; these tests lock
# the fallback/filter logic against the mocked transport only.


@pytest.mark.asyncio
async def test_open_stop_orders_tries_fallback_paths():
    c = MexcClient("https://contract.mexc.com", "k", "s")
    calls: list[str] = []

    async def fake_request(method, path, *, params=None, private=False, **kw):
        calls.append(path)
        if len(calls) == 1:
            raise MexcError("first path 404")
        return [{"stopLossPrice": 99_000.0}]

    c._request = fake_request  # type: ignore[assignment]
    out = await c.open_stop_orders("BTC_USDT")
    assert out == [{"stopLossPrice": 99_000.0}]
    assert len(calls) == 2  # first failed, second succeeded


@pytest.mark.asyncio
async def test_order_by_external_oid_filters_history():
    c = MexcClient("https://contract.mexc.com", "k", "s")

    async def fake_request(method, path, *, params=None, private=False, **kw):
        if "external" in path:
            raise MexcError("external endpoint gone")
        # history returns unrelated + our order
        return {
            "resultList": [
                {"orderId": 1, "externalOid": "other-oid"},
                {"orderId": 2, "externalOid": "mine-123"},
            ]
        }

    c._request = fake_request  # type: ignore[assignment]
    out = await c.order_by_external_oid("BTC_USDT", "mine-123")
    assert out == [{"orderId": 2, "externalOid": "mine-123"}]


@pytest.mark.asyncio
async def test_manual_mode_persists_placed_manual_status_in_audit():
    """Manual order is written to the audit DB with status=placed_manual so the
    opened mode stays auditable; auto-flatten remains skipped even with a db."""
    client = _happy_client({"orderId": 1})  # no SL evidence anywhere
    db = MagicMock()
    db.insert_preview = AsyncMock()
    db.mark_preview_used = AsyncMock()
    db.insert_order = AsyncMock()
    svc = OrderService(
        client,
        _settings(auto_flatten_if_sl_unverified=True),
        PreviewStore(),
        db=db,
    )
    prev = await svc.preview(_ticket(trigger_mode="manual"))
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["status"] == "placed_manual"
    db.insert_order.assert_awaited()
    assert db.insert_order.await_args.kwargs["status"] == "placed_manual"
    # Manual mode must never auto-flatten, even with a db attached
    client.close_position_market.assert_not_awaited()


# ── F-01: pre_hold must fail-closed (no differential-flatten on unknown) ──────


@pytest.mark.asyncio
async def test_pre_hold_failure_prevents_auto_close():
    """If the pre-trade hold query fails AND the order response carries no fill
    quantity, auto-flatten must execute NEITHER a market-close NOR a
    differential-flatten — a failed pre_hold=0 would otherwise let the OLD
    position be read as a fresh fill and closed."""
    client = _happy_client({"orderId": 1})  # no fill field, no SL evidence
    # preview risk, confirm risk, pre_hold (FAILS), post-place SL pos, flatten
    client.positions = AsyncMock(
        side_effect=[
            [],  # preview existing risk
            [],  # confirm existing risk
            MexcError("positions endpoint down"),  # pre_hold — unreliable
            _filled_pos(1.0),  # post-place SL verify (no SL → unverified)
            _filled_pos(1.0),  # flatten hold_now
        ]
    )
    client.open_stop_orders = AsyncMock(return_value=[])  # SL genuinely missing
    svc = OrderService(
        client,
        _settings(auto_flatten_if_sl_unverified=True),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket())
    assert prev["ok"], prev.get("errors")
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    # Fail-closed: nothing is closed or cancelled on unknown pre_hold.
    client.close_position_market.assert_not_awaited()
    client.cancel_order.assert_not_awaited()
    assert out["flatten"] and out["flatten"].get("action") == "skipped_pre_hold_unknown"


# ── F-03: manual close must verify the close response semantically ────────────


# ── F-12: unlabeled trigger must be classified by side/entry, never assumed SL ──


def test_unlabeled_trigger_below_long_entry_is_sl():
    from app.main import _extract_position_sl_tp

    sl, tp = _extract_position_sl_tp(
        [{"triggerPrice": 99_000.0}], side="long", entry_price=100_000.0
    )
    assert sl == 99_000.0
    assert tp is None


def test_unlabeled_trigger_above_long_entry_is_tp_not_sl():
    """This is the F-12 bug: a TP above a long entry must NOT become a
    fabricated SL (which would make an unprotected position look protected)."""
    from app.main import _extract_position_sl_tp

    sl, tp = _extract_position_sl_tp(
        [{"triggerPrice": 102_000.0}], side="long", entry_price=100_000.0
    )
    assert tp == 102_000.0
    assert sl is None


def test_unlabeled_trigger_above_short_entry_is_sl():
    from app.main import _extract_position_sl_tp

    sl, tp = _extract_position_sl_tp(
        [{"triggerPrice": 101_000.0}], side="short", entry_price=100_000.0
    )
    assert sl == 101_000.0
    assert tp is None


def test_unlabeled_trigger_below_short_entry_is_tp():
    from app.main import _extract_position_sl_tp

    sl, tp = _extract_position_sl_tp(
        [{"triggerPrice": 98_000.0}], side="short", entry_price=100_000.0
    )
    assert tp == 98_000.0
    assert sl is None


def test_unlabeled_trigger_unresolvable_is_unknown_not_sl():
    """No side/entry known → neither sl nor tp assigned. Never assume SL."""
    from app.main import _extract_position_sl_tp

    sl, tp = _extract_position_sl_tp([{"triggerPrice": 99_000.0}], side=None, entry_price=None)
    assert sl is None
    assert tp is None


def test_labeled_stop_still_classified_as_sl_regardless_of_side():
    """An explicit 'stop'/'sl' label still wins over side/entry inference."""
    from app.main import _extract_position_sl_tp

    sl, tp = _extract_position_sl_tp(
        [{"triggerPrice": 99_000.0, "orderType": "stop_market"}],
        side="long",
        entry_price=100_000.0,
    )
    assert sl == 99_000.0
    assert tp is None


@pytest.mark.asyncio
async def test_close_inner_error_not_reported_closed():
    """An outwardly-200 close response carrying an inner error (Hyperliquid
    nests rejections inside statuses[]) must NOT be reported as closed/ok."""
    inner_error_resp = {
        "status": "ok",
        "response": {
            "data": {"statuses": [{"error": "Order could not immediately match"}]}
        },
    }
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.contract_meta = AsyncMock(return_value=_contract())
    client.positions = AsyncMock(
        return_value=[
            {"symbol": "BTC_USDT", "positionType": 1, "holdVol": 1.0,
             "holdAvgPrice": 100_000.0}
        ]
    )
    client.close_position_market = AsyncMock(return_value=inner_error_resp)
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(client, _settings(), PreviewStore(), db=db)
    with pytest.raises(OrderError) as ei:
        await svc.close_position(symbol="BTC_USDT", side="long")
    assert "reject" in str(ei.value).lower() or "still" in str(ei.value).lower()
    # Audit must record the failure, never status=closed.
    db.insert_order.assert_awaited()
    assert db.insert_order.await_args.kwargs["status"] == "close_error"


# ── F-03 follow-up: post-close position re-read (partial-fill detection) ──────
#
# A marketable IOC close can be ACCEPTED (transport 200, no inner error, a
# `filled` present) yet only PARTIALLY fill, leaving a residual position open.
# The inner-error check alone would still report it `closed`/ok. The service
# must re-read the live position after a close and only claim fully closed when
# the residual matches what we intended to leave.


def _close_client(*, first_hold, reread, close_resp=None):
    """MEXC client whose positions() returns `first_hold` then `reread`.

    `reread` may be a list (positions rows) or an exception instance (query
    failure). `close_resp` is the market-close response (benign by default).
    """
    client = MagicMock()
    client.exchange_id = "mexc"
    client.contract_meta = AsyncMock(return_value=_contract(vol_unit=1.0, min_vol=1.0))
    client.positions = AsyncMock(side_effect=[first_hold, reread])
    client.close_position_market = AsyncMock(
        return_value=close_resp if close_resp is not None else {"orderId": 9, "dealVol": 3.0}
    )
    return client


def _pos(hold, side_type=1):
    return [{"symbol": "BTC_USDT", "positionType": side_type, "holdVol": hold,
             "holdAvgPrice": 100_000.0}]


@pytest.mark.asyncio
async def test_close_partial_fill_not_reported_fully_closed():
    """Full close accepted, but reread shows a residual still open → NOT ok."""
    client = _close_client(first_hold=_pos(5.0), reread=_pos(2.0))
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(client, _settings(), PreviewStore(), db=db)
    out = await svc.close_position(symbol="BTC_USDT", side="long")
    assert out["ok"] is False
    assert out["residual_vol"] == 2.0
    assert out["status"] in ("partial", "close_incomplete")
    # Audit must NOT record status=closed for a residual position.
    assert db.insert_order.await_args.kwargs["status"] != "closed"


@pytest.mark.asyncio
async def test_close_full_fill_still_reports_ok():
    """A genuine full close (residual ~0) must still report ok:true — the
    verification must never block a legitimate complete close."""
    client = _close_client(first_hold=_pos(5.0), reread=[])  # nothing left open
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(client, _settings(), PreviewStore(), db=db)
    out = await svc.close_position(symbol="BTC_USDT", side="long")
    assert out["ok"] is True
    assert out["closed_vol"] == 5.0
    assert db.insert_order.await_args.kwargs["status"] == "closed"


@pytest.mark.asyncio
async def test_close_reread_failure_is_uncertain_not_closed():
    """If the post-close reread itself fails, fail-safe: uncertain, never a
    false fully-closed."""
    client = _close_client(first_hold=_pos(5.0), reread=MexcError("positions boom"))
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(client, _settings(), PreviewStore(), db=db)
    out = await svc.close_position(symbol="BTC_USDT", side="long")
    assert out["ok"] is False
    assert out["status"] == "close_unverified"
    assert out.get("residual_vol") is None
    assert db.insert_order.await_args.kwargs["status"] != "closed"


# ── F-04: unguarded r.json() after 2xx must not crash — treat as uncertain ───


@pytest.mark.asyncio
async def test_request_2xx_empty_body_raises_mexc_error_not_json_crash():
    """A 2xx response with an empty/non-JSON body must surface as a MexcError
    (the existing recovery path), never an unhandled JSONDecodeError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    c = MexcClient("https://contract.mexc.com", "k", "s")
    c._client = httpx.AsyncClient(
        base_url=c.base_url, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(MexcError):
        await c.place_order({"symbol": "BTC_USDT"})


@pytest.mark.asyncio
async def test_request_2xx_malformed_json_raises_mexc_error_not_crash():
    """Truncated/malformed JSON body on a 2xx response must also raise
    MexcError instead of an unhandled JSONDecodeError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"success": true, "data": {')

    c = MexcClient("https://contract.mexc.com", "k", "s")
    c._client = httpx.AsyncClient(
        base_url=c.base_url, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(MexcError):
        await c.place_order({"symbol": "BTC_USDT"})


@pytest.mark.asyncio
async def test_place_order_empty_body_2xx_recovers_via_external_oid():
    """A place-order response that is 2xx but empty/non-JSON must be treated
    as an UNCERTAIN outcome (order may be live) and reconciled via
    externalOid — exactly like the existing timeout-recovery path — not
    surfaced as a hard failure that hides a possibly-live order."""
    client = _happy_client({"orderId": 1})

    async def _place(_body):
        raise MexcError(
            "invalid JSON in response body: Expecting value: line 1 column 1 (char 0)"
        )

    client.place_order = AsyncMock(side_effect=_place)

    async def _by_ext(symbol, external_oid):
        return {"orderId": 42, "externalOid": external_oid, "symbol": symbol}

    client.order_by_external_oid = AsyncMock(side_effect=_by_ext)
    svc = OrderService(client, _settings(), PreviewStore())
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["ok"] is True
    assert "recovered_placed" in out["status"]
    assert any("DO NOT re-preview" in w for w in out["warnings"])


# ── F-05: stale/ambiguous stop-order endpoint must yield UNKNOWN, not MISSING ─


@pytest.mark.asyncio
async def test_open_stop_orders_all_paths_ambiguous_raises_not_empty():
    """A stale/deprecated stop-order endpoint that replies 2xx with an
    unrecognized shape (not a list, no resultList) must NOT be trusted as
    'no stop orders' — if every candidate path returns such an ambiguous
    body, open_stop_orders must raise (so callers mark the check UNKNOWN),
    not silently return []."""
    c = MexcClient("https://contract.mexc.com", "k", "s")

    async def fake_request(method, path, *, params=None, private=False, **kw):
        return {}  # unrecognized: no resultList key, not a list

    c._request = fake_request  # type: ignore[assignment]
    with pytest.raises(MexcError):
        await c.open_stop_orders("BTC_USDT")


@pytest.mark.asyncio
async def test_open_stop_orders_trusts_recognized_empty_list():
    """A genuinely recognized empty result (bare list) is still trustworthy —
    the fix must not turn every empty response into a false UNKNOWN."""
    c = MexcClient("https://contract.mexc.com", "k", "s")

    async def fake_request(method, path, *, params=None, private=False, **kw):
        return []

    c._request = fake_request  # type: ignore[assignment]
    out = await c.open_stop_orders("BTC_USDT")
    assert out == []


@pytest.mark.asyncio
async def test_sl_ambiguous_stop_endpoint_is_unknown_not_missing_no_flatten():
    """If the stop-order lookup returns an ambiguous/empty body (client
    raises), SL state must be UNKNOWN (checked=False), never MISSING —
    auto-flatten must not close a possibly-protected position."""
    client = _happy_client({"orderId": 1}, post_hold=1.0)
    client.open_stop_orders = AsyncMock(
        side_effect=MexcError(
            "unrecognized stop-order response shape from all candidate paths"
        )
    )
    svc = OrderService(
        client,
        _settings(auto_flatten_if_sl_unverified=True),
        PreviewStore(),
    )
    prev = await svc.preview(_ticket())
    assert prev["ok"]
    out = await svc.confirm(prev["token"])
    assert out["sl_verified"] is False
    assert out["sl_checked"] is False
    assert out["status"] == "placed_sl_unknown"
    client.close_position_market.assert_not_awaited()
