"""HyperliquidClient exchange-integrity fail-safes (final audit C-1, M-2, H-2).

All tests drive the REAL client with a mocked SDK info object — no keys, no
network. They pin the money-safety invariant that a degraded/errored exchange
response must surface as an error (→ UNKNOWN downstream), never as a confident
"flat account" / "no stop orders".
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.hyperliquid.client import HyperliquidClient
from app.hyperliquid.errors import HyperliquidError


def _client(info: MagicMock) -> HyperliquidClient:
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    c.account_address = "0x" + "2" * 40
    c._info = info
    c._get_info = MagicMock(return_value=info)
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_name", [True, 7, ["BTC"], {"coin": "BTC"}, "   "]
)
async def test_list_symbols_does_not_stringify_invalid_names(invalid_name):
    info = MagicMock()
    info.meta = MagicMock(
        return_value={"universe": [{"name": "BTC"}, {"name": invalid_name}]}
    )

    assert await _client(info).list_symbols() == ["BTC"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_row", [{}, 7, None, "BTC"])
async def test_list_symbols_skips_malformed_universe_rows(invalid_row):
    info = MagicMock()
    info.meta = MagicMock(
        return_value={"universe": [{"name": "BTC"}, invalid_row]}
    )

    assert await _client(info).list_symbols() == ["BTC"]


@pytest.mark.asyncio
@pytest.mark.parametrize("universe", [{}, "bad", 7])
async def test_list_symbols_rejects_malformed_universe_collection(universe):
    info = MagicMock()
    info.meta = MagicMock(return_value={"universe": universe})

    with pytest.raises(HyperliquidError, match="metadata response"):
        await _client(info).list_symbols()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, "bad", {"universe": "bad"}])
async def test_malformed_metadata_is_not_cached_after_provider_recovers(malformed):
    info = MagicMock()
    info.meta = MagicMock(
        side_effect=[malformed, {"universe": [{"name": "BTC"}]}]
    )
    client = _client(info)

    with pytest.raises(HyperliquidError, match="metadata response"):
        await client.list_symbols()

    assert client._meta_cache is None
    assert await client.list_symbols() == ["BTC"]
    assert info.meta.call_count == 2


@pytest.mark.asyncio
async def test_metadata_cache_ttl_uses_monotonic_time(monkeypatch):
    info = MagicMock()
    info.meta = MagicMock(
        side_effect=[
            {"universe": [{"name": "BTC"}]},
            {"universe": [{"name": "ETH"}]},
        ]
    )
    client = _client(info)
    fake_time = MagicMock()
    fake_time.time = MagicMock(side_effect=[1_000.0, 100.0])
    fake_time.monotonic = MagicMock(side_effect=[1_000.0, 5_000.0])
    monkeypatch.setattr("app.hyperliquid.client.time", fake_time)

    assert await client.list_symbols() == ["BTC"]
    assert await client.list_symbols() == ["ETH"]
    assert info.meta.call_count == 2


def test_oi_history_sampling_uses_monotonic_time(monkeypatch):
    client = _client(MagicMock())
    fake_time = MagicMock()
    fake_time.time = MagicMock(side_effect=[1_000.0, 100.0])
    fake_time.monotonic = MagicMock(side_effect=[1_000.0, 5_000.0])
    monkeypatch.setattr("app.hyperliquid.client.time", fake_time)

    assert client._record_oi_and_change("BTC", 100.0) == (None, None)
    change_1h, change_4h = client._record_oi_and_change("BTC", 200.0)

    assert change_1h == 100.0
    assert change_4h is None
    assert client._oi_history["BTC"][-1][1] == 200.0


@pytest.mark.asyncio
@pytest.mark.parametrize("mid", ["NaN", "Infinity"])
async def test_ticker_rejects_nonfinite_mid_price(mid):
    info = MagicMock()
    info.all_mids = MagicMock(return_value={"BTC": mid})
    client = _client(info)

    with pytest.raises(HyperliquidError, match="Non-finite mid price"):
        await client.ticker("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize("mid", [0, -1])
async def test_ticker_rejects_nonpositive_mid_price(mid):
    info = MagicMock()
    info.all_mids = MagicMock(return_value={"BTC": mid})

    with pytest.raises(HyperliquidError, match="mid price"):
        await _client(info).ticker("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["o", "h", "l", "c", "v"])
async def test_klines_reject_nonfinite_values(field):
    row = {"t": 1_700_000_000_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    row[field] = "NaN"
    info = MagicMock()
    info.candles_snapshot = MagicMock(return_value=[row])
    client = _client(info)

    with pytest.raises(HyperliquidError, match="Non-finite kline"):
        await client.klines("BTC", "15m")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, None, ["not-a-candle"]])
async def test_klines_reject_unrecognized_or_non_object_shapes(payload):
    info = MagicMock()
    info.candles_snapshot = MagicMock(return_value=payload)
    client = _client(info)

    with pytest.raises(HyperliquidError, match="kline response"):
        await client.klines("BTC", "15m")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("t", None),
        ("t", 0),
        ("o", None),
        ("o", 0),
        ("h", -1),
        ("l", 0),
        ("c", -1),
        ("v", -1),
    ],
)
async def test_klines_reject_missing_or_invalid_required_values(field, value):
    row = {"t": 1_700_000_000_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}
    row[field] = value
    info = MagicMock()
    info.candles_snapshot = MagicMock(return_value=[row])
    client = _client(info)

    with pytest.raises(HyperliquidError, match="kline"):
        await client.klines("BTC", "15m")


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("h", 9), ("l", 11)])
async def test_klines_reject_impossible_ohlc_geometry(field, value):
    row = {"t": 1_700_000_000_000, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1}
    row[field] = value
    info = MagicMock()
    info.candles_snapshot = MagicMock(return_value=[row])

    with pytest.raises(HyperliquidError, match="kline.*geometry"):
        await _client(info).klines("BTC", "15m")


@pytest.mark.asyncio
async def test_klines_normalize_provider_rows_to_chronological_order():
    def row(timestamp, close):
        return {"t": timestamp, "o": close, "h": close, "l": close, "c": close, "v": 1}

    info = MagicMock()
    info.candles_snapshot = MagicMock(
        return_value=[row(1_700_000_002_000, 12), row(1_700_000_001_000, 11)]
    )

    candles = await _client(info).klines("BTC", "15m")

    assert [c.time for c in candles] == [1_700_000_001_000, 1_700_000_002_000]
    assert candles[-1].close == 12


@pytest.mark.asyncio
async def test_klines_reject_duplicate_timestamps():
    def row(close):
        return {
            "t": 1_700_000_001_000,
            "o": close,
            "h": close,
            "l": close,
            "c": close,
            "v": 1,
        }

    info = MagicMock()
    info.candles_snapshot = MagicMock(return_value=[row(11), row(12)])

    with pytest.raises(HyperliquidError, match="duplicate kline timestamp"):
        await _client(info).klines("BTC", "15m")


# ── C-1: open_stop_orders must not silently fall back to a non-trigger endpoint ─


@pytest.mark.asyncio
async def test_open_stop_orders_raises_on_frontend_failure_no_fallback():
    """A frontend_open_orders failure must RAISE (→ SL status UNKNOWN), never be
    swapped for the non-trigger-aware open_orders() which would return a
    confident empty list and read as 'no stop' → false auto-flatten."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(side_effect=RuntimeError("HL 503"))
    info.open_orders = MagicMock(return_value=[])  # must never be consulted
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.open_stop_orders("BTC")
    info.open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_open_stop_orders_raises_on_degraded_shape():
    """A 200-OK-but-degraded body (dict/None instead of a list) is not proof of
    'no triggers' — refuse it rather than trust an empty confident list."""
    for degraded in ({}, None, {"foo": "bar"}):
        info = MagicMock()
        info.frontend_open_orders = MagicMock(return_value=degraded)
        info.open_orders = MagicMock(return_value=[])
        c = _client(info)
        with pytest.raises(HyperliquidError):
            await c.open_stop_orders("BTC")
        info.open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_open_stop_orders_returns_triggers_on_recognized_list():
    """Positive path: a recognized list with a trigger row is returned."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {"coin": "BTC", "oid": 5, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "99000", "reduceOnly": True},
            {"coin": "BTC", "oid": 6, "isTrigger": False, "orderType": "Limit"},
        ]
    )
    c = _client(info)
    out = await c.open_stop_orders("BTC")
    assert len(out) == 1
    assert out[0]["orderId"] == 5
    assert out[0]["triggerPrice"] == 99000.0


@pytest.mark.asyncio
async def test_open_stop_orders_ignores_explicit_foreign_rows_before_validation():
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {
                "coin": "BTC",
                "oid": 5,
                "isTrigger": True,
                "orderType": "Stop Market",
                "triggerPx": "99000",
                "reduceOnly": True,
            },
            {
                "coin": "ETH",
                "oid": 6,
                "isTrigger": "invalid",
                "orderType": "Stop Market",
                "triggerPx": "NaN",
                "reduceOnly": "invalid",
            },
        ]
    )

    out = await _client(info).open_stop_orders("BTC")

    assert [row["orderId"] for row in out] == [5]


@pytest.mark.asyncio
async def test_open_stop_orders_ignores_non_reduce_only_trigger():
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {
                "coin": "BTC",
                "oid": 5,
                "isTrigger": True,
                "orderType": "Stop Market",
                "triggerPx": "99000",
                "reduceOnly": False,
            }
        ]
    )
    c = _client(info)
    assert await c.open_stop_orders("BTC") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"isTrigger": "false"},
        {"reduceOnly": "true"},
        {"triggerPx": "NaN"},
        {"triggerPx": "Infinity"},
        {"coin": ""},
        {"coin": " "},
        {"coin": True},
        {"coin": 123},
        {"oid": True},
        {"oid": 0},
    ],
)
async def test_open_stop_orders_rejects_malformed_protective_trigger(overrides):
    row = {
        "coin": "BTC",
        "oid": 5,
        "isTrigger": True,
        "orderType": "Stop Market",
        "triggerPx": "99000",
        "reduceOnly": True,
    }
    row.update(overrides)
    info = MagicMock()
    info.frontend_open_orders = MagicMock(return_value=[row])
    c = _client(info)

    with pytest.raises(HyperliquidError, match="open_stop_orders"):
        await c.open_stop_orders("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"coin": ""},
        {"coin": " "},
        {"coin": True},
        {"coin": 123},
        {"oid": None},
        {"oid": True},
        {"oid": 0},
        {"side": "unknown"},
        {"sz": "NaN"},
        {"sz": "0"},
        {"limitPx": "Infinity"},
        {"limitPx": "0"},
        {"reduceOnly": "false"},
    ],
)
async def test_open_orders_rejects_malformed_rows(overrides):
    row = {
        "coin": "BTC",
        "oid": 7,
        "side": "B",
        "sz": "0.01",
        "limitPx": "60000",
        "reduceOnly": False,
    }
    row.update(overrides)
    info = MagicMock()
    info.open_orders = MagicMock(return_value=[row])
    c = _client(info)

    with pytest.raises(HyperliquidError, match="open_orders"):
        await c.open_orders("BTC")


@pytest.mark.asyncio
async def test_open_orders_normalizes_valid_row():
    info = MagicMock()
    info.open_orders = MagicMock(
        return_value=[
            {
                "coin": "BTC",
                "oid": 7,
                "side": "B",
                "sz": "0.01",
                "limitPx": "60000",
                "reduceOnly": False,
            }
        ]
    )
    c = _client(info)

    row = (await c.open_orders("BTC"))[0]
    assert row["orderId"] == 7
    assert row["vol"] == 0.01
    assert row["price"] == 60000.0
    assert row["reduceOnly"] is False


@pytest.mark.asyncio
async def test_open_orders_rejects_unrecognized_response_shape():
    info = MagicMock()
    info.open_orders = MagicMock(return_value=None)
    c = _client(info)

    with pytest.raises(HyperliquidError, match="unrecognized shape"):
        await c.open_orders("BTC")


# ── SL-coverage: HL trigger rows must expose the closing size as `vol` (coins) ──
# The frontend Deckungsgrad check sums s.vol over SL orders vs hold_vol. HL rows
# previously carried no top-level size → the check always fell to the conservative
# "protected" fallback and NEVER warned on HL. Expose the resting reduce-only
# trigger's `sz` (remaining coins to close, same unit as abs(szi)/hold_vol).


@pytest.mark.asyncio
async def test_open_stop_orders_exposes_trigger_size_as_vol():
    """A trigger whose raw row carries sz=0.02 must emit vol == 0.02 (coins) so
    the frontend coverage check (s.vol) can compare it against hold_vol."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {"coin": "BTC", "oid": 7, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "99000", "reduceOnly": True, "sz": "0.02", "origSz": "0.05"},
        ]
    )
    c = _client(info)
    out = await c.open_stop_orders("BTC")
    assert len(out) == 1
    assert out[0]["vol"] == 0.02


