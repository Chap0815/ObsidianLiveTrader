"""O1: trim the deep-analyze LLM payload of fields the prompt never reads.

- `amount` (quote turnover) dropped from every recent_candles entry;
  OHLC + vol are kept (the prompt reads those / read.* / indicators_tail).
- the whole `contract` block dropped from the LLM context (prompt uses
  risk_policy.max_leverage, never contract.*).
- `funding` trimmed to the three fields the prompt references
  (fundingRate, fundingAnnualized, fundingExtreme); the rest removed.

None of this changes analysis quality — only fewer wasted input tokens.
"""

from app.config import Settings
from app.llm.client import (
    build_llm_context,
    compact_daily_for_llm,
    compact_tf_for_llm,
)


def _slice_with_candles():
    candles = [
        {
            "time": 1_700_000_000_000 + i * 900_000,
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "vol": 5.0 + i,
            "amount": 123456.0 + i,  # quote turnover — never read by the prompt
        }
        for i in range(35)
    ]
    return {
        "tf": "15m",
        "candles": candles,
        "indicators": {"last": {"ema20": 100.0, "ema50": 99.0, "ema200": 98.0, "atr14": 1.0}},
        "structure": {},
    }


def test_compact_tf_drops_amount_but_keeps_ohlcv():
    compact = compact_tf_for_llm(_slice_with_candles())
    rc = compact["recent_candles"]
    assert rc, "recent_candles should be populated"
    for c in rc:
        assert "amount" not in c  # dropped
        for k in ("time", "open", "high", "low", "close", "vol"):
            assert k in c  # OHLCV preserved


def test_compact_reads_omit_overflowed_relative_percentages():
    slice_dict = {
        "tf": "15m",
        "candles": [{"close": 1e308}],
        "indicators": {
            "as_of_close": 1e308,
            "last": {
                "ema20": 5e-324,
                "ema50": 5e-324,
                "ema200": 5e-324,
                "vwap": 5e-324,
            },
        },
        "structure": {},
    }

    tf_read = compact_tf_for_llm(slice_dict)["read"]
    daily_read = compact_daily_for_llm(slice_dict)["read"]

    assert "price_vs_ema20_pct" not in tf_read
    assert "price_vs_vwap_pct" not in tf_read
    assert "price_vs_ema20_pct" not in daily_read


def test_compact_reads_sanitize_scalar_indicator_values():
    slice_dict = {
        "tf": "15m",
        "candles": [{"close": 100.0}],
        "indicators": {
            "as_of_close": 100.0,
            "last": {
                "ema20": 99.0,
                "ema50": 98.0,
                "ema200": 97.0,
                "atr14": float("inf"),
            },
            "rvol": True,
            "vol_trend": "sideways",
        },
        "structure": {},
    }

    tf_read = compact_tf_for_llm(slice_dict)["read"]
    daily_read = compact_daily_for_llm(slice_dict)["read"]

    assert tf_read["atr14"] is None
    assert tf_read["rvol"] is None
    assert tf_read["vol_trend"] is None
    assert daily_read["atr14"] is None
    assert daily_read["rvol"] is None

    slice_dict["indicators"]["last"]["atr14"] = 2.5
    slice_dict["indicators"]["rvol"] = 0.0
    slice_dict["indicators"]["vol_trend"] = "flat"
    valid_read = compact_tf_for_llm(slice_dict)["read"]
    assert valid_read["atr14"] == 2.5
    assert valid_read["rvol"] == 0.0
    assert valid_read["vol_trend"] == "flat"


def _market_api():
    slc = _slice_with_candles()
    daily = {"tf": "1D", "candles": slc["candles"], "indicators": slc["indicators"], "structure": {}}
    return {
        "symbol": "BTC",
        "last_price": 100.5,
        "funding": {
            "symbol": "BTC",
            "fundingRate": 0.0002,
            "maxFundingRate": 0.01,
            "minFundingRate": -0.01,
            "collectCycle": 8,
            "nextSettleTime": 1_700_000_000_000,
            "timestamp": 1_700_000_000_000,
            "fundingAnnualized": 1.75,
            "fundingExtreme": "crowded_long",
        },
        "contract": {
            "symbol": "BTC",
            "contractSize": 0.0001,
            "maxLeverage": 100,
            "minLeverage": 1,
            "apiAllowed": True,
        },
        "market": {"open_interest": None},
        "daily": daily,
        "htf": _slice_with_candles(),
        "ltf": _slice_with_candles(),
    }


def test_build_llm_context_keeps_only_contract_max_leverage():
    """R2-02 (revises O1): the FULL contract block is still trimmed for tokens,
    but the ONE field the model needs — the per-coin max_leverage — is now
    surfaced so it proposes within the coin cap instead of 'propose then block'
    at preview. contractSize/minLeverage/apiAllowed remain dropped."""
    ctx = build_llm_context(_market_api(), {}, Settings(include_account_in_llm=False))
    assert ctx["contract"] == {"max_leverage": 100}
    assert "contractSize" not in ctx["contract"]
    assert "minLeverage" not in ctx["contract"]
    assert "apiAllowed" not in ctx["contract"]


def test_build_llm_context_trims_funding_to_referenced_fields():
    ctx = build_llm_context(_market_api(), {}, Settings(include_account_in_llm=False))
    assert ctx["funding"] == {
        "fundingRate": 0.0002,
        "fundingAnnualized": 1.75,
        "fundingExtreme": "crowded_long",
    }


def test_build_llm_context_funding_missing_is_graceful():
    mk = _market_api()
    mk["funding"] = {}
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False))
    # No referenced keys present -> empty funding, no error.
    assert ctx["funding"] == {}


def test_build_llm_context_recent_candles_have_no_amount():
    ctx = build_llm_context(_market_api(), {}, Settings(include_account_in_llm=False))
    assert "note" not in ctx  # policy lives in the system prompt, not repeated per payload
    for tf in ("htf", "ltf"):
        for c in ctx[tf]["recent_candles"]:
            assert "amount" not in c


def test_recent_candles_keep_live_bar_with_marker():
    """Task 16 / K2-03: indicators are computed on CLOSED bars, but the raw
    recent_candles the LLM sees KEEP the still-forming live bar (so the model
    sees current price action). The compact payload carries a `bar_progress`
    marker flagging that the last recent candle is not yet closed."""
    from app.analysis.context import _candles_public
    from app.analysis.indicators import indicator_bundle
    from app.models import Candle

    candles = [
        Candle(
            time=1_700_000_000_000 + i * 900_000,
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            vol=5.0 + i,
        )
        for i in range(60)
    ]
    slice_dict = {
        "tf": "15m",
        "candles": _candles_public(candles),
        "indicators": indicator_bundle(candles),
        "structure": {},
    }

    out = compact_tf_for_llm(slice_dict)

    # The live (last) bar is retained in recent_candles unchanged.
    assert out["recent_candles"][-1]["time"] == candles[-1].time
    assert out["recent_candles"][-1]["close"] == candles[-1].close

    # Marker flags that the last bar is still forming; closed reference is the
    # PRIOR bar (indicators were computed as-of the last closed bar).
    assert out["bar_progress"]["last_bar_forming"] is True
    assert out["bar_progress"]["closed_as_of"] == candles[-2].time

    # And the read-labels are anchored to the CLOSED bar, not the live one.
    assert slice_dict["indicators"]["as_of_close"] == candles[-2].close
