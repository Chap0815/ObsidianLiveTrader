"""Open interest passthrough: snapshot -> api dict -> llm context, MEXC None-fallback."""

import pytest

from app.analysis.context import (
    build_market_snapshot,
    snapshot_to_api_dict,
)
from app.config import Settings
from app.llm.client import build_llm_context
from app.models import Candle, ContractMeta, FundingRate, Ticker


class _FakeMarketClient:
    """MEXC-like client WITHOUT market_extras -> exercises the None fallback."""

    def _candles(self, base=100.0, n=60):
        return [
            Candle(
                time=(1_700_000_000 + i * 900) * 1000,
                open=base, high=base + 1, low=base - 1, close=base + 0.5, vol=5.0,
            )
            for i in range(n)
        ]

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
        return self._candles()


def test_snapshot_to_api_dict_includes_market_key():
    from app.models import MarketSnapshot, TimeframeSlice

    snap = MarketSnapshot(
        symbol="BTC", last_price=100.0,
        ltf=TimeframeSlice(tf="15m"), htf=TimeframeSlice(tf="1H"),
        market={"open_interest": 1234.0, "premium": 0.001,
                "oi_change_pct_1h": 2.5, "oi_change_pct_4h": None, "prev_day_px": 99.0},
    )
    d = snapshot_to_api_dict(snap)
    assert d["market"]["open_interest"] == 1234.0
    assert d["market"]["oi_change_pct_1h"] == 2.5


def test_build_llm_context_exposes_market_block_without_prev_day_px():
    market = {
        "symbol": "BTC", "last_price": 100.0, "funding": {}, "contract": {},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
        "market": {"open_interest": 500.0, "premium": 0.002,
                   "oi_change_pct_1h": 1.1, "oi_change_pct_4h": 3.3, "prev_day_px": 98.0},
    }
    ctx = build_llm_context(market, {}, Settings(include_account_in_llm=False))
    # K2-05: `premium` is dropped as dead token ballast (never read by the
    # prompt); OI fields remain because open_interest is present here.
    assert ctx["market"] == {
        "open_interest": 500.0, "oi_change_pct_1h": 1.1,
        "oi_change_pct_4h": 3.3,
    }
    assert "premium" not in ctx["market"]
    assert "prev_day_px" not in ctx["market"]


@pytest.mark.asyncio
async def test_build_market_snapshot_mexc_like_client_yields_none_market():
    snap = await build_market_snapshot("BTC_USDT", "15m", "1H", _FakeMarketClient())
    assert snap.market["open_interest"] is None
    assert snap.market["oi_change_pct_1h"] is None


def _hl_client():
    from app.hyperliquid.client import HyperliquidClient
    return HyperliquidClient(testnet=True)  # no keys needed for OI-history math


def test_oi_change_none_on_first_sample():
    c = _hl_client()
    assert c._record_oi_and_change("BTC", 1000.0, now=10_000.0) == (None, None)


def test_oi_change_1h_computed_4h_insufficient():
    c = _hl_client()
    t0 = 10_000.0
    c._record_oi_and_change("BTC", 1000.0, now=t0)
    ch1, ch4 = c._record_oi_and_change("BTC", 1100.0, now=t0 + 3600.0)
    assert ch1 == 10.0     # +10% over exactly 1h
    assert ch4 is None     # only 1h of history — not >= 50% of the 4h lookback


def test_oi_change_ignores_nonpositive_and_none_oi():
    c = _hl_client()
    assert c._record_oi_and_change("BTC", None, now=1.0) == (None, None)
    assert c._record_oi_and_change("BTC", 0.0, now=2.0) == (None, None)
    assert c._oi_history.get("BTC", []) == []  # nothing recorded


def test_prompt_mentions_open_interest_and_null_skip():
    from app.llm.prompts import build_system_prompt
    p = build_system_prompt()
    assert "Open interest" in p
    assert "short covering" in p
    assert "SKIP this step" in p