@pytest.mark.asyncio
async def test_open_stop_orders_omits_vol_when_size_unreadable():
    """Missing / empty / garbage sz must yield vol=None (never 0) so the frontend
    falls through to the conservative 'protected' fallback instead of reading a
    false '0 covered' → false partial-coverage alarm."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {"coin": "BTC", "oid": 8, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "99000", "reduceOnly": True},  # no sz
            {"coin": "BTC", "oid": 9, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "98000", "reduceOnly": True, "sz": "abc"},  # garbage sz
            {"coin": "BTC", "oid": 10, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "97000", "reduceOnly": True, "sz": ""},  # empty sz
        ]
    )
    c = _client(info)
    out = await c.open_stop_orders("BTC")
    assert len(out) == 3
    for row in out:
        assert row["vol"] is None


@pytest.mark.asyncio
async def test_open_stop_orders_zero_size_yields_none_not_zero():
    """A resting trigger with sz<=0 (e.g. a whole-position TP/SL reported with
    coin size 0) must emit vol=None, NOT 0.0 — a false 0 would read as '0 covered'
    and raise a false partial-coverage alarm on a fully-protected position."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {"coin": "BTC", "oid": 11, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "99000", "reduceOnly": True, "sz": "0"},
            {"coin": "BTC", "oid": 12, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "98000", "reduceOnly": True, "sz": "0.0"},
        ]
    )
    c = _client(info)
    out = await c.open_stop_orders("BTC")
    assert len(out) == 2
    for row in out:
        assert row["vol"] is None


