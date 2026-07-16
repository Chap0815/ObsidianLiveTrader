"""Real HyperliquidClient with a mocked SDK exchange/info (no keys, no network).

Covers the untested place_order mapping + cloid stamping + timeout recovery
(audit Backend-C1 / test T1).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.hyperliquid.client import HyperliquidClient, external_oid_to_cloid
from app.hyperliquid.errors import HyperliquidError

_OK = {
    "status": "ok",
    "response": {
        "data": {"statuses": [{"filled": {"oid": 123, "totalSz": "0.01", "avgPx": "100"}}]}
    },
}


def _resp_filled(sz: float, oid: int = 123) -> dict:
    """An HL order response reporting a FILL of `sz` coins."""
    return {
        "status": "ok",
        "response": {
            "data": {
                "statuses": [
                    {"filled": {"oid": oid, "totalSz": str(sz), "avgPx": "100"}}
                ]
            }
        },
    }


def _resp_resting(oid: int = 777) -> dict:
    """An HL order response for a RESTING (unfilled) limit order."""
    return {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}},
    }


def _client() -> HyperliquidClient:
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    # Preload meta so _asset_row() needs no network (now a (ts, meta) tuple).
    c._meta_cache = (
        time.time(),
        {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
    )
    c._exchange = MagicMock()
    # A real market_open fills the requested size — echo `sz` (call arg #3) as the
    # reported fill so trigger sizing (now tied to the ACTUAL fill) is faithful.
    c._exchange.market_open = MagicMock(
        side_effect=lambda coin, is_buy, sz, *a, **k: _resp_filled(sz)
    )
    c._exchange.order = MagicMock(return_value=_OK)
    return c


def _sl_trigger_calls(c) -> list:
    return [
        call
        for call in c._exchange.order.call_args_list
        if isinstance(call.args[4], dict)
        and "trigger" in call.args[4]
        and call.args[4]["trigger"]["tpsl"] == "sl"
    ]


def _all_trigger_calls(c) -> list:
    return [
        call
        for call in c._exchange.order.call_args_list
        if isinstance(call.args[4], dict) and "trigger" in call.args[4]
    ]


def test_external_oid_to_cloid_is_valid_16_byte_hex():
    cl = external_oid_to_cloid("mlt-deadbeef")
    raw = cl.to_raw()
    assert raw.startswith("0x") and len(raw) == 34  # "0x" + 32 hex
    # deterministic
    assert external_oid_to_cloid("mlt-deadbeef").to_raw() == raw


@pytest.mark.asyncio
async def test_hl_place_order_side_and_type_mapping():
    c = _client()
    # market long -> market_open with is_buy True
    await c.place_order({"symbol": "BTC", "side": "long", "type": "market", "vol": 0.01})
    assert c._exchange.market_open.call_args.args[1] is True
    # market short -> is_buy False
    await c.place_order({"symbol": "BTC", "side": 3, "type": "market", "vol": 0.01})
    assert c._exchange.market_open.call_args.args[1] is False
    # limit long -> ex.order path
    await c.place_order(
        {"symbol": "BTC", "side": 1, "type": "limit", "vol": 0.01, "price": 100.0}
    )
    assert c._exchange.order.call_args.args[1] is True


@pytest.mark.asyncio
async def test_hl_place_order_rejects_close_and_unknown_sides():
    c = _client()
    for bad in (2, "4", None, "x"):
        with pytest.raises(HyperliquidError):
            await c.place_order(
                {"symbol": "BTC", "side": bad, "type": "market", "vol": 0.01}
            )
    c._exchange.market_open.assert_not_called()


@pytest.mark.asyncio
async def test_hl_place_order_stamps_cloid_from_external_oid():
    c = _client()
    await c.place_order(
        {"symbol": "BTC", "side": 1, "type": "market", "vol": 0.01, "externalOid": "mlt-abc123"}
    )
    passed = c._exchange.market_open.call_args.kwargs["cloid"]
    assert passed.to_raw() == external_oid_to_cloid("mlt-abc123").to_raw()


@pytest.mark.asyncio
async def test_order_by_external_oid_recovers_filled_order_via_cloid():
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value={"status": "order", "order": {"order": {"coin": "BTC"}, "status": "filled"}}
    )
    c._info = info
    res = await c.order_by_external_oid("BTC", "mlt-xyz")
    assert res and res.get("match") == "cloid"
    assert "mlt-xyz" in str(res)  # service.py recovery guard passes
    info.query_order_by_cloid.assert_called_once()


@pytest.mark.asyncio
async def test_place_stop_order_maps_reduce_only_trigger():
    c = _client()
    # long position -> protective stop is a SELL (is_buy False), reduce-only, trigger
    out = await c.place_stop_order(
        "BTC", position_side="long", vol=0.01, trigger_px=99_000.0, tpsl="sl"
    )
    assert out["orderId"] == 123  # _OK carries filled.oid 123
    assert out["error"] is None
    call = c._exchange.order.call_args
    assert call.args[1] is False                     # is_buy (close of long)
    ot = call.args[4]                                # order_type dict
    assert ot["trigger"]["tpsl"] == "sl"
    assert ot["trigger"]["isMarket"] is True
    assert call.kwargs["reduce_only"] is True

    # short position -> close is a BUY (is_buy True)
    await c.place_stop_order(
        "BTC", position_side="short", vol=0.01, trigger_px=101_000.0, tpsl="sl"
    )
    assert c._exchange.order.call_args.args[1] is True


def _fake_info(coin: str, szi: float):
    """Info stub whose user_state reports one position (coin/szi)."""
    info = MagicMock()
    info.user_state = MagicMock(
        return_value={
            "assetPositions": [
                {"position": {"coin": coin, "szi": str(szi)}}
            ]
        }
    )
    return info


@pytest.mark.asyncio
async def test_close_refuses_when_live_side_flipped():
    """F-08: the SDK's market_close ignores `side`. If the live position flipped
    from long to short between the service check and execution, the close must
    be REFUSED, not blindly executed against the new opposite side."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", -0.5)  # live is SHORT now
    c._exchange.market_close = MagicMock(return_value=_OK)
    with pytest.raises(HyperliquidError) as ei:
        await c.close_position_market("BTC", side="long", vol=0.5)  # requested LONG
    assert "flipped" in str(ei.value).lower() or "refus" in str(ei.value).lower()
    c._exchange.market_close.assert_not_called()  # never touched the short


