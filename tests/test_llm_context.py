"""Task 17: regime-label decoupling (M2-03), BTC regime anchor (K2-02),
token-ballast removal (K2-05)."""

from types import SimpleNamespace

import pytest

from app.analysis import context as ctxmod
from app.analysis.context import (
    clear_btc_regime_cache,
    clear_daily_cache,
    fetch_btc_regime,
    is_btc_symbol,
)
from app.config import Settings
from app.journal.stats import build_stats_response, build_track_record
from app.llm.client import (
    _ema_stack_label,
    build_llm_context,
    build_original_thesis,
)
from app.llm.prompts import build_reevaluate_system_prompt, build_system_prompt
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

    async def klines(self, symbol, interval, limit_hint=200, *, paced=False):
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
async def test_fetch_btc_regime_cache_ttl_uses_monotonic_time(monkeypatch):
    clear_btc_regime_cache()
    clear_daily_cache()
    client = _BtcClient()
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

    await fetch_btc_regime(client)
    calls_after_first = dict(client.calls)
    wall[0] = 100.0
    elapsed[0] += ctxmod._BTC_REGIME_TTL_S + 1.0
    await fetch_btc_regime(client)

    assert all(client.calls[key] > count for key, count in calls_after_first.items())


@pytest.mark.asyncio
async def test_fetch_btc_regime_degrades_on_empty():
    clear_btc_regime_cache()
    clear_daily_cache()  # ensure the empty client isn't served stale candles

    class _Empty:
        async def klines(self, symbol, interval, limit_hint=200, *, paced=False):
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


# --- Task 21 K2-01/F2-01: track_record calibration hint in the analyze ctx ----


def _raw_stats(wins=15, losses=10):
    """A journal_stats()-shaped raw dict with resolved groups big enough to pass
    the sample gate; overall net denominator populated."""
    grp = {"wins": 14, "losses": 8, "sum_r": 6.0}  # 22 resolved
    return {
        "total": 60,
        "stay_out": 5,
        "wins": wins,
        "losses": losses,
        "overall_sum_r": 5.0,
        "overall_sum_r_net": 3.0,
        "overall_net_sample": wins + losses,
        "by_confidence": {"high": dict(grp)},
        "by_setup": {"pullback": dict(grp)},
    }


def test_track_record_block_present_when_sample_ok():
    stats = build_stats_response(_raw_stats(), min_sample=20)
    tr = build_track_record(stats, min_sample=20)
    assert tr is not None
    # overall carries n + net expectancy + the HONEST Wilson LOWER bound
    assert tr["overall"]["n"] == 25
    assert tr["overall"]["net_expectancy_r"] is not None
    lo = tr["overall"]["win_rate_lo"]
    # never the point estimate (15/25 = 0.6): the lower bound is strictly below
    assert lo is not None and 0.0 <= lo < 0.6
    # per-group breakdowns present (each >= min_sample) with n + lower bound
    assert "high" in tr["by_confidence"]
    assert tr["by_confidence"]["high"]["n"] == 22
    assert tr["by_confidence"]["high"]["win_rate_lo"] is not None
    assert "pullback" in tr["by_setup"]
    # it reaches the analyze prompt, framed as a CALIBRATION HINT (not a veto)
    ctx = build_llm_context(
        _market(), {}, Settings(include_account_in_llm=False), track_record=tr
    )
    assert ctx["track_record"] == tr
    prompt = build_system_prompt(ctx)
    assert "TRACK RECORD (calibration hint)" in prompt


def test_track_record_absent_below_min_sample():
    stats = build_stats_response(_raw_stats(wins=6, losses=4), min_sample=20)
    # overall resolved sample is 10 < 20 -> no block at all (never noise as edge)
    assert build_track_record(stats, min_sample=20) is None
    # and with no track_record supplied the prompt has no calibration section
    ctx = build_llm_context(_market(), {}, Settings(include_account_in_llm=False))
    assert "track_record" not in ctx
    assert "TRACK RECORD (calibration hint)" not in build_system_prompt(ctx)


# --- R2-04: aggregate remaining risk budget when a position exists ------------


def _pos(**kw):
    base = {
        "symbol": "ETH_USDT",
        "side": "long",
        "hold_vol": 100.0,
        "entry_price": 100.0,
        "stop_loss": 95.0,  # 5-USDT loss/contract -> known same-side risk
        "liquidate_price": 50.0,
    }
    base.update(kw)
    return base