# ── M-2: a degraded user_state body must NOT be trusted as a flat account ──────

_COMPLETE_STATE = {
    "marginSummary": {
        "accountValue": "1000",
        "totalMarginUsed": "50",
        "totalNtlPos": "500",
    },
    "withdrawable": "950",
    "assetPositions": [],
}

# 200-OK-but-degraded/partial shapes: not a proper user_state payload. A
# complete body always carries BOTH a margin summary AND an assetPositions list;
# anything missing either must fail-closed (raise), never read as "flat/no
# positions" which would hide a real position from risk/flatten logic.
_DEGRADED_STATES = [
    None,
    {},
    [],
    {"foo": "bar"},
    {"marginSummary": {"accountValue": "1000"}},  # positions key missing
    {"marginSummary": {"accountValue": "1000"}, "assetPositions": None},
    {"assetPositions": []},  # margin summary missing
    {"marginSummary": {}, "assetPositions": []},
    {"marginSummary": "invalid", "assetPositions": []},
    {"marginSummary": {"accountValue": None}, "assetPositions": []},
    {"marginSummary": {"accountValue": ""}, "assetPositions": []},
    {"marginSummary": {"accountValue": True}, "assetPositions": []},
    {"marginSummary": {"accountValue": "NaN"}, "assetPositions": []},
    {"marginSummary": {"accountValue": "Infinity"}, "assetPositions": []},
]


