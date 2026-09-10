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

import pytest

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


def test_analyze_cache_key_includes_scanner_verdict(monkeypatch):
    """A verdict-less manual analyze and a scan-triggered analyze (with a
    scanner_verdict) for the same coin/tf/htf/provider must NOT share a cache
    entry — otherwise a verdict-mismatched proposal could be served from
    cache for up to the TTL (re-opening the scanner<->analyzer B1 gap)."""
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
                "/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
            )
            r2 = tc.post(
                "/api/analyze",
                json={
                    "symbol": "BTC_USDT",
                    "tf": "15m",
                    "htf": "1H",
                    "scanner_verdict": {
                        "bias": "long",
                        "setup": "breakout",
                        "key_level": 100_500.0,
                        "score": 0.8,
                    },
                },
            )

        assert r1.json()["cached"] is False
        assert r2.json()["cached"] is False  # NOT served from the verdict-less entry
        assert analyze_mock.await_count == 2

    get_settings.cache_clear()


def test_analyze_cache_key_verdict_hit_when_identical(monkeypatch):
    """Two identical requests, both WITH the same scanner_verdict, must still
    hit the cache on the second call (verdict inclusion must not break the
    normal cache-hit path)."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    verdict = {"bias": "long", "setup": "breakout", "key_level": 100_500.0, "score": 0.8}

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        p1, p2, p3, p4 = _patched(analyze_mock)
        with p1, p2, p3, p4:
            r1 = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H", "scanner_verdict": verdict},
            )
            r2 = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H", "scanner_verdict": verdict},
            )

        assert r1.json()["cached"] is False
        assert r2.json()["cached"] is True
        assert analyze_mock.await_count == 1

    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            {"bias": "long", "score": 7, "reason": "bounced off support"},
            {"bias": "long", "score": 7, "reason": "momentum breakout"},
        ),
        ({"bias": "long"}, {"bias": "long", "score": 0}),
    ],
)
def test_analyze_cache_key_distinguishes_scanner_context(monkeypatch, first, second):
    """Different sanitized scanner input must not reuse an analysis generated
    for a different rationale or for a verdict where score was absent."""
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
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H", "scanner_verdict": first},
            )
            r2 = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H", "scanner_verdict": second},
            )

        assert r1.json()["cached"] is False
        assert r2.json()["cached"] is False
        assert analyze_mock.await_count == 2

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


# --- Singleflight on cache MISS (mirrors /api/news's news_lock pattern) -----


@pytest.mark.asyncio
async def test_concurrent_identical_cache_miss_calls_llm_once(monkeypatch):
    """Two concurrent, identical cache-miss analyze requests must result in
    ONE real LLM call — the second waits on the PER-KEY lock, then re-checks
    the (now-populated) cache instead of firing its own LLM call.

    (Task 23: the single global analyze_lock was replaced by a per-key lock
    dict `app.state.analyze_locks`; identical requests share one key → one
    lock → still exactly one LLM call.)"""
    import asyncio as _asyncio

    import httpx

    _env(monkeypatch)
    from app.main import app

    llm_calls = 0
    started = _asyncio.Event()
    release = _asyncio.Event()

    async def fake_analyze_with_llm(context, settings):
        nonlocal llm_calls
        llm_calls += 1
        started.set()
        await release.wait()
        return _proposal()

    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    app.state.mexc = client
    app.state.analyze_cache = {}
    app.state.analyze_locks = {}

    p1 = patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock()))
    p2 = patch("app.main.snapshot_to_api_dict", return_value=_mock_snap())
    p3 = patch("app.main.analyze_with_llm", new=fake_analyze_with_llm)
    p4 = patch("app.main.build_llm_context", return_value={"symbol": "BTC_USDT"})

    transport = httpx.ASGITransport(app=app)
    body = {"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
    with p1, p2, p3, p4:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            t1 = _asyncio.create_task(ac.post("/api/analyze", json=body))
            await _asyncio.wait_for(started.wait(), timeout=2.0)
            t2 = _asyncio.create_task(ac.post("/api/analyze", json=body))
            await _asyncio.sleep(0.1)  # let t2 reach (and block on) the per-key lock
            release.set()
            r1 = await t1
            r2 = await t2

    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert llm_calls == 1
    # Exactly one of the two got the fresh (non-cached) response, the other the
    # cache hit — which one wins the race is not the point, only the call count.
    cached_flags = sorted([r1.json()["cached"], r2.json()["cached"]])
    assert cached_flags == [False, True]

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_analyze_different_coins_run_concurrently(monkeypatch):
    """Task 23 (L2X-01): two DIFFERENT coins (→ two different cache keys) must
    run their LLM calls CONCURRENTLY, not serialized behind one global lock.

    Both fake LLM calls block on a shared gate; the test only releases the gate
    AFTER both have started. Under the old single global analyze_lock the
    second request would block before ever starting its LLM call, `both_started`
    would never fire, and `wait_for` would time out."""
    import asyncio as _asyncio

    import httpx

    _env(monkeypatch)
    from app.main import app

    inflight = 0
    max_inflight = 0
    gate = _asyncio.Event()
    both_started = _asyncio.Event()

    async def fake_analyze_with_llm(context, settings):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        if inflight >= 2:
            both_started.set()
        await gate.wait()
        inflight -= 1
        return _proposal()

    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    app.state.mexc = client
    app.state.analyze_cache = {}
    app.state.analyze_locks = {}

    p1 = patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock()))
    p2 = patch("app.main.snapshot_to_api_dict", return_value=_mock_snap())
    p3 = patch("app.main.analyze_with_llm", new=fake_analyze_with_llm)
    p4 = patch("app.main.build_llm_context", return_value={"symbol": "BTC_USDT"})

    transport = httpx.ASGITransport(app=app)
    with p1, p2, p3, p4:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            t1 = _asyncio.create_task(
                ac.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"})
            )
            t2 = _asyncio.create_task(
                ac.post("/api/analyze", json={"symbol": "ETH_USDT", "tf": "15m", "htf": "1H"})
            )
            # If the two coins were serialized, only one LLM call would ever be
            # in flight and this would time out.
            await _asyncio.wait_for(both_started.wait(), timeout=2.0)
            gate.set()
            r1 = await t1
            r2 = await t2

    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert max_inflight == 2  # both LLM calls were genuinely concurrent
    # The per-key lock dict must empty out once both requests finish (no leak).
    assert app.state.analyze_locks == {}

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_same_key_still_singleflight(monkeypatch):
    """Task 23: the per-key scheme must PRESERVE the round-1 guarantee — two
    concurrent identical requests (same key) still trigger exactly ONE LLM
    call; the second waits on the shared per-key lock and serves the cache."""
    import asyncio as _asyncio

    import httpx

    _env(monkeypatch)
    from app.main import app

    llm_calls = 0
    started = _asyncio.Event()
    release = _asyncio.Event()

    async def fake_analyze_with_llm(context, settings):
        nonlocal llm_calls
        llm_calls += 1
        started.set()
        await release.wait()
        return _proposal()

    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    app.state.mexc = client
    app.state.analyze_cache = {}
    app.state.analyze_locks = {}

    p1 = patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock()))
    p2 = patch("app.main.snapshot_to_api_dict", return_value=_mock_snap())
    p3 = patch("app.main.analyze_with_llm", new=fake_analyze_with_llm)
    p4 = patch("app.main.build_llm_context", return_value={"symbol": "BTC_USDT"})

    transport = httpx.ASGITransport(app=app)
    body = {"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
    with p1, p2, p3, p4:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            t1 = _asyncio.create_task(ac.post("/api/analyze", json=body))
            await _asyncio.wait_for(started.wait(), timeout=2.0)
            t2 = _asyncio.create_task(ac.post("/api/analyze", json=body))
            await _asyncio.sleep(0.1)  # let t2 reach (and block on) the per-key lock
            release.set()
            r1 = await t1
            r2 = await t2

    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert llm_calls == 1
    cached_flags = sorted([r1.json()["cached"], r2.json()["cached"]])
    assert cached_flags == [False, True]
    assert app.state.analyze_locks == {}  # entry deleted on idle

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_waiting_analysis_rejects_invalidated_generation_before_work(monkeypatch):
    from types import SimpleNamespace

    import app.main as main
    from app.models import AnalyzeRequest

    _env(monkeypatch)
    old_cache = {}
    state = SimpleNamespace(
        mexc=MagicMock(),
        exchange=MagicMock(),
        analyze_cache=old_cache,
        analyze_locks={},
        llm_override=None,
    )
    state.exchange = state.mexc
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    class InvalidatingWait:
        async def __aenter__(self):
            state.analyze_cache = {}

        async def __aexit__(self, *_exc):
            return None

    build = AsyncMock()
    with (
        patch("app.main._keyed_singleflight_lock", return_value=InvalidatingWait()),
        patch("app.main.build_market_snapshot", new=build),
    ):
        with pytest.raises(main.HTTPException) as exc:
            await main.analyze(
                request,
                AnalyzeRequest(symbol="BTC_USDT", tf="15m", htf="1H"),
                None,
            )

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "Analysis configuration changed. Retry the request."
    )
    build.assert_not_awaited()
    assert old_cache == {}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_inflight_analysis_is_rejected_after_configuration_invalidation(monkeypatch):
    """A settings hot-update must not return or cache an old-model result."""
    import asyncio as _asyncio

    import httpx

    _env(monkeypatch)
    from app.main import app

    started = _asyncio.Event()
    release = _asyncio.Event()

    async def fake_analyze_with_llm(context, settings):
        started.set()
        await release.wait()
        return _proposal()

    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )
    old_cache = {}
    app.state.mexc = client
    app.state.analyze_cache = old_cache
    app.state.analyze_locks = {}

    p1 = patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock()))
    p2 = patch("app.main.snapshot_to_api_dict", return_value=_mock_snap())
    p3 = patch("app.main.analyze_with_llm", new=fake_analyze_with_llm)
    p4 = patch("app.main.build_llm_context", return_value={"symbol": "BTC_USDT"})

    transport = httpx.ASGITransport(app=app)
    body = {"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
    with p1, p2, p3, p4:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            task = _asyncio.create_task(ac.post("/api/analyze", json=body))
            await _asyncio.wait_for(started.wait(), timeout=2.0)
            app.state.analyze_cache = {}
            new_cache = app.state.analyze_cache
            release.set()
            response = await task

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "Analysis configuration changed. Retry the request."
    )
    assert old_cache == {}
    assert app.state.analyze_cache is new_cache
    assert new_cache == {}
    get_settings.cache_clear()


def test_analyze_cache_size_is_capped(monkeypatch):
    """The cache must not grow unbounded — once it exceeds the cap, the
    oldest entry is dropped to bound memory growth."""
    _env(monkeypatch)
    from fastapi.testclient import TestClient

    from app.main import app
    from app.main import ANALYZE_CACHE_MAX_ENTRIES

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        for i in range(ANALYZE_CACHE_MAX_ENTRIES + 5):
            sym = f"COIN{i}_USDT"
            p1, p2, p3, p4 = _patched(analyze_mock, symbol=sym)
            with p1, p2, p3, p4:
                r = tc.post("/api/analyze", json={"symbol": sym, "tf": "15m", "htf": "1H"})
            assert r.status_code == 200, r.text

        assert len(tc.app.state.analyze_cache) <= ANALYZE_CACHE_MAX_ENTRIES

    get_settings.cache_clear()
