"""Task 17: regime-label decoupling (M2-03), BTC regime anchor (K2-02),
token-ballast removal (K2-05)."""

import pytest

from app.analysis.context import (
    clear_btc_regime_cache,
    clear_daily_cache,
    fetch_btc_regime,
    is_btc_symbol,
)
from app.config import Settings
from app.llm.client import _ema_stack_label, build_llm_context
from app.llm.prompts import build_system_prompt
from app.models import Candle


def _market(*, open_interest=None, premium=0.002):
    return {
        "symbol": "ETH_USDT",
        "last_price": 100.0,
        "funding": {},
        "contract": {},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
        "market": {
            "open_interest": open_interest,
            "premium": premium,
            "oi_change_pct_1h": 1.1,
            "oi_change_pct_4h": 3.3,
            "prev_day_px": 98.0,
        },
    }


# --- M2-03: a pullback into value must NOT read as regime breakdown ----------


def test_ema_stack_pullback_keeps_regime():
    # EMAs still stacked bullish (e20>e50>e200); price has pulled back BELOW
    # EMA50 into the value zone but is still ABOVE EMA200 (the regime line).
    last = {"ema20": 105.0, "ema50": 100.0, "ema200": 90.0}
    # old gate (last_close > e50) would flip this to "mixed" at the best entry:
    assert _ema_stack_label(last, 97.0) == "bullish"   # below e50, above e200
    assert _ema_stack_label(last, 90.5) == "bullish"   # just above e200
    # only once price loses the EMA200 does the regime read break:
    assert _ema_stack_label(last, 89.0) == "mixed"
    # symmetric for bearish
    bear = {"ema20": 90.0, "ema50": 100.0, "ema200": 110.0}
    assert _ema_stack_label(bear, 103.0) == "bearish"  # above e50, below e200
    assert _ema_stack_label(bear, 111.0) == "mixed"


# --- K2-02: BTC regime anchor in the analyze context ------------------------


def test_context_contains_btc_regime_block():
    mr = {
        "btc_daily_stack": "bearish",
        "btc_htf_stack": "bearish",
        "btc_price_vs_ema20_pct": -1.2,
    }
    ctx = build_llm_context(
        _market(), {}, Settings(include_account_in_llm=False), market_regime=mr
    )
    assert ctx["market_regime"] == mr
    # the prompt cap rule appears only when the block is present
    assert "MARKET REGIME (BTC beta)" in build_system_prompt(ctx)


def test_context_omits_btc_regime_when_absent():
    ctx = build_llm_context(_market(), {}, Settings(include_account_in_llm=False))
    assert "market_regime" not in ctx
    assert "MARKET REGIME (BTC beta)" not in build_system_prompt(ctx)


def test_is_btc_symbol_self_referential():
    assert is_btc_symbol("BTC_USDT")
    assert is_btc_symbol("btc_usdt")
    assert not is_btc_symbol("ETH_USDT")
    assert not is_btc_symbol("BTCB_USDT")  # a different asset, not BTC


class _BtcClient:
    """Fake client whose BTC klines yield a clean bullish stack."""

    def __init__(self):
        self.calls = {}

    async def klines(self, symbol, interval, limit_hint=200):
        self.calls[interval] = self.calls.get(interval, 0) + 1
        # rising closes -> ema20>ema50>ema200, price above all -> bullish
        return [
            Candle(
                time=(1_700_000_000 + i * 3600) * 1000,
                open=100.0 + i, high=101.0 + i, low=99.0 + i,
                close=100.0 + i, vol=5.0,
            )
            for i in range(260)
        ]


@pytest.mark.asyncio
async def test_fetch_btc_regime_shape_and_cache():
    clear_btc_regime_cache()
    clear_daily_cache()  # underlying candle cache — avoid cross-test pollution
    client = _BtcClient()
    block = await fetch_btc_regime(client)
    assert set(block) == {
        "btc_daily_stack", "btc_htf_stack", "btc_price_vs_ema20_pct"
    }
    assert block["btc_daily_stack"] == "bullish"
    assert block["btc_htf_stack"] == "bullish"
    assert isinstance(block["btc_price_vs_ema20_pct"], float)
    calls_after_first = dict(client.calls)
    # second call within TTL is fully served from cache (no new upstream fetch)
    await fetch_btc_regime(client)
    assert client.calls == calls_after_first


@pytest.mark.asyncio
async def test_fetch_btc_regime_degrades_on_empty():
    clear_btc_regime_cache()
    clear_daily_cache()  # ensure the empty client isn't served stale candles

    class _Empty:
        async def klines(self, symbol, interval, limit_hint=200):
            return []

    assert await fetch_btc_regime(_Empty()) is None


# --- K2-05: premium dropped, OI omitted when open_interest is None ----------


def test_context_omits_premium_and_empty_oi():
    ctx = build_llm_context(_market(open_interest=None), {}, Settings())
    # no OI present -> the whole OI/premium block collapses to {}
    assert ctx["market"] == {}
    assert "premium" not in ctx["market"]

    # OI present -> fields emitted, but premium is STILL gone (dead ballast)
    ctx2 = build_llm_context(_market(open_interest=500.0), {}, Settings())
    assert ctx2["market"]["open_interest"] == 500.0
    assert ctx2["market"]["oi_change_pct_1h"] == 1.1
    assert "premium" not in ctx2["market"]