@pytest.mark.parametrize("degraded", _DEGRADED_STATES)
@pytest.mark.asyncio
async def test_assets_raises_on_degraded_user_state(degraded):
    info = MagicMock()
    info.user_state = MagicMock(return_value=degraded)
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.assets()


@pytest.mark.parametrize("degraded", _DEGRADED_STATES)
@pytest.mark.asyncio
async def test_positions_raises_on_degraded_user_state(degraded):
    info = MagicMock()
    info.user_state = MagicMock(return_value=degraded)
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.positions()


@pytest.mark.asyncio
async def test_assets_and_positions_ok_on_complete_state():
    """Positive path: a complete user_state is honoured (equity/flat position)."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    rows = await c.assets()
    assert rows[0]["equity"] == 1000.0
    assert await c.positions() == []


def _state_with_open_position(**overrides):
    position = {
        "coin": "BTC",
        "szi": "0.01",
        "entryPx": "60000",
        "unrealizedPnl": "5",
        "liquidationPx": "45000",
        "marginUsed": "60",
        "leverage": {"value": 10, "type": "cross"},
    }
    position.update(overrides)
    return {
        "marginSummary": {
            "accountValue": "1000",
            "totalMarginUsed": "60",
            "totalNtlPos": "600",
        },
        "withdrawable": "940",
        "assetPositions": [{"position": position}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("szi", None),
        ("szi", "NaN"),
        ("szi", "Infinity"),
        ("entryPx", None),
        ("entryPx", "NaN"),
        ("entryPx", "0"),
    ],
)
async def test_positions_reject_invalid_open_position_geometry(field, value):
    state = _state_with_open_position(**{field: value})
    info = MagicMock()
    info.user_state = MagicMock(return_value=state)
    c = _client(info)

    with pytest.raises(HyperliquidError, match="position"):
        await c.positions(fresh=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", [None, "BTC"])
@pytest.mark.parametrize("coin", ["", None, " ", True, 123])
async def test_positions_reject_open_position_without_symbol(symbol, coin):
    info = MagicMock()
    info.user_state = MagicMock(return_value=_state_with_open_position(coin=coin))
    c = _client(info)

    with pytest.raises(HyperliquidError, match="position symbol"):
        await c.positions(symbol, fresh=True)


@pytest.mark.asyncio
async def test_symbol_scoped_positions_ignore_explicit_foreign_invalid_row():
    state = _state_with_open_position()
    foreign = _state_with_open_position(coin="ETH", szi=None)["assetPositions"][0]
    state["assetPositions"].append(foreign)
    info = MagicMock()
    info.user_state = MagicMock(return_value=state)

    rows = await _client(info).positions("BTC", fresh=True)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leverage",
    [None, {}, {"type": "unknown", "value": 10}, {"type": True, "value": 10}, "isolated"],
)
async def test_positions_reject_missing_or_unknown_margin_mode(leverage):
    info = MagicMock()
    info.user_state = MagicMock(
        return_value=_state_with_open_position(leverage=leverage)
    )

    with pytest.raises(HyperliquidError, match="leverage type"):
        await _client(info).positions(fresh=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "NaN", "Infinity", 0, -1])
async def test_positions_degrade_invalid_leverage_value_to_unknown(value):
    info = MagicMock()
    info.user_state = MagicMock(
        return_value=_state_with_open_position(
            leverage={"type": "isolated", "value": value}
        )
    )

    row = (await _client(info).positions(fresh=True))[0]

    assert row["leverage"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("liquidation_price", [0, -1])
async def test_positions_degrade_nonpositive_liquidation_price_to_unknown(
    liquidation_price,
):
    info = MagicMock()
    info.user_state = MagicMock(
        return_value=_state_with_open_position(liquidationPx=liquidation_price)
    )

    row = (await _client(info).positions(fresh=True))[0]

    assert row["liquidatePrice"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("margin_used", [0, -1])
async def test_positions_degrade_nonpositive_margin_used_to_unknown(margin_used):
    info = MagicMock()
    info.user_state = MagicMock(
        return_value=_state_with_open_position(marginUsed=margin_used)
    )

    row = (await _client(info).positions(fresh=True))[0]

    assert row["im"] is None


@pytest.mark.asyncio
async def test_positions_drop_nonfinite_optional_finance_values():
    info = MagicMock()
    info.user_state = MagicMock(
        return_value=_state_with_open_position(
            unrealizedPnl="NaN", liquidationPx="Infinity", marginUsed="-Infinity"
        )
    )
    c = _client(info)

    row = (await c.positions(fresh=True))[0]
    assert row["unRealizedPnl"] is None
    assert row["liquidatePrice"] is None
    assert row["im"] is None


@pytest.mark.asyncio
async def test_positions_drop_boolean_optional_finance_values():
    info = MagicMock()
    info.user_state = MagicMock(
        return_value=_state_with_open_position(
            unrealizedPnl=True, liquidationPx=True, marginUsed=True
        )
    )
    c = _client(info)

    row = (await c.positions(fresh=True))[0]
    assert row["unRealizedPnl"] is None
    assert row["liquidatePrice"] is None
    assert row["im"] is None


def _valid_fill(**overrides):
    row = {
        "coin": "BTC",
        "px": "60000",
        "sz": "0.01",
        "side": "B",
        "time": 1_700_000_000_000,
        "dir": "Open Long",
        "startPosition": "0",
        "closedPnl": "0",
        "fee": "0.1",
        "oid": 42,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, "not-a-list"])
async def test_user_fills_rejects_unrecognized_top_level_shape(payload):
    info = MagicMock()
    info.user_fills = MagicMock(return_value=payload)
    c = _client(info)

    with pytest.raises(HyperliquidError, match="user_fills.*unrecognized shape"):
        await c.user_fills("BTC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("coin", ""),
        ("px", "NaN"),
        ("px", "0"),
        ("sz", "Infinity"),
        ("sz", "0"),
        ("side", "unknown"),
        ("time", "NaN"),
        ("time", 0),
        ("time", True),
    ],
)
async def test_user_fills_skips_invalid_required_fields(field, value):
    info = MagicMock()
    info.user_fills = MagicMock(return_value=[_valid_fill(**{field: value})])
    c = _client(info)

    assert await c.user_fills("BTC") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("coin", [" ", True, 123])
async def test_user_fills_unscoped_skips_invalid_coin_identity(coin):
    info = MagicMock()
    info.user_fills = MagicMock(return_value=[_valid_fill(coin=coin)])

    assert await _client(info).user_fills() == []


@pytest.mark.asyncio
async def test_user_fills_drop_nonfinite_optional_values():
    info = MagicMock()
    info.user_fills = MagicMock(
        return_value=[
            _valid_fill(
                startPosition="NaN",
                closedPnl="Infinity",
                fee="-Infinity",
            )
        ]
    )
    c = _client(info)

    row = (await c.user_fills("BTC"))[0]
    assert row["start_position"] is None
    assert row["closed_pnl"] is None
    assert row["fee"] is None


@pytest.mark.asyncio
async def test_user_fills_drop_boolean_optional_values_and_oid():
    info = MagicMock()
    info.user_fills = MagicMock(
        return_value=[
            _valid_fill(
                startPosition=True,
                closedPnl=True,
                fee=True,
                oid=True,
            )
        ]
    )
    c = _client(info)

    row = (await c.user_fills("BTC"))[0]
    assert row["start_position"] is None
    assert row["closed_pnl"] is None
    assert row["fee"] is None
    assert row["oid"] is None


# ── user_state short-TTL cache + 429 resilience (over-poll → 429 → 502 fix) ───


@pytest.mark.asyncio
async def test_assets_and_positions_share_one_user_state_within_ttl():
    """assets()+positions() within the short TTL must collapse to a SINGLE
    upstream user_state fetch. This dedups account_snapshot's 2 reads and rapid
    concurrent endpoint polls that were driving Hyperliquid 429 → /api/market 502."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()
    await c.positions()
    assert info.user_state.call_count == 1


