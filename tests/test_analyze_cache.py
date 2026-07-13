"""POST /api/analyze in-memory result cache (LLM-credit saver).

Mirrors the /api/analyze test pattern in test_history.py: mocks the market
snapshot builder and the LLM call, then verifies the cache wraps the whole
analyze flow — a fresh cache hit returns the stored proposal WITHOUT a
second LLM call, `force=true` bypasses it, TTL expiry re-runs, an LLM error
is never cached, and the cache key includes the RESOLVED provider so
switching KI never serves a stale other-provider result.

Advisory-only note: this cache only ever short-circuits the /api/analyze
response body. It never touches order placement/preview/confirm, which
always re-run their own risk gates against the live price.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.config import get_settings
from app.llm.client import LlmError
from app.models import TradeProposal


def _mock_snap(symbol="BTC_USDT"):
    return {
        "symbol": symbol,
        "last_price": 100_000.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 0.0001},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }


def _proposal(action="BUY") -> TradeProposal:
    return TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action=action,
        entry_price=100_000.0 if action != "STAY_OUT" else None,
        stop_loss=99_000.0 if action != "STAY_OUT" else None,
        tp1=102_000.0 if action != "STAY_OUT" else None,
        rrr=2.0 if action != "STAY_OUT" else None,
        recommended_leverage="5x",
        rationale="test proposal for cache",
    )


def _env(monkeypatch, **extra):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("XAI_API_KEY", "test-xai")
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def _patched(analyze_mock, symbol="BTC_USDT"):
    return (
        patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock())),
        patch("app.main.snapshot_to_api_dict", return_value=_mock_snap(symbol)),
        patch("app.main.analyze_with_llm", new=analyze_mock),
        patch("app.main.build_llm_context", return_value={"symbol": symbol}),
    )


def test_analyze_cache_hit_skips_second_llm_call(monkeypatch):
    """Two identical requests within the TTL: only ONE real LLM call, the
    second response is served from cache and flagged accordingly."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        p1, p2, p3, p4 = _patched(analyze_mock)
        with p1, p2, p3, p4:
            r1 = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
            )
            r2 = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
            )

        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["cached"] is False
        assert r2.json()["cached"] is True
        assert isinstance(r2.json()["cached_age_s"], int)
        assert r2.json()["proposal"]["action"] == "BUY"
        assert analyze_mock.await_count == 1

    get_settings.cache_clear()


def test_analyze_force_bypasses_cache(monkeypatch):
    """force=true always re-runs the LLM even with a fresh cache entry."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        p1, p2, p3, p4 = _patched(analyze_mock)
        with p1, p2, p3, p4:
            tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})
            r2 = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H", "force": True},
            )

        assert r2.status_code == 200, r2.text
        assert r2.json()["cached"] is False
        assert analyze_mock.await_count == 2

    get_settings.cache_clear()


def test_analyze_cache_ttl_expiry_reruns(monkeypatch):
    """Once the entry is older than the TTL, the next request re-runs the LLM.

    Rewrites the stored cache timestamp directly (instead of monkeypatching
    the global time.monotonic, which anyio/TestClient's own scheduling also
    relies on) to simulate age passing.
    """
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        p1, p2, p3, p4 = _patched(analyze_mock)
        with p1, p2, p3, p4:
            tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})

            cache = tc.app.state.analyze_cache
            assert len(cache) == 1
            key = next(iter(cache))
            ts, value = cache[key]

            # Still fresh (61s old < 120s TTL) -> cache hit
            cache[key] = (ts - 61.0, value)
            r_fresh = tc.post(
                "/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
            )

            # Now stale (121s old > 120s TTL) -> re-run
            cache[key] = (ts - 121.0, value)
            r_stale = tc.post(
                "/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
            )

        assert r_fresh.json()["cached"] is True
        assert r_stale.json()["cached"] is False
        assert analyze_mock.await_count == 2

    get_settings.cache_clear()


def test_analyze_error_not_cached(monkeypatch):
    """An LLM error must never be cached — the next identical request must
    still hit the LLM again (and can succeed)."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(side_effect=[LlmError("boom"), _proposal()])
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        p1, p2, p3, p4 = _patched(analyze_mock)
        with p1, p2, p3, p4:
            r1 = tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})
            r2 = tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})

        assert r1.status_code == 502
        assert r2.status_code == 200, r2.text
        assert r2.json()["cached"] is False
        assert analyze_mock.await_count == 2

    get_settings.cache_clear()


def test_analyze_stay_out_is_cached(monkeypatch):
    """STAY_OUT is a valid, cacheable result (not treated as an error)."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal("STAY_OUT"))
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        p1, p2, p3, p4 = _patched(analyze_mock)
        with p1, p2, p3, p4:
            r1 = tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})
            r2 = tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})

        assert r1.json()["proposal"]["action"] == "STAY_OUT"
        assert r2.json()["cached"] is True
        assert r2.json()["proposal"]["action"] == "STAY_OUT"
        assert analyze_mock.await_count == 1

    get_settings.cache_clear()


def test_analyze_cache_key_includes_resolved_provider(monkeypatch):
    """Switching the KI provider (hot-swap dropdown) must never serve a
    cached proposal generated for a DIFFERENT provider — the cache key has
    to include the resolved provider."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        tc.app.state.llm_override = None  # resolves to claude (default)
        p1, p2, p3, p4 = _patched(analyze_mock)
        try:
            with p1, p2, p3, p4:
                r1 = tc.post(
                    "/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
                )
                tc.app.state.llm_override = "xai"  # hot-swap to a different provider
                r2 = tc.post(
                    "/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
                )

            assert r1.json()["cached"] is False
            assert r2.json()["cached"] is False  # NOT served from the claude entry
            assert analyze_mock.await_count == 2
        finally:
            # `app` is a module-level singleton shared across the whole test
            # session (other test files `from app.main import app` too) — never
            # leave the hot-swap override dirty for a later, unrelated test.
            tc.app.state.llm_override = None

    get_settings.cache_clear()
