"""Real HyperliquidClient with a mocked SDK exchange/info (no keys, no network).

Covers the untested place_order mapping + cloid stamping + timeout recovery
(audit Backend-C1 / test T1).
"""

from __future__ import annotations

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


def _client() -> HyperliquidClient:
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    # Preload meta so _asset_row() needs no network.
    c._meta_cache = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}
    c._exchange = MagicMock()
    c._exchange.market_open = MagicMock(return_value=_OK)
    c._exchange.order = MagicMock(return_value=_OK)
    return c


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