@pytest.mark.asyncio
async def test_user_state_cache_ttl_uses_monotonic_time(monkeypatch):
    newer_state = {
        **_COMPLETE_STATE,
        "marginSummary": {
            **_COMPLETE_STATE["marginSummary"],
            "accountValue": "2000",
        },
    }
    info = MagicMock()
    info.user_state = MagicMock(side_effect=[_COMPLETE_STATE, newer_state])
    client = _client(info)
    fake_time = MagicMock()
    fake_time.time = MagicMock(side_effect=[1_000.0, 100.0])
    fake_time.monotonic = MagicMock(side_effect=[1_000.0, 5_000.0, 5_000.0])
    monkeypatch.setattr("app.hyperliquid.client.time", fake_time)

    assert (await client.assets())[0]["equity"] == 1000.0
    assert (await client.assets())[0]["equity"] == 2000.0
    assert info.user_state.call_count == 2


@pytest.mark.asyncio
async def test_assets_positions_serve_stale_cache_on_429():
    """A warm cache older than the TTL but within the stale bound must be served
    when the refetch 429s — a transient rate-limit burst degrades to a slightly
    stale read instead of 502-ing the market view / starving the trade monitor."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()  # warm the cache (call_count == 1)
    # Age the cache past the TTL but within the stale bound, then 429 the refetch.
    ts, state, addr = c._user_state_cache
    c._user_state_cache = (time.monotonic() - 5.0, state, addr)
    info.user_state = MagicMock(side_effect=RuntimeError("429 Too Many Requests"))
    rows = await c.assets()
    assert rows[0]["equity"] == 1000.0  # stale equity served, no raise
    assert await c.positions() == []  # stale positions served, no raise


@pytest.mark.asyncio
async def test_assets_positions_raise_on_error_without_warm_cache():
    """No usable cache + upstream error → still fail honestly (never fabricate a
    flat account). Only a genuinely warm cache may absorb a 429."""
    info = MagicMock()
    info.user_state = MagicMock(side_effect=RuntimeError("429 Too Many Requests"))
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.assets()
    with pytest.raises(HyperliquidError):
        await c.positions()


@pytest.mark.asyncio
async def test_assets_raises_when_cache_too_stale_on_error():
    """Beyond the bounded stale window the cache is no longer trustworthy: a 429
    with a too-old cache must raise, not serve arbitrarily old equity/positions."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()
    ts, state, addr = c._user_state_cache
    c._user_state_cache = (time.monotonic() - 30.0, state, addr)  # past MAX_STALE
    info.user_state = MagicMock(side_effect=RuntimeError("429"))
    with pytest.raises(HyperliquidError):
        await c.assets()


# ── _to_thread transient-429 retry (read paths retry, money path NEVER) ───────


