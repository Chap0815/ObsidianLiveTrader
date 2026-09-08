"""HyperliquidClient.account_snapshot() -> mapped positions (F-10).

Hyperliquid is coin-denominated, so each mapped position must carry a
contract_size of 1.0 (unlike MEXC, where contract_size varies per symbol
and must come from that symbol's own contract metadata).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.hyperliquid.client import HyperliquidClient

_USER_STATE = {
    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50", "totalNtlPos": "500"},
    "withdrawable": "950",
    "assetPositions": [
        {
            "position": {
                "coin": "BTC",
                "szi": "0.01",
                "entryPx": "60000",
                "unrealizedPnl": "5.0",
                "marginUsed": "50",
                "leverage": {"value": 10, "type": "cross"},
            }
        }
    ],
}


def _client() -> HyperliquidClient:
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    c.account_address = "0x" + "2" * 40
    info = MagicMock()
    info.user_state = MagicMock(return_value=_USER_STATE)
    c._info = info
    c._get_info = MagicMock(return_value=info)
    return c


@pytest.mark.asyncio
async def test_hl_account_snapshot_position_contract_size_is_one():
    c = _client()
    snap = await c.account_snapshot()
    assert len(snap["positions"]) == 1
    p = snap["positions"][0]
    assert p["symbol"] == "BTC"
    assert p["contract_size"] == 1.0


@pytest.mark.asyncio
async def test_hl_fresh_account_snapshot_uses_one_consistent_user_state_read():
    c = _client()

    await c.account_snapshot(fresh=True)

    assert c._info.user_state.call_count == 1