@pytest.mark.asyncio
async def test_close_executes_when_side_matches():
    """Matching live side → close proceeds and returns the (ok) response."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)  # live LONG matches request
    c._exchange.market_close = MagicMock(return_value=_OK)
    out = await c.close_position_market("BTC", side="long", vol=0.5)
    assert out == _OK
    c._exchange.market_close.assert_called_once()
    # Size is capped to the live size.
    assert c._exchange.market_close.call_args.kwargs["sz"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_close_inner_error_raises_not_silent_ok():
    """F-03: an outwardly-ok market_close carrying an inner statuses[].error must
    raise, so the service never reports a still-open position as closed."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)  # live LONG matches
    inner_err = {
        "status": "ok",
        "response": {"data": {"statuses": [{"error": "insufficient margin"}]}},
    }
    c._exchange.market_close = MagicMock(return_value=inner_err)
    with pytest.raises(HyperliquidError) as ei:
        await c.close_position_market("BTC", side="long", vol=0.5)
    assert "reject" in str(ei.value).lower() or "insufficient" in str(ei.value).lower()


# ── F-02: protective triggers must be sized to the ACTUAL entry fill ─────────


@pytest.mark.asyncio
async def test_limit_partial_fill_sizes_sl_to_filled_not_requested():
    """A limit entry that only PARTIALLY fills must get an SL sized to the
    filled coins (0.006), not the requested 0.01 — a reduce-only stop sized to
    the request would over-reduce (and, with a pre-existing same-side position,
    could attach to the OLD position)."""
    c = _client()
    c._exchange.order = MagicMock(return_value=_resp_filled(0.006))
    out = await c.place_order(
        {
            "symbol": "BTC",
            "side": 1,
            "type": "limit",
            "vol": 0.01,
            "price": 100.0,
            "stopLossPrice": 99.0,
        }
    )
    trig = _sl_trigger_calls(c)
    assert len(trig) == 1
    assert trig[0].args[2] == pytest.approx(0.006)
    assert out["slTriggerOid"] is not None
    assert out.get("entryFilledSz") == pytest.approx(0.006)