class _Boom(Exception):
    """Mimics the SDK ClientError enough for _is_rate_limited (has .status_code)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"({status_code}, None, 'null')")
        self.status_code = status_code


@pytest.mark.asyncio
async def test_to_thread_retries_read_path_on_429(monkeypatch):
    """A read/data call that 429s must be retried with backoff and succeed once
    the transient burst clears — this is what rides out a market-scan 429."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)  # no real sleep in test
    c = _client(MagicMock())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Boom(429)
        return "ok"

    assert await c._to_thread(fn) == "ok"
    assert calls["n"] == 3  # two 429s absorbed, third attempt succeeds


@pytest.mark.asyncio
async def test_to_thread_never_retries_money_path_on_429(monkeypatch):
    """A money-path call (place/modify/cancel) must NEVER be auto-resent on 429 —
    the first send may already have landed; a retry could double the order."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)
    c = _client(MagicMock())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _Boom(429)

    with pytest.raises(HyperliquidError):
        await c._to_thread(fn, money_path=True)
    assert calls["n"] == 1  # exactly one attempt, no resend


@pytest.mark.asyncio
async def test_to_thread_does_not_retry_non_429(monkeypatch):
    """Only 429 is transient. A 500/other error fails fast — no retry storm."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)
    c = _client(MagicMock())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _Boom(500)

    with pytest.raises(HyperliquidError):
        await c._to_thread(fn)
    assert calls["n"] == 1


# ── read-rate budget (token bucket paces scanner flood; money path bypasses) ──


@pytest.mark.asyncio
async def test_token_bucket_bursts_then_paces(monkeypatch):
    """The first `capacity` acquires pass free (burst); the next one must sleep
    ~1/rate — this is what turns the 75-coin klines flood into a paced stream."""
    import app.hyperliquid.client as mod

    slept: list[float] = []

    async def _fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(mod.asyncio, "sleep", _fake_sleep)
    b = mod._AsyncTokenBucket(rate=5.0, capacity=3.0)
    for _ in range(3):
        await b.acquire()
    assert slept == []  # burst of 3 passes without pacing
    await b.acquire()
    assert len(slept) == 1 and slept[0] > 0  # 4th call paced


@pytest.mark.asyncio
async def test_only_paced_reads_consume_a_token():
    """Pacing is OPT-IN (only the scanner klines fan-out passes paced=True). An
    unpaced interactive read must NOT queue on the bucket (no priority inversion),
    and the money path is never paced even if paced=True is passed."""
    c = _client(MagicMock())
    calls = {"n": 0}

    class _SpyBucket:
        async def acquire(self):
            calls["n"] += 1

    c._read_limiter = _SpyBucket()
    assert await c._to_thread(lambda: "ok", paced=True) == "ok"  # paced read → token
    assert await c._to_thread(lambda: "ok") == "ok"  # unpaced read → no token
    assert (
        await c._to_thread(lambda: "ok", money_path=True, paced=True) == "ok"
    )  # money path → never paced
    assert calls["n"] == 1  # only the paced read consumed a token


@pytest.mark.asyncio
async def test_paced_read_repaces_each_retry(monkeypatch):
    """Finding 1: a paced read that 429s must RE-acquire a token for every retry,
    so a 429 storm's retries stay under the rate limit instead of bypassing it."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)
    c = _client(MagicMock())
    tokens = {"n": 0}

    class _SpyBucket:
        async def acquire(self):
            tokens["n"] += 1

    c._read_limiter = _SpyBucket()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Boom(429)
        return "ok"

    assert await c._to_thread(fn, paced=True) == "ok"
    assert calls["n"] == 3  # 2 retries then success
    assert tokens["n"] == 3  # one token acquired PER attempt (re-paced)


@pytest.mark.asyncio
async def test_token_bucket_clamps_pathological_sleep(monkeypatch):
    """A fat-fingered tiny rps must never produce a multi-minute lock-held sleep:
    each acquire wait is clamped to _MAX_ACQUIRE_WAIT_S."""
    import app.hyperliquid.client as mod

    slept: list[float] = []

    async def _fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(mod.asyncio, "sleep", _fake_sleep)
    b = mod._AsyncTokenBucket(rate=1e-6, capacity=1.0)  # absurdly slow
    await b.acquire()  # first token free (capacity 1)
    await b.acquire()  # must pace — but clamped, not ~11 days
    assert slept and slept[-1] <= mod._MAX_ACQUIRE_WAIT_S


@pytest.mark.asyncio
async def test_assets_fresh_fails_closed_on_429_despite_warm_cache(monkeypatch):
    """Money-decision reads (assets(fresh=True), feeding order sizing/gate) must
    NOT accept the 8s stale-serve: a 429 with a warm cache still raises, so sizing
    fails closed rather than sizing against stale, optimistic-high equity."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()  # warm the cache
    ts, state, addr = c._user_state_cache
    c._user_state_cache = (
        time.monotonic() - 3.0,
        state,
        addr,
    )  # aged past TTL, within 8s
    info.user_state = MagicMock(side_effect=RuntimeError("429 Too Many Requests"))
    # Contrast: a non-fresh read would serve the stale cache; fresh must fail closed.
    with pytest.raises(HyperliquidError):
        await c.assets(fresh=True)


# ── Finding 1: fresh=True must BYPASS the 2s TTL short-circuit ────────────────
# (the double-risk hole: confirm A under _trade_lock warms the cache with the
# PRE-A account; confirm B <2s later, also fresh, must NOT be served that cache.)