def test_context_has_remaining_budget_with_position():
    """R2-04: an OPEN same-side position on the symbol surfaces
    remaining_risk_budget_pct = MAX_RISK_PCT - used, so an add-on suggestion
    stays inside the aggregate G3 budget."""
    settings = Settings(include_account_in_llm=True, max_risk_pct=5.0)
    account = {
        "equity_usdt": 10_000.0,
        "available_usdt": 5_000.0,
        # loss-to-own-SL = |100-95| * contractSize(1) * 100 = 500 USDT = 5% of
        # 10k equity; but market contractSize is {} here -> 1.0 default.
        "positions": [_pos(hold_vol=40.0)],  # |5|*1*40 = 200 USDT = 2% used
    }
    mk = _market()
    mk["contract"] = {"contractSize": 1.0, "maxLeverage": 25}
    ctx = build_llm_context(mk, account, settings)
    assert "remaining_risk_budget_pct" in ctx
    # 5% cap - 2% used = ~3% remaining.
    assert ctx["remaining_risk_budget_pct"] == pytest.approx(3.0, abs=1e-6)
    # and the per-coin cap is exposed for the leverage clamp/context (R2-02)
    assert ctx["contract"]["max_leverage"] == 25


def test_context_no_remaining_budget_without_position():
    """No open position -> field omitted (no wasted tokens, nothing to bound)."""
    settings = Settings(include_account_in_llm=True, max_risk_pct=5.0)
    account = {"equity_usdt": 10_000.0, "available_usdt": 5_000.0, "positions": []}
    ctx = build_llm_context(_market(), account, settings)
    assert "remaining_risk_budget_pct" not in ctx


def test_context_omits_remaining_budget_when_one_side_risk_is_unknown():
    settings = Settings(include_account_in_llm=True, max_risk_pct=5.0)
    account = {
        "equity_usdt": 10_000.0,
        "available_usdt": 5_000.0,
        "positions": [
            _pos(side="long", stop_loss=None, liquidate_price=None),
            _pos(side="short", hold_vol=20.0),
        ],
    }
    mk = _market()
    mk["contract"] = {"contractSize": 1.0, "maxLeverage": 25}

    ctx = build_llm_context(mk, account, settings)

    assert "remaining_risk_budget_pct" not in ctx


def test_context_remaining_budget_omitted_when_account_private():
    """Privacy: with INCLUDE_ACCOUNT_IN_LLM=false the positions never reach the
    context, so the budget field is never computed/leaked."""
    settings = Settings(include_account_in_llm=False, max_risk_pct=5.0)
    account = {"equity_usdt": 10_000.0, "positions": [_pos()]}
    ctx = build_llm_context(_market(), account, settings)
    assert "remaining_risk_budget_pct" not in ctx


# --- Task 21 O2-06: ORIGINAL thesis anchor in the reevaluate context ----------


def test_reevaluate_context_contains_original_thesis():
    proposal = {
        "action": "BUY",
        "setup_confidence": "medium",
        "chart_pattern": "Bull Flag",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "tp1": 110.0,
        "rationale": "1H uptrend, pullback into EMA20 at prior support.",
        "conviction_score": 5,
    }
    thesis = build_original_thesis(proposal)
    assert thesis is not None
    assert thesis["chart_pattern"] == "Bull Flag"
    assert thesis["entry_price"] == 100.0
    assert thesis["stop_loss"] == 95.0
    assert thesis["tp1"] == 110.0
    assert thesis["setup_confidence"] == "medium"
    assert thesis["rationale"]
    # a STAY_OUT / level-less proposal never opened a position -> no anchor
    assert build_original_thesis({"action": "STAY_OUT"}) is None
    assert build_original_thesis(None) is None
    # the reevaluate prompt gains the consistency-anchor instruction ONLY when
    # the block is present (the base prompt must not already carry it)
    with_thesis = build_reevaluate_system_prompt({"original_thesis": thesis})
    assert "ORIGINAL THESIS (consistency anchor)" in with_thesis
    assert "state explicitly whether it still holds" in with_thesis
    assert "ORIGINAL THESIS (consistency anchor)" not in build_reevaluate_system_prompt()
