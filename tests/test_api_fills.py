"""GET /api/fills — account executions for chart trade markers."""

import asyncio
import httpx
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.hyperliquid.errors import HyperliquidError
from app.main import app
from app.mexc.client import MexcClient


SAMPLE_FILLS = [
    {
        "symbol": "SOL_USDT",
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


def test_fills_uses_declared_client_exchange_for_symbol_semantics(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    row = {**SAMPLE_FILLS[0], "symbol": "SOL"}
    ex = MagicMock()
    ex.exchange_id = "hyperliquid"
    ex.user_fills = AsyncMock(return_value=[row])

    with TestClient(app) as client:
        client.app.state.mexc = ex
        client.app.state.exchange = ex
        response = client.get(
            "/api/fills", params={"symbol": "SOL_USDT", "limit": 50}
        )

    assert response.status_code == 200
    assert response.json() == {"fills": [row], "supported": True, "error": None}
    ex.user_fills.assert_awaited_once_with(symbol="SOL", limit=50)


def test_fills_unsupported_client_empty():
    class NoFills:
        pass

    with TestClient(app) as client:
        client.app.state.mexc = NoFills()
        client.app.state.exchange = NoFills()
        r = client.get("/api/fills")
    assert r.status_code == 200
    assert r.json() == {"fills": [], "supported": False, "error": None}


@pytest.mark.parametrize("invalid_symbol", ["", "   ", "BTC/USDT"])
def test_fills_rejects_invalid_symbol_before_client_capability_check(invalid_symbol):
    class NoFills:
        pass

    with TestClient(app) as client:
        client.app.state.mexc = NoFills()
        client.app.state.exchange = NoFills()
        response = client.get("/api/fills", params={"symbol": invalid_symbol})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid symbol")


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
    marker = "SYNTHETIC_PRIVATE_FILL_ERROR"
    ex = MagicMock()
    ex.user_fills = AsyncMock(side_effect=HyperliquidError(marker))
    with TestClient(app) as client:
        client.app.state.mexc = ex
        client.app.state.exchange = ex
        r = client.get("/api/fills")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["error"] == "Exchange fill history unavailable"
    assert marker not in r.text


@pytest.mark.parametrize(
    "malformed_rows",
    [
        None,
        {},
        [None],
        [SAMPLE_FILLS[0], SAMPLE_FILLS[0]],
        [{key: value for key, value in SAMPLE_FILLS[0].items() if key != "sz"}],
        [{**SAMPLE_FILLS[0], "privateDiagnostic": "SYNTHETIC_PRIVATE_FILL_RAW"}],
        [{**SAMPLE_FILLS[0], "symbol": "ETH_USDT"}],
        [{**SAMPLE_FILLS[0], "px": "142.3"}],
        [{**SAMPLE_FILLS[0], "fee": float("inf")}],
        [{**SAMPLE_FILLS[0], "oid": 0}],
        [{**SAMPLE_FILLS[0], "side": "sell", "dir": "Open Long"}],
        [{**SAMPLE_FILLS[0], "time": 1}],
        [{**SAMPLE_FILLS[0], "time": 10**20}],
    ],
)
def test_fills_rejects_malformed_or_oversized_adapter_rows(
    monkeypatch, malformed_rows
):
    from app.config import Settings

    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    ex = MagicMock()
    ex.user_fills = AsyncMock(return_value=malformed_rows)
    with TestClient(app) as client:
        client.app.state.mexc = ex
        client.app.state.exchange = ex
        response = client.get(
            "/api/fills", params={"symbol": "SOL_USDT", "limit": 1}
        )

    assert response.status_code == 200
    assert response.json() == {
        "fills": [],
        "supported": True,
        "error": "Exchange fill history unavailable",
    }
    assert "SYNTHETIC_PRIVATE_FILL_RAW" not in response.text


@pytest.mark.asyncio
async def test_fills_hot_swap_rejects_old_client_rows():
    from types import SimpleNamespace

    import app.main as main

    started = asyncio.Event()
    release = asyncio.Event()

    async def old_fills(*, symbol, limit):
        started.set()
        await release.wait()
        return [{"source": "old"}]

    old_client = SimpleNamespace(user_fills=old_fills)
    new_client = object()
    state = SimpleNamespace(mexc=old_client, exchange=old_client)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    task = asyncio.create_task(main.fills(request, None, 100, None))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    state.mexc = new_client
    state.exchange = new_client
    release.set()
    with pytest.raises(main.HTTPException) as exc:
        await task

    assert exc.value.status_code == 409
    assert exc.value.detail == "Exchange changed while loading data. Retry the request."