def _state_with_equity(value: str) -> dict:
    """A complete user_state carrying a specific accountValue (money read)."""
    return {
        "marginSummary": {
            "accountValue": value,
            "totalMarginUsed": "50",
            "totalNtlPos": "500",
        },
        "withdrawable": value,
        "assetPositions": [],
    }


@pytest.mark.asyncio
async def test_assets_fresh_bypasses_ttl_shortcircuit_and_fetches_live():
    """A <=2s-old cache is 'fresh enough' for display, but a fresh=True money read
    must ignore it and hit the exchange: a same-_trade_lock earlier confirm can
    have warmed that cache with PRE-trade equity/positions. Non-fresh keeps using
    the cache (dedup preserved)."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_state_with_equity("1000"))
    c = _client(info)
    await c.assets(fresh=True)  # warm cache; call_count == 1
    assert info.user_state.call_count == 1
    # Account changed upstream WITHIN the 2s TTL (e.g. an order just filled).
    info.user_state = MagicMock(return_value=_state_with_equity("2000"))
    # Non-fresh read within TTL: still served from cache (1000) — dedup intact.
    assert (await c.assets())[0]["equity"] == 1000.0
    assert info.user_state.call_count == 0
    # fresh read within TTL: MUST refetch and see the live 2000, not stale 1000.
    fresh_rows = await c.assets(fresh=True)
    assert fresh_rows[0]["equity"] == 2000.0
    assert info.user_state.call_count == 1


@pytest.mark.asyncio
async def test_positions_fresh_bypasses_ttl_shortcircuit_and_fetches_live():
    """Same TTL-bypass invariant for the aggregate-exposure read: a fresh
    positions() must reflect a position opened <2s ago, not the empty pre-trade
    cache that would zero out the same-side risk gate."""
    empty = _state_with_equity("1000")
    with_pos = {
        "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50",
                          "totalNtlPos": "500"},
        "withdrawable": "1000",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.5",
                    "entryPx": "100",
                    "leverage": {"value": 5, "type": "isolated"},
                }
            }
        ],
    }
    info = MagicMock()
    info.user_state = MagicMock(return_value=empty)
    c = _client(info)
    await c.positions("BTC", fresh=True)  # warm cache with the flat pre-trade state
    info.user_state = MagicMock(return_value=with_pos)  # position opened <2s ago
    live = await c.positions("BTC", fresh=True)
    assert len(live) == 1  # sees the new position, not the stale empty cache


# ── H-2: totalNtlPos is notional exposure, not unrealized PnL ─────────────────


@pytest.mark.asyncio
async def test_assets_does_not_mislabel_notional_as_unrealized():
    """`marginSummary.totalNtlPos` is total NOTIONAL position value, not
    unrealized PnL. It must not be surfaced under a misleading 'unrealized' key
    that a future PnL/UI caller could trust."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    row = (await c.assets())[0]
    assert "unrealized" not in row
    assert row["notional_position"] == 500.0


@pytest.mark.parametrize("withdrawable", [0, "0", None, "", -1])
def test_assets_never_invents_available_balance_when_withdrawable_is_zero_or_missing(
    withdrawable,
):
    row = HyperliquidClient._assets_from_state(
        {
            "marginSummary": {
                "accountValue": "1000",
                "totalMarginUsed": "50",
                "totalNtlPos": "500",
            },
            "withdrawable": withdrawable,
        }
    )[0]

    assert row["availableBalance"] == 0.0


# ── O5: funding_rate reads from the ctx cache, no wasteful all_mids() ──────────


def test_market_context_cache_ttl_uses_monotonic_time(monkeypatch):
    first = ({"universe": [{"name": "BTC"}]}, [{"funding": "0.001"}])
    second = ({"universe": [{"name": "BTC"}]}, [{"funding": "0.002"}])
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(side_effect=[first, second])
    client = _client(info)
    fake_time = MagicMock()
    fake_time.time = MagicMock(side_effect=[1_000.0, 100.0])
    fake_time.monotonic = MagicMock(side_effect=[1_000.0, 1_003.0])
    monkeypatch.setattr("app.hyperliquid.client.time", fake_time)

    assert client._meta_ctxs_sync() == first
    assert client._meta_ctxs_sync() == second
    assert info.meta_and_asset_ctxs.call_count == 2


@pytest.mark.asyncio
async def test_funding_rate_reads_from_ctx_without_all_mids():
    """O5: funding is read straight from meta_and_asset_ctxs (the shared ctx
    cache), so funding_rate() must NOT trigger the all_mids() round-trip that
    ticker() used to do just for an unneeded mid price."""
    info = MagicMock()
    meta_ctx = (
        {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [{"funding": 0.00012}, {"funding": -0.0003}],
    )
    info.meta_and_asset_ctxs = MagicMock(return_value=meta_ctx)
    info.all_mids = MagicMock(side_effect=AssertionError("all_mids must not be called"))
    c = _client(info)
    fr = await c.funding_rate("BTC_USDT")
    assert fr.symbol == "BTC"
    assert fr.funding_rate == 0.00012
    info.all_mids.assert_not_called()


@pytest.mark.asyncio
async def test_funding_rate_falls_back_to_zero_when_ctx_missing_coin():
    """If the ctx doesn't carry the coin (or funding), fall back safely to 0.0
    with the same return shape rather than raising."""
    info = MagicMock()
    meta_ctx = ({"universe": [{"name": "SOL"}]}, [{"funding": 0.001}])
    info.meta_and_asset_ctxs = MagicMock(return_value=meta_ctx)
    c = _client(info)
    fr = await c.funding_rate("BTC_USDT")
    assert fr.symbol == "BTC"
    assert fr.funding_rate == 0.0


@pytest.mark.asyncio
async def test_funding_rate_falls_back_to_zero_when_value_is_nonfinite():
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[{"universe": [{"name": "BTC"}]}, [{"funding": "NaN"}]]
    )
    client = _client(info)

    result = await client.funding_rate("BTC_USDT")

    assert result.funding_rate == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_name", [True, 7, ["BTC"], {"coin": "BTC"}])
