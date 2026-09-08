"""Real HyperliquidClient with a mocked SDK exchange/info (no keys, no network).

Covers the untested place_order mapping + cloid stamping + timeout recovery
(audit Backend-C1 / test T1).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.hyperliquid.client import HyperliquidClient, external_oid_to_cloid
from app.hyperliquid.errors import HyperliquidError

_OK = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"filled": {"oid": 123, "totalSz": "0.01", "avgPx": "100"}}]}
    },
}

_CANCEL_OK = {
    "status": "ok",
    "response": {"type": "cancel", "data": {"statuses": ["success"]}},
}


def _resp_filled(sz: float, oid: int = 123) -> dict:
    """An HL order response reporting a FILL of `sz` coins."""
    return {
        "status": "ok",
        "response": {
            "type": "order",
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
        "response": {
            "type": "order",
            "data": {"statuses": [{"resting": {"oid": oid}}]},
        },
    }


def _client() -> HyperliquidClient:
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    # Preload meta so _asset_row() needs no network (now a (ts, meta) tuple).
    c._meta_cache = (
        time.monotonic(),
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


@pytest.mark.asyncio
async def test_hl_contract_meta_marks_delisted_coin_not_api_allowed():
    c = _client()
    c._meta_cache = (
        time.monotonic(),
        {
            "universe": [
                {
                    "name": "BTC",
                    "szDecimals": 5,
                    "maxLeverage": 50,
                    "isDelisted": True,
                }
            ]
        },
    )

    meta = await c.contract_meta("BTC")

    assert meta.api_allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("is_delisted", [0, 1, "true", "false"])
async def test_hl_contract_meta_rejects_invalid_delisted_marker(is_delisted):
    c = _client()
    c._meta_cache = (
        time.monotonic(),
        {
            "universe": [
                {
                    "name": "BTC",
                    "szDecimals": 5,
                    "maxLeverage": 50,
                    "isDelisted": is_delisted,
                }
            ]
        },
    )

    with pytest.raises(HyperliquidError, match="isDelisted"):
        await c.contract_meta("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_name", [True, 7, ["BTC"], {"coin": "BTC"}])
async def test_hl_contract_meta_rejects_nonstring_market_identity(invalid_name):
    c = _client()
    c._meta_cache = (
        time.monotonic(),
        {
            "universe": [
                {
                    "name": invalid_name,
                    "szDecimals": 5,
                    "maxLeverage": 50,
                }
            ]
        },
    )

    with pytest.raises(HyperliquidError, match="Unknown Hyperliquid coin"):
        await c.contract_meta(str(invalid_name).upper())


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_row", [7, None, "BTC"])
async def test_hl_contract_meta_skips_malformed_universe_rows(invalid_row):
    c = _client()
    c._meta_cache = (
        time.monotonic(),
        {
            "universe": [
                invalid_row,
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            ]
        },
    )

    meta = await c.contract_meta("BTC")

    assert meta.symbol == "BTC"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["szDecimals", "maxLeverage"])
async def test_hl_contract_meta_rejects_missing_required_limits(missing_field):
    row = {"name": "BTC", "szDecimals": 5, "maxLeverage": 50}
    row.pop(missing_field)
    c = _client()
    c._meta_cache = (time.monotonic(), {"universe": [row]})

    with pytest.raises(HyperliquidError, match=missing_field):
        await c.contract_meta("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["szDecimals", "maxLeverage"])
async def test_hl_contract_meta_rejects_boolean_numeric_limits(field):
    row = {"name": "BTC", "szDecimals": 5, "maxLeverage": 50}
    row[field] = True
    c = _client()
    c._meta_cache = (time.monotonic(), {"universe": [row]})

    with pytest.raises(HyperliquidError, match=field):
        await c.contract_meta("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["szDecimals", "maxLeverage"])
async def test_hl_contract_meta_rejects_fractional_integer_limits(field):
    row = {"name": "BTC", "szDecimals": 5, "maxLeverage": 50}
    row[field] = 1.5
    c = _client()
    c._meta_cache = (time.monotonic(), {"universe": [row]})

    with pytest.raises(HyperliquidError, match=field):
        await c.contract_meta("BTC")


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
async def test_set_leverage_accepts_documented_default_success():
    c = _client()
    expected = {"status": "ok", "response": {"type": "default"}}
    c._exchange.update_leverage = MagicMock(return_value=expected)

    assert await c.set_leverage("BTC", 5, 1) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"status": "err", "response": "bad leverage"},
        {"status": "ok"},
        {"status": "ok", "response": {}},
        {},
        None,
    ],
)
async def test_set_leverage_rejects_error_or_uncertain_response(response):
    c = _client()
    c._exchange.update_leverage = MagicMock(return_value=response)

    with pytest.raises(HyperliquidError, match="set_leverage"):
        await c.set_leverage("BTC", 5, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leverage", "open_type"),
    [(True, 1), (1.5, 1), (0, 1), (-1, 1), (5, True), (5, 0), (5, 3)],
)
async def test_set_leverage_rejects_invalid_inputs_before_send(leverage, open_type):
    c = _client()

    with pytest.raises(HyperliquidError):
        await c.set_leverage("BTC", leverage, open_type)

    c._exchange.update_leverage.assert_not_called()


@pytest.mark.asyncio
async def test_set_leverage_rejects_empty_symbol_before_send():
    c = _client()

    with pytest.raises(HyperliquidError, match="symbol"):
        await c.set_leverage("  ", 5, 1)

    c._exchange.update_leverage.assert_not_called()


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
@pytest.mark.parametrize("invalid_symbol", [True, 77, ["BTC"], {"coin": "BTC"}])
async def test_hl_place_order_rejects_nonstring_symbol_before_send(invalid_symbol):
    c = _client()
    c._meta_cache = (
        time.monotonic(),
        {
            "universe": [
                {
                    "name": str(invalid_symbol).upper(),
                    "szDecimals": 5,
                    "maxLeverage": 50,
                }
            ]
        },
    )

    with pytest.raises(HyperliquidError, match="symbol"):
        await c.place_order(
            {
                "symbol": invalid_symbol,
                "side": "long",
                "type": "market",
                "vol": 0.01,
            }
        )

    c._exchange.market_open.assert_not_called()
    c._exchange.order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"status": "ok"},
        {"status": "ok", "response": {"type": "default"}},
        {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": []}},
        },
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"oid": 1}}]},
            },
        },
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"filled": {"oid": 1, "totalSz": "NaN"}}]
                },
            },
        },
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"filled": {"oid": True, "totalSz": "0.01"}}]
                },
            },
        },
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": True}}]},
            },
        },
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"oid": 0, "totalSz": "0.01"}}]},
            },
        },
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": 0}}]},
            },
        },
    ],
)
async def test_hl_place_order_rejects_unrecognized_success_shape(response):
    c = _client()
    c._exchange.market_open = MagicMock(return_value=response)

    with pytest.raises(HyperliquidError, match="uncertain order-create response"):
        await c.place_order(
            {
                "symbol": "BTC",
                "side": 1,
                "type": "market",
                "vol": 0.01,
                "stopLossPrice": 99.0,
            }
        )
    c._exchange.order.assert_not_called()


@pytest.mark.asyncio
async def test_hl_place_order_rejects_close_and_unknown_sides():
    c = _client()
    for bad in (True, 2, "4", None, "x"):
        with pytest.raises(HyperliquidError):
            await c.place_order(
                {"symbol": "BTC", "side": bad, "type": "market", "vol": 0.01}
            )
    c._exchange.market_open.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", [True, False, 2, "x", None])
async def test_hl_place_order_rejects_unknown_order_type(order_type):
    c = _client()

    with pytest.raises(HyperliquidError, match="order type"):
        await c.place_order(
            {
                "symbol": "BTC",
                "side": 1,
                "type": order_type,
                "vol": 0.01,
                "price": 100.0,
            }
        )

    c._exchange.market_open.assert_not_called()
    c._exchange.order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"symbol": "BTC", "side": 1, "type": "market", "vol": float("nan")},
        {
            "symbol": "BTC",
            "side": 1,
            "type": "limit",
            "vol": 0.01,
            "price": float("nan"),
        },
        {
            "symbol": "BTC",
            "side": 1,
            "type": "market",
            "vol": 0.01,
            "stopLossPrice": float("nan"),
        },
    ],
)
async def test_hl_place_order_rejects_nonfinite_inputs_before_send(body):
    c = _client()

    with pytest.raises(HyperliquidError, match="[Nn]on-finite"):
        await c.place_order(body)

    c._exchange.market_open.assert_not_called()
    c._exchange.order.assert_not_called()


@pytest.mark.asyncio
async def test_hl_place_order_rejects_negative_stop_before_entry_send():
    c = _client()

    with pytest.raises(HyperliquidError, match="stop-loss price"):
        await c.place_order(
            {
                "symbol": "BTC",
                "side": 1,
                "type": "market",
                "vol": 0.01,
                "stopLossPrice": -1.0,
            }
        )

    c._exchange.market_open.assert_not_called()
    c._exchange.order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ladder_fields",
    [
        {"takeProfitPrice2": "not-a-number", "tp1Share": 0.5},
        {"takeProfitPrice2": float("nan"), "tp1Share": 0.5},
        {"takeProfitPrice2": 120.0, "tp1Share": "not-a-number"},
        {"takeProfitPrice2": 120.0, "tp1Share": float("nan")},
    ],
)
async def test_hl_place_order_validates_tp_ladder_before_entry_send(ladder_fields):
    c = _client()
    body = {
        "symbol": "BTC",
        "side": 1,
        "type": "market",
        "vol": 0.01,
        "takeProfitPrice": 110.0,
        **ladder_fields,
    }

    with pytest.raises(HyperliquidError):
        await c.place_order(body)

    c._exchange.market_open.assert_not_called()
    c._exchange.order.assert_not_called()


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
        return_value={
            "status": "order",
            "order": {"order": {"coin": "BTC", "oid": 7}, "status": "filled"},
        }
    )
    c._info = info
    res = await c.order_by_external_oid("BTC", "mlt-xyz")
    assert res and res.get("match") == "cloid"
    assert "mlt-xyz" in str(res)  # service.py recovery guard passes
    info.query_order_by_cloid.assert_called_once()


def _cloid_status(inner_status: str, *, orig_sz=None, sz=None) -> dict:
    """An HL orderStatus-by-cloid response reporting `inner_status`.

    HL order object carries origSz/sz as float-strings; filled = origSz - sz.
    """
    order: dict = {"coin": "BTC", "oid": 7}
    if orig_sz is not None:
        order["origSz"] = orig_sz
    if sz is not None:
        order["sz"] = sz
    return {
        "status": "order",
        "order": {"order": order, "status": inner_status},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dead_status",
    ["canceled", "rejected", "marginCanceled", "reduceOnlyCanceled", "scheduledCancel"],
)
async def test_order_by_external_oid_rejects_zero_fill_dead_cloid_status(dead_status):
    """X-05: a ZERO-FILL cancel/reject (origSz == sz → filled 0) must NOT be
    reported as recovered. Mirrors MEXC _mexc_state_is_dead → return {} so the
    caller runs its fail-closed hard error (no phantom position)."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value=_cloid_status(dead_status, orig_sz="0.01", sz="0.01")
    )
    c._info = info
    # No open/stop fallback either → nothing to recover.
    c._exchange.open_orders = MagicMock(return_value=[])
    c._exchange.frontend_open_orders = MagicMock(return_value=[])
    res = await c.order_by_external_oid("BTC", "mlt-dead")
    assert res == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_status", ["canceled", "marginCanceled", "reduceOnlyCanceled"]
)
async def test_order_by_external_oid_partial_fill_then_cancel_is_recovered(cancel_status):
    """Money-critical: an IOC/market order can PARTIALLY fill then cancel the
    remainder (origSz > sz → filled > 0) = a REAL open position. It must run the
    recovery/verify path, NOT be discarded as dead (else the filled part is
    unprotected + baits a double entry)."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value=_cloid_status(cancel_status, orig_sz="0.01", sz="0.004")
    )
    c._info = info
    res = await c.order_by_external_oid("BTC", "mlt-partial")
    assert res and res.get("match") == "cloid"
    assert "mlt-partial" in str(res)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["filled", "open", "triggered", "resting"])
async def test_order_by_external_oid_accepts_live_cloid_status(live_status):
    """Live/recoverable inner states remain valid cloid hits (recovered)."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(return_value=_cloid_status(live_status))
    c._info = info
    res = await c.order_by_external_oid("BTC", "mlt-live")
    assert res and res.get("match") == "cloid"
    assert "mlt-live" in str(res)


