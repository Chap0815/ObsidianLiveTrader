"""GET /api/mini — lightweight multi-coin candle snapshot for the overview grid."""

from unittest.mock import AsyncMock, MagicMock

import pytest
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

    async def klines(symbol, interval, limit_hint=200, *, paced=False):
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


def test_mini_nonfinite_derived_change_degrades_to_none():
    candles = [
        Candle(
            time=1_710_000_000,
            open=5e-324,
            high=5e-324,
            low=5e-324,
            close=5e-324,
            vol=1.0,
        ),
        Candle(
            time=1_710_000_900,
            open=1e308,
            high=1e308,
            low=1e308,
            close=1e308,
            vol=1.0,
        ),
    ]
    mock = _mock_client({"BTC_USDT": candles})

    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        client.app.state.mini_cache = {}
        response = client.get(
            "/api/mini", params={"symbols": "BTC_USDT", "limit": 2}
        )

    assert response.status_code == 200
    assert response.json()["results"][0]["change_pct"] is None


def test_mini_empty_symbols_is_ok():
    mock = _mock_client({})
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get("/api/mini", params={"symbols": ""})
    assert r.status_code == 200
    assert r.json() == {"results": [], "errors": []}


def test_mini_rejects_oversized_symbol_query_before_processing():
    mock = _mock_client({})
    oversized = ",".join(["X"] * 601)

    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        response = client.get("/api/mini", params={"symbols": oversized})

    assert response.status_code == 422
    mock.klines.assert_not_awaited()


def test_mini_invalid_symbol_skipped_valid_returned():
    """An unparsable symbol must not 400 the whole overview request."""
    mock = _mock_client({"BTC_USDT": _candles([100.0, 101.0])})
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get(
            "/api/mini", params={"symbols": "not-a-symbol,BTC_USDT", "limit": 2}
        )
    assert r.status_code == 200
    body = r.json()
    assert [x["symbol"] for x in body["results"]] == ["BTC_USDT"]
    assert len(body["errors"]) == 1
    assert "not-a-symbol" in body["errors"][0] or "NOT-A-SYMBOL" in body["errors"][0]


def test_mini_invalid_interval_rejected_without_upstream_call():
    mock = _mock_client({"BTC_USDT": _candles([100.0, 101.0])})
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get(
            "/api/mini", params={"symbols": "BTC_USDT", "tf": "garbage", "limit": 2}
        )

    assert r.status_code == 422
    assert "Invalid interval" in r.json()["detail"]
    mock.klines.assert_not_awaited()


def test_mini_per_symbol_error_isolated():
    def _mk():
        mock = MagicMock()

        async def klines(symbol, interval, limit_hint=200, *, paced=False):
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


def test_mini_cache_ttl_skips_upstream_refetch():
    """V3-03: a second call for the same symbols/tf/limit within the TTL must
    be served from the in-memory cache — no second round of upstream
    client.klines() calls (mirrors the /api/news cache-hit contract)."""
    mock = _mock_client({"BTC_USDT": _candles([100.0, 101.0])})
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r1 = client.get("/api/mini", params={"symbols": "BTC_USDT", "limit": 2})
        r2 = client.get("/api/mini", params={"symbols": "BTC_USDT", "limit": 2})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert mock.klines.await_count == 1  # only the FIRST call hit the exchange


def test_mini_cache_size_is_capped():
    """The mini cache must not grow unbounded — once it exceeds the cap, the
    oldest entry is dropped to bound memory growth (mirrors the analyze_cache
    size cap in test_analyze_cache.py::test_analyze_cache_size_is_capped)."""
    from app.main import MINI_CACHE_MAX_ENTRIES

    mock = _mock_client({"BTC_USDT": _candles([100.0, 101.0])})
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        client.app.state.mini_cache = {}
        for i in range(MINI_CACHE_MAX_ENTRIES + 5):
            r = client.get(
                "/api/mini",
                params={"symbols": "BTC_USDT", "limit": i + 2, "tf": "15m"},
            )
            assert r.status_code == 200, r.text

        assert len(client.app.state.mini_cache) <= MINI_CACHE_MAX_ENTRIES


def test_mini_cache_preserves_errors():
    """A cached payload must still carry per-symbol errors — a cache hit must
    not silently swallow a failure that was present on the cache-filling
    request (V3-02: errors are passed through, never dropped)."""
    def _mk():
        mock = MagicMock()

        async def klines(symbol, interval, limit_hint=200, *, paced=False):
            if symbol == "ETH_USDT":
                raise RuntimeError("boom")
            return _candles([1.0, 1.1])

        mock.klines = AsyncMock(side_effect=klines)
        return mock

    mock = _mk()
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r1 = client.get(
            "/api/mini", params={"symbols": "BTC_USDT,ETH_USDT", "limit": 2}
        )
        r2 = client.get(
            "/api/mini", params={"symbols": "BTC_USDT,ETH_USDT", "limit": 2}
        )
    assert r1.json() == r2.json()
    body2 = r2.json()
    assert [x["symbol"] for x in body2["results"]] == ["BTC_USDT"]
    assert len(body2["errors"]) == 1 and body2["errors"][0].startswith("ETH_USDT:")
    assert mock.klines.await_count == 2  # BTC + ETH fetched once each, not re-fetched


@pytest.mark.asyncio
async def test_mini_different_cache_keys_refresh_concurrently():
    import asyncio

    import httpx

    inflight = 0
    max_inflight = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def klines(symbol, interval, limit_hint=200, *, paced=False):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        if inflight == 2:
            both_started.set()
        try:
            await release.wait()
            return _candles([100.0, 101.0])
        finally:
            inflight -= 1

    mock = MagicMock()
    mock.klines = klines
    app.state.exchange = mock
    app.state.mexc = mock
    app.state.mini_cache = {}
    app.state.mini_lock = asyncio.Lock()
    app.state.mini_locks = {}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        tasks = [
            asyncio.create_task(client.get("/api/mini?symbols=BTC_USDT&limit=2")),
            asyncio.create_task(client.get("/api/mini?symbols=ETH_USDT&limit=2")),
        ]
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1.0)
        finally:
            release.set()
            responses = await asyncio.gather(*tasks)

    assert all(response.status_code == 200 for response in responses)
    assert max_inflight == 2
    assert app.state.mini_locks == {}
