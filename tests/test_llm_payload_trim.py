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
from app.llm.client import build_llm_context, compact_tf_for_llm


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


def test_build_llm_context_drops_contract_block():
    ctx = build_llm_context(_market_api(), {}, Settings(include_account_in_llm=False))
    assert "contract" not in ctx


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
    for tf in ("htf", "ltf"):
        for c in ctx[tf]["recent_candles"]:
            assert "amount" not in c