async def test_market_context_does_not_match_nonstring_market_identity(invalid_name):
    requested = str(invalid_name).upper()
    info = MagicMock()
    info.all_mids = MagicMock(return_value={requested: "10"})
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[
            {"universe": [{"name": invalid_name}]},
            [
                {
                    "funding": "0.001",
                    "openInterest": "12",
                    "premium": "0.01",
                    "prevDayPx": "8",
                }
            ],
        ]
    )
    client = _client(info)

    ticker = await client.ticker(requested)
    funding = await client.funding_rate(requested)
    extras = await client.market_extras(requested)

    assert ticker.funding_rate is None
    assert funding.funding_rate == 0.0
    assert extras["open_interest"] is None
    assert extras["premium"] is None
    assert extras["prev_day_px"] is None


@pytest.mark.asyncio
async def test_market_context_reads_skip_malformed_universe_rows():
    info = MagicMock()
    info.all_mids = MagicMock(return_value={"BTC": "10"})
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[
            {"universe": [7, {"name": "BTC"}]},
            [
                {"funding": "0.9"},
                {
                    "funding": "0.001",
                    "openInterest": "12",
                    "premium": "0.01",
                    "prevDayPx": "8",
                },
            ],
        ]
    )
    client = _client(info)

    ticker = await client.ticker("BTC")
    funding = await client.funding_rate("BTC")
    extras = await client.market_extras("BTC")

    assert ticker.funding_rate == 0.001
    assert funding.funding_rate == 0.001
    assert extras["open_interest"] == 12.0
    assert extras["premium"] == 0.01
    assert extras["prev_day_px"] == 8.0


@pytest.mark.asyncio
async def test_market_overview_sanitizes_invalid_ranking_fields():
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[
            {"universe": [{"name": "GOOD"}, {"name": "BAD"}]},
            [
                {
                    "markPx": "10",
                    "prevDayPx": "8",
                    "dayNtlVlm": "1000",
                    "funding": "0.001",
                    "openInterest": "50",
                },
                {
                    "markPx": "NaN",
                    "prevDayPx": "Infinity",
                    "dayNtlVlm": "-1",
                    "funding": "NaN",
                    "openInterest": "-2",
                },
            ],
        ]
    )

    rows = await _client(info).market_overview()

    assert rows[0]["symbol"] == "GOOD"
    bad = rows[1]
    assert bad["volume24"] == 0.0
    assert bad["funding"] == 0.0
    assert bad["last"] is None
    assert bad["price_change_pct"] is None
    assert bad["open_interest"] is None


@pytest.mark.asyncio
async def test_market_overview_degrades_overflowed_price_change_to_none():
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[
            {"universe": [{"name": "BTC"}]},
            [
                {
                    "markPx": "1e308",
                    "prevDayPx": "5e-324",
                    "dayNtlVlm": "1000",
                }
            ],
        ]
    )

    rows = await _client(info).market_overview()

    assert rows[0]["price_change_pct"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_name", [True, 7, ["BTC"], {"coin": "BTC"}, "   "]
)
async def test_market_overview_does_not_stringify_invalid_names(invalid_name):
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[
            {"universe": [{"name": "BTC"}, {"name": invalid_name}]},
            [{"markPx": "10"}, {"markPx": "20"}],
        ]
    )

    rows = await _client(info).market_overview()

    assert [row["symbol"] for row in rows] == ["BTC"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_row", [{}, 7, None, "BTC"])
async def test_market_overview_skips_malformed_universe_rows(invalid_row):
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[
            {"universe": [{"name": "BTC"}, invalid_row]},
            [{"markPx": "10"}, {"markPx": "20"}],
        ]
    )

    rows = await _client(info).market_overview()

    assert [row["symbol"] for row in rows] == ["BTC"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_ctx", [None, 7, [], "bad"])
async def test_market_overview_treats_malformed_context_as_missing(invalid_ctx):
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(
        return_value=[{"universe": [{"name": "BTC"}]}, [invalid_ctx]]
    )

    row = (await _client(info).market_overview())[0]

    assert row == {
        "symbol": "BTC",
        "volume24": 0.0,
        "funding": 0.0,
        "last": None,
        "price_change_pct": None,
        "open_interest": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [{"universe": [{"name": "BTC"}]}, "bad"],
        [{"universe": "bad"}, []],
        [{"universe": {}}, []],
        [{"universe": []}, {}],
    ],
)
async def test_market_overview_rejects_malformed_collection_shapes(payload):
    info = MagicMock()
    info.meta_and_asset_ctxs = MagicMock(return_value=payload)

    with pytest.raises(HyperliquidError, match="market-overview response"):
        await _client(info).market_overview()