@pytest.mark.asyncio
async def test_unfilled_limit_places_no_orphan_sl():
    """A resting (zero-fill) limit entry must place NO protective trigger — there
    is no position to protect — and must report itself as unfilled/unprotected
    (slTriggerOid None) so the service surfaces it as pending, never protected."""
    c = _client()
    c._exchange.order = MagicMock(return_value=_resp_resting(oid=777))
    out = await c.place_order(
        {
            "symbol": "BTC",
            "side": 1,
            "type": "limit",
            "vol": 0.01,
            "price": 100.0,
            "stopLossPrice": 99.0,
            "takeProfitPrice": 110.0,
        }
    )
    assert _all_trigger_calls(c) == []  # no SL and no TP orphan
    assert out["slTriggerOid"] is None
    assert out["tpTriggerOid"] is None
    assert out.get("entryFilledSz") == 0.0
    assert out.get("unfilled") is True
    assert out["orderId"] == 777


@pytest.mark.asyncio
async def test_market_partial_fill_sizes_sl_to_new_fill_only():
    """Requirement (3): even if a same-side position already exists, the new SL
    must be sized to the NEW fill only. The client sizes the reduce-only stop to
    the entry response's reported fill (0.004), never the requested 0.01."""
    c = _client()
    c._exchange.market_open = MagicMock(return_value=_resp_filled(0.004))
    out = await c.place_order(
        {
            "symbol": "BTC",
            "side": 1,
            "type": "market",
            "vol": 0.01,
            "stopLossPrice": 99.0,
        }
    )
    trig = _sl_trigger_calls(c)
    assert len(trig) == 1
    assert trig[0].args[2] == pytest.approx(0.004)
    assert out["slTriggerOid"] is not None
    assert out.get("entryFilledSz") == pytest.approx(0.004)


# ── O-06 / O-08: side-aware SL/TP rounding + close cloid ─────────────────────


@pytest.mark.asyncio
async def test_hl_sl_rounds_away_from_entry():
    """O-06: a long SL's exchange-tick rounding must never move it closer to
    entry. At szDecimals=5 (max 1 decimal), nearest-rounding of 99.95 goes UP
    to 100.0 (round_hl_price(99.95, 5) == 100.0) — side-aware rounding must
    floor it to 99.9 instead, so the placed trigger is never less protective
    than what was risk-approved."""
    c = _client()
    await c.place_order(
        {
            "symbol": "BTC",
            "side": 1,
            "type": "market",
            "vol": 0.01,
            "stopLossPrice": 99.95,
        }
    )
    trig = _sl_trigger_calls(c)
    assert len(trig) == 1
    assert trig[0].args[3] == pytest.approx(99.9)  # floor, never the nearest 100.0
    assert trig[0].args[3] <= 99.95  # never closer to entry than requested


@pytest.mark.asyncio
async def test_hl_close_stamps_cloid():
    """O-08: close_position_market stamps a deterministic Cloid onto the SDK's
    market_close call, so a transport timeout during a close can be recovered
    unambiguously via order_by_external_oid instead of guessing. Derived from
    "close:"+external_oid — namespaced so a caller reusing the ENTRY oid can
    never produce a cloid collision between entry and close order."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)  # live LONG matches requested close
    c._exchange.market_close = MagicMock(return_value=_OK)
    await c.close_position_market(
        "BTC", side="long", vol=0.5, external_oid="mlt-close-1"
    )
    c._exchange.market_close.assert_called_once()
    passed = c._exchange.market_close.call_args.kwargs["cloid"]
    assert passed.to_raw() == external_oid_to_cloid("close:mlt-close-1").to_raw()
    # Namespace-Garantie: nie identisch mit dem Entry-Cloid derselben OID.
    assert passed.to_raw() != external_oid_to_cloid("mlt-close-1").to_raw()


@pytest.mark.asyncio
async def test_hl_place_order_scale_out_places_two_tp_triggers():
    c = _client()
    await c.place_order({
        "symbol": "BTC", "side": 1, "type": "market", "vol": 0.02,
        "takeProfitPrice": 110.0, "takeProfitPrice2": 120.0, "tp1Share": 0.5,
    })
    # entry (market_open) + two TP triggers via ex.order
    trig_calls = [call for call in c._exchange.order.call_args_list
                  if isinstance(call.args[4], dict) and "trigger" in call.args[4]]
    assert len(trig_calls) == 2
    sizes = sorted(call.args[2] for call in trig_calls)
    assert sizes == pytest.approx([0.01, 0.01])
