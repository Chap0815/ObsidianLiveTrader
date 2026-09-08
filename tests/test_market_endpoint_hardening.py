"""GET /api/market/{symbol} — Self-DoS hardening (audit finding A, LOW).

Before this fix the endpoint had neither an interval allowlist nor a cache:
tf/htf passed through to the exchange client's INTERVAL_MAP.get(interval,
interval) unvalidated, and every call fired live klines+ticker+funding at the
exchange. A malicious/broken browser tab looping this endpoint with random tf
values could burn through the user's exchange rate limits (GETs deliberately
carry no Origin/CSRF check). Mirrors the /api/mini cache pattern (short TTL +
size-capped in-memory cache) plus a hard interval allowlist (422 on garbage).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _mock_snapshot():
    return {
        "symbol": "BTC_USDT",
        "last_price": 1.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 1.0},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }


def test_market_rejects_invalid_tf():
    with TestClient(app) as client:
        client.app.state.mexc = MagicMock()
        client.app.state.exchange = client.app.state.mexc
        r = client.get(
            "/api/market/BTC_USDT", params={"tf": "'; DROP TABLE x;--", "htf": "1H"}
        )
    assert r.status_code == 422


def test_market_rejects_invalid_htf():
    with TestClient(app) as client:
        client.app.state.mexc = MagicMock()
        client.app.state.exchange = client.app.state.mexc
        r = client.get("/api/market/BTC_USDT", params={"tf": "15m", "htf": "9999x"})
    assert r.status_code == 422


def test_market_accepts_known_intervals():
    with TestClient(app) as client:
        client.app.state.mexc = MagicMock()
        client.app.state.exchange = client.app.state.mexc
        with (
            patch(
                "app.main.build_market_snapshot",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("app.main.snapshot_to_api_dict", return_value=_mock_snapshot()),
        ):
            r = client.get(
                "/api/market/BTC_USDT", params={"tf": "5m", "htf": "4H"}
            )
    assert r.status_code == 200, r.text


def test_market_cache_ttl_skips_upstream_refetch():
    """A second call for the same symbol/tf/htf within the TTL must be served
    from the in-memory cache — no second build_market_snapshot call."""
    with TestClient(app) as client:
        client.app.state.mexc = MagicMock()
        client.app.state.exchange = client.app.state.mexc
        client.app.state.market_cache = {}
        build_mock = AsyncMock(return_value=MagicMock())
        with (
            patch("app.main.build_market_snapshot", new=build_mock),
            patch("app.main.snapshot_to_api_dict", return_value=_mock_snapshot()),
        ):
            r1 = client.get(
                "/api/market/BTC_USDT", params={"tf": "15m", "htf": "1H"}
            )
            r2 = client.get(
                "/api/market/BTC_USDT", params={"tf": "15m", "htf": "1H"}
            )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert build_mock.await_count == 1  # only the FIRST call hit the exchange


def test_market_cache_size_is_capped():
    from app.main import MARKET_CACHE_MAX_ENTRIES

    with TestClient(app) as client:
        client.app.state.mexc = MagicMock()
        client.app.state.exchange = client.app.state.mexc
        client.app.state.market_cache = {}
        with (
            patch(
                "app.main.build_market_snapshot",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("app.main.snapshot_to_api_dict", return_value=_mock_snapshot()),
        ):
            for i in range(MARKET_CACHE_MAX_ENTRIES + 5):
                r = client.get(
                    f"/api/market/SYM{i}_USDT", params={"tf": "15m", "htf": "1H"}
                )
                assert r.status_code == 200, r.text

        assert len(client.app.state.market_cache) <= MARKET_CACHE_MAX_ENTRIES


@pytest.mark.asyncio
async def test_market_different_cache_keys_refresh_concurrently():
    import asyncio

    import httpx

    inflight = 0
    max_inflight = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def build(symbol, tf, htf, client, limit_hint):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        if inflight == 2:
            both_started.set()
        try:
            await release.wait()
            return {"symbol": symbol}
        finally:
            inflight -= 1

    app.state.exchange = MagicMock()
    app.state.mexc = app.state.exchange
    app.state.market_cache = {}
    app.state.market_lock = asyncio.Lock()
    app.state.market_locks = {}
    transport = httpx.ASGITransport(app=app)

    with (
        patch("app.main.build_market_snapshot", new=build),
        patch("app.main.snapshot_to_api_dict", side_effect=lambda snapshot: snapshot),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            tasks = [
                asyncio.create_task(client.get("/api/market/BTC_USDT")),
                asyncio.create_task(client.get("/api/market/ETH_USDT")),
            ]
            try:
                await asyncio.wait_for(both_started.wait(), timeout=1.0)
            finally:
                release.set()
                responses = await asyncio.gather(*tasks)

    assert all(response.status_code == 200 for response in responses)
    assert max_inflight == 2
    assert app.state.market_locks == {}