@pytest.mark.asyncio
async def test_order_by_external_oid_missing_inner_status_not_dead():
    """Fail-safe: a missing/unexpected inner status is NOT treated as dead —
    a legitimate recoverable order must never be falsely discarded."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value={
            "status": "order",
            "order": {"order": {"coin": "BTC", "oid": 7}},
        }
    )
    c._info = info
    res = await c.order_by_external_oid("BTC", "mlt-nostatus")
    assert res and res.get("match") == "cloid"


@pytest.mark.asyncio
async def test_order_by_external_oid_cancel_missing_sizes_not_dead():
    """Fail-safe: a cancel/reject status WITHOUT parseable origSz/sz cannot be
    proven zero-fill → NOT treated as dead (never discard a possibly-filled
    order)."""
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(return_value=_cloid_status("canceled"))
    c._info = info
    res = await c.order_by_external_oid("BTC", "mlt-nosize")
    assert res and res.get("match") == "cloid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("orig_sz", "remaining"),
    [("0", "0"), ("0.01", "0.02"), ("-1", "-1"), ("0.01", "-0.01")],
)
async def test_order_by_external_oid_malformed_sizes_cannot_prove_dead(
    orig_sz, remaining
):
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value=_cloid_status(
            "canceled", orig_sz=orig_sz, sz=remaining
        )
    )
    c._info = info

    result = await c.order_by_external_oid("BTC", "mlt-malformed-size")
    assert result["match"] == "cloid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        {"coin": "ETH", "oid": 7},
        {"coin": "BTC"},
        {"coin": "BTC", "oid": True},
        {"coin": "BTC", "oid": 0},
        {"coin": "BTC", "oid": -1},
        {"coin": "BTC", "oid": "abc"},
        {"coin": "BTC", "oid": 7, "cloid": "0x" + "0" * 32},
    ],
)
async def test_order_by_external_oid_rejects_wrong_recovery_identity(order):
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value={"status": "order", "order": {"order": order, "status": "filled"}}
    )
    info.open_orders = MagicMock(return_value=[])
    info.frontend_open_orders = MagicMock(return_value=[])
    c._info = info

    assert await c.order_by_external_oid("BTC", "mlt-wrong-identity") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_coin", [True, 7, ["BTC"], {"coin": "BTC"}])
async def test_order_by_external_oid_rejects_nonstring_cloid_coin(invalid_coin):
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(
        return_value={
            "status": "order",
            "order": {
                "order": {"coin": invalid_coin, "oid": 7},
                "status": "filled",
            },
        }
    )
    info.open_orders = MagicMock(return_value=[])
    info.frontend_open_orders = MagicMock(return_value=[])
    c._info = info

    result = await c.order_by_external_oid(
        str(invalid_coin).upper(), "mlt-invalid-cloid-coin"
    )

    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_symbol", [True, 7, ["BTC"], {"coin": "BTC"}])
async def test_order_by_external_oid_fallback_rejects_nonstring_symbol(
    invalid_symbol,
):
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(return_value={"status": "unknownOid"})
    c._info = info
    wanted = external_oid_to_cloid("mlt-invalid-fallback-symbol").to_raw()
    c.open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 7,
                "symbol": invalid_symbol,
                "raw": {"cloid": wanted},
            }
        ]
    )
    c.open_stop_orders = AsyncMock(return_value=[])

    result = await c.order_by_external_oid(
        str(invalid_symbol).upper(), "mlt-invalid-fallback-symbol"
    )

    assert result == {}


@pytest.mark.asyncio
async def test_order_by_external_oid_fallback_rejects_other_symbol_cloid():
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.query_order_by_cloid = MagicMock(return_value={"status": "unknownOid"})
    c._info = info
    wanted = external_oid_to_cloid("mlt-fallback-symbol").to_raw()
    c.open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 7,
                "symbol": "ETH",
                "raw": {"coin": "ETH", "cloid": wanted},
            }
        ]
    )
    c.open_stop_orders = AsyncMock(return_value=[])

    assert await c.order_by_external_oid("BTC", "mlt-fallback-symbol") == {}


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


@pytest.mark.asyncio
async def test_place_stop_order_rejects_unrecognized_success_shape():
    c = _client()
    c._exchange.order = MagicMock(return_value={"status": "ok"})

    out = await c.place_stop_order(
        "BTC", position_side="long", vol=0.01, trigger_px=99_000.0, tpsl="sl"
    )

    assert out["orderId"] is None
    assert out["error"] == "unrecognized order response"


@pytest.mark.asyncio
async def test_place_stop_order_rejects_nonfinite_volume_before_send():
    c = _client()

    with pytest.raises(HyperliquidError, match="[Nn]on-finite"):
        await c.place_stop_order(
            "BTC",
            position_side="long",
            vol=float("nan"),
            trigger_px=99_000.0,
        )

    c._exchange.order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_kwargs",
    [
        {"tpsl": "unknown"},
        {"reduce_only": False},
    ],
)
async def test_place_stop_order_rejects_unsafe_trigger_semantics_before_send(
    unsafe_kwargs,
):
    c = _client()

    with pytest.raises(HyperliquidError):
        await c.place_stop_order(
            "BTC",
            position_side="long",
            vol=0.01,
            trigger_px=99_000.0,
            **unsafe_kwargs,
        )

    c._exchange.order.assert_not_called()


def _fake_info(coin: str, szi: float):
    """Info stub whose user_state reports one position (coin/szi)."""
    info = MagicMock()
    info.user_state = MagicMock(
        return_value={
            "marginSummary": {"accountValue": "1000"},
            "assetPositions": [
                {"position": {"coin": coin, "szi": str(szi)}}
            ]
        }
    )
    return info


@pytest.mark.asyncio
async def test_close_rejects_degraded_live_user_state_as_unknown():
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.user_state = MagicMock(return_value=None)
    c._info = info
    c._exchange.market_close = MagicMock(return_value=_OK)

    with pytest.raises(HyperliquidError, match="unrecognized/degraded shape"):
        await c.close_position_market("BTC", side="long", vol=0.5)

    c._exchange.market_close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_coin", [True, 7, ["BTC"], {"coin": "BTC"}])
async def test_close_recheck_rejects_nonstring_market_identity(invalid_coin):
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info(invalid_coin, 0.5)
    c._exchange.market_close = MagicMock(return_value=_OK)

    with pytest.raises(HyperliquidError, match="no open long position"):
        await c.close_position_market(
            str(invalid_coin).upper(), side="long", vol=0.5
        )

    c._exchange.market_close.assert_not_called()


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
@pytest.mark.parametrize(
    ("raw_szi", "side"),
    [(True, "long"), (float("inf"), "long"), (float("-inf"), "short")],
)
async def test_close_rejects_invalid_live_position_size(raw_szi, side):
    c = _client()
    c.account_address = "0x" + "a" * 40
    info = MagicMock()
    info.user_state = MagicMock(
        return_value={
            "marginSummary": {},
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": raw_szi}}
            ]
        }
    )
    c._info = info
    c._exchange.market_close = MagicMock(return_value=_OK)

    with pytest.raises(HyperliquidError):
        await c.close_position_market("BTC", side=side, vol=0.5)

    c._exchange.market_close.assert_not_called()


@pytest.mark.asyncio
async def test_close_rejects_nonfinite_volume_instead_of_closing_full_position():
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)
    c._exchange.market_close = MagicMock(return_value=_OK)

    with pytest.raises(HyperliquidError, match="[Nn]on-finite"):
        await c.close_position_market("BTC", side="long", vol=float("nan"))

    c._exchange.market_close.assert_not_called()


@pytest.mark.asyncio
async def test_close_rejects_negative_volume_instead_of_closing_full_position():
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)
    c._exchange.market_close = MagicMock(return_value=_OK)

    with pytest.raises(HyperliquidError, match="close volume"):
        await c.close_position_market("BTC", side="long", vol=-0.1)

    c._exchange.market_close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_invalid_volume", [0, ""])
async def test_close_rejects_explicit_invalid_volume_instead_of_closing_full_position(
    explicit_invalid_volume,
):
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)
    c._exchange.market_close = MagicMock(return_value=_OK)

    with pytest.raises(HyperliquidError, match="close volume"):
        await c.close_position_market(
            "BTC", side="long", vol=explicit_invalid_volume
        )

    c._exchange.market_close.assert_not_called()


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


@pytest.mark.asyncio
async def test_close_unrecognized_success_shape_raises_for_reconciliation():
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)
    c._exchange.market_close = MagicMock(return_value={"status": "ok"})

    with pytest.raises(HyperliquidError, match="uncertain close response"):
        await c.close_position_market("BTC", side="long", vol=0.5)


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
async def test_hl_sl_rounds_toward_entry_via_place_order():
    """X2-05 (flips prior Task-15 test): a long SL's exchange-tick rounding must
    round TOWARD entry, never wider. On HL price_unit==0, so the risk gate does
    NOT round the SL (it computes risk on the raw value); if the client then
    FLOORED the SL (away from entry, further below), the placed loss would exceed
    the gate-approved risk by up to one tick. Correct: CEIL a long SL (up, toward
    entry) so the placed risk is never larger than the gate saw. At szDecimals=5
    (max 1 decimal) a raw SL of 99.95 must ceil to 100.0, never floor to 99.9."""
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
    assert trig[0].args[3] == pytest.approx(100.0)  # ceil toward entry, not 99.9
    assert trig[0].args[3] >= 99.95  # never wider (further from entry) than raw


def test_hl_sl_rounds_toward_entry_never_wider():
    """X2-05 unit proof for round_hl_price_side_aware. SL rounds TOWARD entry
    (long ceil / short floor) so realized risk is never wider than the raw,
    gate-approved value; TP rounds TOWARD entry too (long floor / short ceil) so
    RRR is never overstated at placement. szDecimals=5 → 0.1 tick.

    Concrete: long entry 100, raw SL 95.03 → OLD floor 95.0 (risk 5.0) vs NEW
    ceil 95.1 (risk 4.9 ≤ the 4.97 the gate approved)."""
    from app.hyperliquid.client import round_hl_price_side_aware as r

    # long SL: ceil (up, toward entry) — risk 100-95.1 = 4.9 < floor's 5.0
    assert r(95.03, 5, is_buy=True, kind="sl") == pytest.approx(95.1)
    assert r(95.03, 5, is_buy=True, kind="sl") >= 95.03
    # short SL (above entry): floor (down, toward entry) — risk never wider
    assert r(104.97, 5, is_buy=False, kind="sl") == pytest.approx(104.9)
    assert r(104.97, 5, is_buy=False, kind="sl") <= 104.97
    # long TP (above entry): floor (toward entry) — reward/RRR never overstated
    assert r(104.97, 5, is_buy=True, kind="tp") == pytest.approx(104.9)
    assert r(104.97, 5, is_buy=True, kind="tp") <= 104.97
    # short TP (below entry): ceil (toward entry) — RRR never overstated
    assert r(95.03, 5, is_buy=False, kind="tp") == pytest.approx(95.1)
    assert r(95.03, 5, is_buy=False, kind="tp") >= 95.03


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


# ── Finding 1 (defense-in-depth): position-changing mutations must EVICT the
# user_state cache, so even a non-fresh reader sees the post-trade account and
# never a place/close/stop/cancel-stale snapshot. ─────────────────────────────


def _warm_cache(c) -> None:
    c._user_state_cache = (
        time.monotonic(),
        _COMPLETE_STATE_FOR_INVAL,
        "0x" + "a" * 40,
    )


_COMPLETE_STATE_FOR_INVAL = {
    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50",
                      "totalNtlPos": "500"},
    "withdrawable": "950",
    "assetPositions": [],
}


@pytest.mark.asyncio
async def test_place_order_invalidates_user_state_cache():
    c = _client()
    _warm_cache(c)
    await c.place_order({"symbol": "BTC", "side": 1, "type": "market", "vol": 0.01})
    assert c._user_state_cache is None


@pytest.mark.asyncio
async def test_place_stop_order_invalidates_user_state_cache():
    c = _client()
    _warm_cache(c)
    await c.place_stop_order(
        "BTC", position_side="long", vol=0.01, trigger_px=99_000.0, tpsl="sl"
    )
    assert c._user_state_cache is None


@pytest.mark.asyncio
async def test_cancel_order_invalidates_user_state_cache():
    c = _client()
    c._exchange.cancel = MagicMock(return_value=_CANCEL_OK)
    _warm_cache(c)
    await c.cancel_order({"orderId": 123, "symbol": "BTC"})
    assert c._user_state_cache is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"orderId": True, "symbol": "BTC"},
        {"orderId": -1, "symbol": "BTC"},
        {"orderId": 1.5, "symbol": "BTC"},
        {"orderId": 7, "symbol": ""},
        {"orderId": 7, "symbol": True},
        {"orderId": 7, "symbol": 77},
        {"orderId": 7, "symbol": ["BTC"]},
        {"orderId": 7, "symbol": {"coin": "BTC"}},
        [],
    ],
)
async def test_hl_cancel_rejects_invalid_request_before_send(body):
    c = _client()
    c._exchange.cancel = MagicMock(return_value=_CANCEL_OK)

    with pytest.raises(HyperliquidError):
        await c.cancel_order(body)

    c._exchange.cancel.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {
            "status": "ok",
            "response": {
                "type": "cancel",
                "data": {"statuses": [{"error": "already filled"}]},
            },
        },
        {"status": "ok", "response": {"type": "cancel", "data": {}}},
        {"status": "ok", "response": {"type": "default"}},
        {},
        None,
    ],
)
async def test_cancel_order_rejects_error_or_uncertain_response(response):
    c = _client()
    c._exchange.cancel = MagicMock(return_value=response)

    with pytest.raises(HyperliquidError, match="cancel"):
        await c.cancel_order({"orderId": 123, "symbol": "BTC"})


@pytest.mark.asyncio
async def test_close_position_market_invalidates_user_state_cache():
    c = _client()
    c.account_address = "0x" + "a" * 40
    c._info = _fake_info("BTC", 0.5)  # live LONG matches request
    c._exchange.market_close = MagicMock(return_value=_OK)
    _warm_cache(c)
    await c.close_position_market("BTC", side="long", vol=0.5)
    assert c._user_state_cache is None
