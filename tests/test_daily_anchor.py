"""Daily regime anchor: compact block, snapshot passthrough, parallel fetch + cache."""

from types import SimpleNamespace

import pytest

from app.analysis import context as ctxmod
from app.analysis.context import build_market_snapshot, clear_daily_cache, snapshot_to_api_dict
from app.llm.client import compact_daily_for_llm
from app.models import Candle, ContractMeta, FundingRate, Ticker


class _CountingClient:
    def __init__(self):
        self.kline_calls = {}

    async def ticker(self, symbol):
        return Ticker(symbol=symbol, last_price=100.5, timestamp=1)

    async def funding_rate(self, symbol):
        return FundingRate(symbol=symbol, funding_rate=0.0, timestamp=1)

    async def contract_meta(self, symbol):
        return ContractMeta(
            symbol=symbol, contract_size=1.0, price_unit=0.1, vol_unit=0.001,
            min_vol=0.001, max_vol=1e6, max_leverage=50, api_allowed=True,
        )

    async def klines(self, symbol, interval, limit_hint=200, *, paced=False):
        self.kline_calls[interval] = self.kline_calls.get(interval, 0) + 1
        return [
            Candle(time=(1_700_000_000 + i * 900) * 1000,
                   open=100.0, high=101.0, low=99.0, close=100.5, vol=5.0)
            for i in range(60)
        ]


def test_compact_daily_is_ultra_compact():
    slice_dict = {
        "tf": "1D",
        "candles": [{"close": 110.0}],
        "indicators": {"last": {"ema20": 100.0, "ema50": 95.0, "ema200": 90.0}, "rvol": 1.2},
        "structure": {"swings": {
            "highs": [{"price": h, "time": h} for h in range(1, 6)],
            "lows": [{"price": l, "time": l} for l in range(1, 6)],
        }},
    }
    c = compact_daily_for_llm(slice_dict)
    assert "recent_candles" not in c and "indicators_tail" not in c
    assert c["read"]["ema_stack"] == "bullish"
    assert c["read"]["price_vs_ema20_pct"] == 10.0
    assert len(c["recent_swing_highs"]) == 3 and len(c["recent_swing_lows"]) == 3


@pytest.mark.asyncio
async def test_snapshot_includes_daily_slice():
    clear_daily_cache()
    snap = await build_market_snapshot("BTC_A", "15m", "1H", _CountingClient())
    d = snapshot_to_api_dict(snap)
    assert d["daily"] is not None and d["daily"]["tf"] == "1D"


@pytest.mark.asyncio
async def test_daily_fetch_is_cached_within_ttl():
    clear_daily_cache()
    client = _CountingClient()
    await build_market_snapshot("BTC_B", "15m", "1H", client)
    await build_market_snapshot("BTC_B", "15m", "1H", client)
    assert client.kline_calls["1D"] == 1   # second call served from cache
    assert client.kline_calls["15m"] == 2  # ltf not cached


@pytest.mark.asyncio
async def test_daily_cache_expires(monkeypatch):
    clear_daily_cache()
    client = _CountingClient()
    wall = [1_000.0]
    elapsed = [1_000.0]
    monkeypatch.setattr(
        ctxmod,
        "time",
        SimpleNamespace(
            time=lambda: wall[0],
            monotonic=lambda: elapsed[0],
        ),
    )
    await build_market_snapshot("BTC_C", "15m", "1H", client)
    wall[0] = 100.0
    elapsed[0] += ctxmod._DAILY_TTL_S + 1.0
    await build_market_snapshot("BTC_C", "15m", "1H", client)
    assert client.kline_calls["1D"] == 2


class _DailyRaisingClient(_CountingClient):
    """Same as _CountingClient, but klines() raises for the daily interval —
    used to verify the fail-safe: a daily fetch error must not blow up
    build_market_snapshot, it must just yield daily=None."""

    async def klines(self, symbol, interval, limit_hint=200, *, paced=False):
        if interval == "1D":
            self.kline_calls[interval] = self.kline_calls.get(interval, 0) + 1
            raise RuntimeError("daily fetch boom")
        return await super().klines(symbol, interval, limit_hint=limit_hint)


@pytest.mark.asyncio
async def test_daily_fetch_error_yields_none_daily():
    clear_daily_cache()
    client = _DailyRaisingClient()
    snap = await build_market_snapshot("BTC_D", "15m", "1H", client)
    d = snapshot_to_api_dict(snap)
    assert d["daily"] is None
    assert d["htf"] is not None and d["ltf"] is not None


class _DepthClient:
    """klines() returns exactly `limit_hint` rising daily candles and records
    every depth it was asked for — lets us prove the cache is depth-aware."""

    def __init__(self):
        self.daily_limits: list[int] = []

    async def klines(self, symbol, interval, limit_hint=200, *, paced=False):
        self.daily_limits.append(limit_hint)
        return [
            Candle(
                time=(1_700_000_000 + i * 86400) * 1000,
                open=100.0 + i * 0.5, high=101.0 + i * 0.5,
                low=99.0 + i * 0.5, close=100.5 + i * 0.5, vol=5.0,
            )
            for i in range(limit_hint)
        ]


@pytest.mark.asyncio
async def test_daily_cache_is_depth_aware_shallow_not_served_to_deep():
    """B1: a shallow scan fetch (120) must NEVER be served back to the deeper
    analysis request (260) within the TTL — that was leaving daily.ema_stack
    'unknown' on the normal scan->analyze flow. The deeper series then also
    satisfies later shallow requests without an extra fetch."""
    from app.analysis.context import _fetch_daily_candles

    clear_daily_cache()
    client = _DepthClient()
    shallow = await _fetch_daily_candles(client, "DEPTH", "1D", 120)
    assert len(shallow) == 120
    assert client.daily_limits == [120]

    deep = await _fetch_daily_candles(client, "DEPTH", "1D", 260)
    assert len(deep) == 260                     # NOT the cached 120 series
    assert client.daily_limits == [120, 260]    # a real refetch happened

    again = await _fetch_daily_candles(client, "DEPTH", "1D", 120)
    assert len(again) == 260                     # deeper cache satisfies shallow
    assert client.daily_limits == [120, 260]     # no extra fetch


def test_daily_stack_is_real_with_sufficient_candles_but_unknown_when_shallow():
    """B2: EMA200 needs >= 200 candles. With the scanner's old 120 the daily
    regime anchor was ALWAYS 'unknown'; with >= 250 it computes for real."""
    from app.llm.scanner import _daily_stack

    candles = [
        Candle(
            time=(1_700_000_000 + i * 86400) * 1000,
            open=100.0 + i * 0.5, high=101.0 + i * 0.5,
            low=99.0 + i * 0.5, close=100.5 + i * 0.5, vol=5.0,
        )
        for i in range(260)
    ]
    assert _daily_stack("1D", candles) == "bullish"      # real anchor
    assert _daily_stack("1D", candles[:120]) == "unknown"  # the dead-anchor bug


def test_prompt_mentions_daily_regime_anchor():
    from app.llm.prompts import build_system_prompt
    p = build_system_prompt()
    assert "REGIME anchor" in p
    assert "daily.read.ema_stack" in p


def test_prompt_daily_caps_confidence_low():
    from app.llm.prompts import build_system_prompt
    p = build_system_prompt()
    # against-daily-regime must appear as a 'low' setup_confidence condition
    assert 'caps setup_confidence at "low"' in p
