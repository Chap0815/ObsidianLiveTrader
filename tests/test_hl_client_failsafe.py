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
