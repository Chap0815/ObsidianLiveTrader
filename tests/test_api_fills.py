"""GET /api/fills — account executions for chart trade markers."""

import httpx
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.hyperliquid.errors import HyperliquidError
from app.main import app
from app.mexc.client import MexcClient


SAMPLE_FILLS = [
    {
        "symbol": "SOL",
        "px": 142.3,
        "sz": 0.5,
        "side": "buy",
        "time": 1710000005000,
        "dir": "Open Long",
        "closed_pnl": None,
        "oid": 77,
        "fee": 0.05,
    }
]


def test_fills_supported_returns_rows():
    ex = MagicMock()
    ex.user_fills = AsyncMock(return_value=SAMPLE_FILLS)
    with TestClient(app) as client:
        client.app.state.mexc = ex
        client.app.state.exchange = ex
        r = client.get("/api/fills", params={"symbol": "SOL_USDT", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["error"] is None
    assert body["fills"][0]["px"] == 142.3
    assert body["fills"][0]["side"] == "buy"
    ex.user_fills.assert_awaited_once()
    assert ex.user_fills.await_args.kwargs["limit"] == 50


def test_fills_unsupported_client_empty():
    class NoFills:
        pass

    with TestClient(app) as client:
        client.app.state.mexc = NoFills()
        client.app.state.exchange = NoFills()
        r = client.get("/api/fills")
    assert r.status_code == 200
    assert r.json() == {"fills": [], "supported": False, "error": None}


def test_fills_supported_true_for_real_mexc_client():
    """Task 32 (C3-01): a REAL MexcClient (not a mock) — now that it has
    user_fills — must make /api/fills report supported=True and return the
    normalized rows, purely via the endpoint's generic hasattr feature-detect
    (no MEXC special-case anywhere in main.py)."""
    deal_rows = [
        {
            "symbol": "BTC_USDT",
            "side": 1,
            "vol": 0.5,
            "price": 65000.5,
            "fee": 0.02,
            "timestamp": 1710000005000,
            "profit": 0,
            "orderId": "555",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": deal_rows})

    mexc = MexcClient("https://contract.mexc.com", "k", "s")
    mexc._client = httpx.AsyncClient(
        base_url=mexc.base_url, transport=httpx.MockTransport(handler)
    )
    with TestClient(app) as client:
        client.app.state.mexc = mexc
        client.app.state.exchange = mexc
        r = client.get("/api/fills", params={"symbol": "BTC_USDT", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["error"] is None
    assert body["fills"][0]["symbol"] == "BTC_USDT"
    assert body["fills"][0]["side"] == "buy"
    assert body["fills"][0]["dir"] == "Open Long"


def test_fills_exchange_error_soft():
    ex = MagicMock()
    ex.user_fills = AsyncMock(side_effect=HyperliquidError("info down"))
    with TestClient(app) as client:
        client.app.state.mexc = ex
        client.app.state.exchange = ex
        r = client.get("/api/fills")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert "info down" in body["error"]
