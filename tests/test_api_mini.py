"""GET /api/mini — lightweight multi-coin candle snapshot for the overview grid."""

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.models import Candle


def _candles(closes):
    return [
        Candle(
            time=1_710_000_000 + i * 900,
            open=c,
            high=c + 1,
            low=c - 1,
            close=c,
            vol=10.0,
            amount=100.0,
        )
        for i, c in enumerate(closes)
    ]


def _mock_client(by_symbol):
    mock = MagicMock()

    async def klines(symbol, interval, limit_hint=200):
        return by_symbol[symbol]

    mock.klines = AsyncMock(side_effect=klines)
    return mock


def test_mini_returns_candles_and_change():
    mock = _mock_client(
        {
            "BTC_USDT": _candles([100.0, 102.0, 110.0]),
            "ETH_USDT": _candles([50.0, 49.0, 48.0]),
        }
    )
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get(
            "/api/mini", params={"symbols": "BTC_USDT,ETH_USDT", "limit": 3}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    res = {x["symbol"]: x for x in body["results"]}
    assert res["BTC_USDT"]["last_price"] == 110.0
    assert res["BTC_USDT"]["change_pct"] == 10.0  # (110-100)/100*100
    assert res["ETH_USDT"]["change_pct"] == -4.0  # (48-50)/50*100
    assert len(res["BTC_USDT"]["candles"]) == 3
    assert set(res["BTC_USDT"]["candles"][0]) == {"time", "open", "high", "low", "close"}


def test_mini_empty_symbols_is_ok():
    mock = _mock_client({})
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get("/api/mini", params={"symbols": ""})
    assert r.status_code == 200
    assert r.json() == {"results": [], "errors": []}


def test_mini_per_symbol_error_isolated():
    def _mk():
        mock = MagicMock()

        async def klines(symbol, interval, limit_hint=200):
            if symbol == "ETH_USDT":
                raise RuntimeError("boom")
            return _candles([1.0, 1.1])

        mock.klines = AsyncMock(side_effect=klines)
        return mock

    mock = _mk()
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get(
            "/api/mini", params={"symbols": "BTC_USDT,ETH_USDT", "limit": 2}
        )
    assert r.status_code == 200
    body = r.json()
    assert [x["symbol"] for x in body["results"]] == ["BTC_USDT"]
    assert len(body["errors"]) == 1 and body["errors"][0].startswith("ETH_USDT:")
