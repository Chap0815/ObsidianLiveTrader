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
