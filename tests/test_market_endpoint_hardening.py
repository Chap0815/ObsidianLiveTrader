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


@pytest.mark.asyncio
async def test_symbols_concurrent_cache_misses_share_one_upstream_read():
    import asyncio
    from types import SimpleNamespace

    from app.main import symbols

    class ObservedLock:
        def __init__(self):
            self._lock = asyncio.Lock()
            self.attempts = 0
            self.second_waiter = asyncio.Event()

        async def __aenter__(self):
            self.attempts += 1
            if self.attempts == 2:
                self.second_waiter.set()
            await self._lock.acquire()
            return self

        async def __aexit__(self, *_exc):
            self._lock.release()

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def list_symbols():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ["BTC_USDT"]

    client = SimpleNamespace(list_symbols=list_symbols)
    lock = ObservedLock()
    state = SimpleNamespace(
        mexc=client,
        exchange=client,
        symbols_cache=None,
        symbols_lock=lock,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    first = asyncio.create_task(symbols(request))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    second = asyncio.create_task(symbols(request))
    await asyncio.wait_for(lock.second_waiter.wait(), timeout=1.0)
    release.set()
    responses = await asyncio.gather(first, second)

    assert calls == 1
    assert responses == [
        {"symbols": ["BTC_USDT"], "error": None},
        {"symbols": ["BTC_USDT"], "error": None},
    ]


@pytest.mark.asyncio
async def test_symbols_hot_swap_retries_with_active_client():
    import asyncio
    from types import SimpleNamespace

    from app.main import symbols

    started = asyncio.Event()
    release = asyncio.Event()

    async def old_list_symbols():
        started.set()
        await release.wait()
        return ["OLD_USDT"]

    old_client = SimpleNamespace(list_symbols=old_list_symbols)
    new_client = SimpleNamespace(list_symbols=AsyncMock(return_value=["NEW_USDT"]))
    state = SimpleNamespace(
        mexc=old_client,
        exchange=old_client,
        symbols_cache=None,
        symbols_lock=asyncio.Lock(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    task = asyncio.create_task(symbols(request))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    state.mexc = new_client
    state.exchange = new_client
    state.symbols_cache = None
    release.set()
    response = await task

    assert response == {"symbols": ["NEW_USDT"], "error": None}
    new_client.list_symbols.assert_awaited_once_with()
    assert state.symbols_cache is not None
    assert state.symbols_cache[1] == ["NEW_USDT"]


@pytest.mark.asyncio
async def test_symbols_canonicalizes_deduplicates_and_skips_invalid_rows():
    import asyncio
    from types import SimpleNamespace

    from app.main import symbols

    exchange = SimpleNamespace(
        list_symbols=AsyncMock(
            return_value=[
                "ETH_USDT",
                " btc-usdt ",
                "BTC_USDT",
                "BAD",
                None,
                ["SOL_USDT"],
            ]
        )
    )
    state = SimpleNamespace(
        mexc=exchange,
        exchange=exchange,
        symbols_cache=None,
        symbols_lock=asyncio.Lock(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    response = await symbols(request)

    assert response == {"symbols": ["BTC_USDT", "ETH_USDT"], "error": None}
    assert state.symbols_cache[1] == ["BTC_USDT", "ETH_USDT"]


@pytest.mark.asyncio
async def test_symbols_malformed_top_level_uses_visible_fallback():
    import asyncio
    from types import SimpleNamespace

    from app.main import FALLBACK_COINS, symbols

    exchange = SimpleNamespace(list_symbols=AsyncMock(return_value="BTC_USDT"))
    state = SimpleNamespace(
        mexc=exchange,
        exchange=exchange,
        symbols_cache=None,
        symbols_lock=asyncio.Lock(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    response = await symbols(request)

    assert response["fallback"] is True
    assert response["error"] is None
    assert response["symbols"] == [coin + "_USDT" for coin in FALLBACK_COINS]
    assert state.symbols_cache is None


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


@pytest.mark.asyncio
async def test_market_hot_swap_rejects_old_client_payload():
    import asyncio
    from types import SimpleNamespace

    import app.main as main

    started = asyncio.Event()
    release = asyncio.Event()
    old_client = object()
    new_client = object()
    old_cache = {}
    state = SimpleNamespace(
        mexc=old_client,
        exchange=old_client,
        market_cache=old_cache,
        market_locks={},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    async def build(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {"source": "old"}

    with (
        patch("app.main.build_market_snapshot", new=build),
        patch("app.main.snapshot_to_api_dict") as serialize,
    ):
        task = asyncio.create_task(
            main.market(request, "BTC_USDT", tf="15m", htf="1H")
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        state.mexc = new_client
        state.exchange = new_client
        state.market_cache = {}
        release.set()
        with pytest.raises(main.HTTPException) as exc:
            await task

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "Exchange changed while loading data. Retry the request."
    )
    serialize.assert_not_called()
    assert old_cache == {}
    assert state.market_cache == {}


@pytest.mark.asyncio
async def test_market_waiter_rejects_old_cache_hit_after_hot_swap():
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import app.main as main

    started = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def delayed_lock(*_args):
        started.set()
        await release.wait()
        yield

    old_client = object()
    new_client = object()
    old_cache = {}
    state = SimpleNamespace(
        mexc=old_client,
        exchange=old_client,
        market_cache=old_cache,
        market_locks={},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    build = AsyncMock()

    with (
        patch("app.main._keyed_singleflight_lock", new=delayed_lock),
        patch("app.main.build_market_snapshot", new=build),
    ):
        task = asyncio.create_task(
            main.market(request, "BTC_USDT", tf="15m", htf="1H")
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        old_cache[("BTC_USDT", "15m", "1H")] = (
            main._time.monotonic(),
            {"source": "old-cache"},
        )
        state.mexc = new_client
        state.exchange = new_client
        state.market_cache = {}
        release.set()
        with pytest.raises(main.HTTPException) as exc:
            await task

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "Exchange changed while loading data. Retry the request."
    )
    build.assert_not_awaited()
