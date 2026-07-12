"""Daily regime anchor: compact block, snapshot passthrough, parallel fetch + cache."""

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

    async def klines(self, symbol, interval, limit_hint=200):
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
    t = [1000.0]
    monkeypatch.setattr(ctxmod.time, "time", lambda: t[0])
    await build_market_snapshot("BTC_C", "15m", "1H", client)
    t[0] += ctxmod._DAILY_TTL_S + 1.0
    await build_market_snapshot("BTC_C", "15m", "1H", client)
    assert client.kline_calls["1D"] == 2
