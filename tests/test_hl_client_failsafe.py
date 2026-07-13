"""HyperliquidClient exchange-integrity fail-safes (final audit C-1, M-2, H-2).

All tests drive the REAL client with a mocked SDK info object — no keys, no
network. They pin the money-safety invariant that a degraded/errored exchange
response must surface as an error (→ UNKNOWN downstream), never as a confident
"flat account" / "no stop orders".
"""

from __future__ import annotations

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
    assert out[0]["triggerPrice"] == "99000"


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
    {"assetPositions": []},  # margin summary missing
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


# ── O5: funding_rate reads from the ctx cache, no wasteful all_mids() ──────────


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
