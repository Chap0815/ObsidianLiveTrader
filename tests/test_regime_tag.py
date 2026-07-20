"""Block 2/TP2 Task P1: regime_tag() -- pure, deterministic, fail-safe helper
that tags a proposal with a compact '<btcTrend>/<vol>' string for later
journal segmentation. Advisory only -- never touches a gate/sizing/decision.
"""

from __future__ import annotations

import pytest

from app.analysis.context import regime_tag


@pytest.mark.parametrize(
    "daily_stack, atr_pct, expected",
    [
        ("bullish", 0.5, "btcUp/volLow"),
        ("bullish", 0.99, "btcUp/volLow"),
        ("bullish", 1.0, "btcUp/volNormal"),  # boundary: low is exclusive at 1.0
        ("bullish", 2.5, "btcUp/volNormal"),
        ("bearish", 3.0, "btcDown/volHigh"),  # boundary: high is inclusive at 3.0
        ("bearish", 10.0, "btcDown/volHigh"),
        ("mixed", 1.5, "btcChop/volNormal"),
    ],
)
def test_regime_tag_deterministic_buckets(daily_stack, atr_pct, expected):
    btc_regime = {"btc_daily_stack": daily_stack}
    assert regime_tag(btc_regime, atr_pct) == expected
    # PURE + deterministic: same input -> same output, every time.
    assert regime_tag(btc_regime, atr_pct) == regime_tag(btc_regime, atr_pct)


@pytest.mark.parametrize(
    "btc_regime, atr_pct",
    [
        (None, 1.0),                              # BTC regime fetch failed/omitted
        ({}, 1.0),                                  # empty dict (no daily_stack key)
        ({"btc_daily_stack": "unknown"}, 1.0),       # _ema_stack_label's own "unknown"
        ({"btc_daily_stack": "bogus"}, 1.0),         # unrecognized value
        ({"btc_daily_stack": "bullish"}, None),      # atr_pct unavailable
        ({"btc_daily_stack": "bullish"}, "oops"),    # wrong type
        ({"btc_daily_stack": "bullish"}, -1.0),      # nonsensical negative ATR%
        ({"btc_daily_stack": "bullish"}, float("nan")),  # NaN guard
        ("not-a-dict", 1.0),                          # wrong type entirely
    ],
)
def test_regime_tag_unknown_on_missing_or_invalid_signals(btc_regime, atr_pct):
    # Fail-safe: any missing/invalid signal collapses to "unknown", never a crash.
    assert regime_tag(btc_regime, atr_pct) == "unknown"
