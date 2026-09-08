"""Pure unit tests for technical indicators (no network)."""

import math

import pytest

from app.analysis.indicators import (
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_rvol,
    compute_vwap,
    indicator_bundle,
)
from app.models import Candle


def test_ema_period_3_simple():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    ema = compute_ema(closes, 3)
    assert ema[0] is None and ema[1] is None
    assert ema[2] == pytest.approx((1 + 2 + 3) / 3)
    # next: k=2/4=0.5 → 4*0.5 + 2*0.5 = 3.0
    assert ema[3] == pytest.approx(3.0)
    # next: 5*0.5 + 3*0.5 = 4.0
    assert ema[4] == pytest.approx(4.0)


def test_ema_short_series_all_none():
    assert compute_ema([1.0, 2.0], 3) == [None, None]


def test_rsi_constant_series():
    closes = [10.0] * 20
    rsi = compute_rsi(closes, period=14)
    # first 14 values None (indices 0..13); from index 14 onward flat → RSI 50
    assert all(v is None for v in rsi[:14])
    assert rsi[14] == pytest.approx(50.0)
    assert rsi[-1] == pytest.approx(50.0)


def test_rsi_all_up_is_100():
    closes = [float(i) for i in range(1, 20)]
    rsi = compute_rsi(closes, period=14)
    assert rsi[14] == pytest.approx(100.0)
    assert rsi[-1] == pytest.approx(100.0)


def test_macd_shape_and_seed():
    closes = [float(i) for i in range(1, 50)]
    out = compute_macd(closes)
    assert set(out.keys()) == {"macd", "signal", "hist"}
    n = len(closes)
    assert len(out["macd"]) == n
    assert len(out["signal"]) == n
    assert len(out["hist"]) == n
    # MACD line needs slow EMA (26) → first non-None at index 25
    assert all(v is None for v in out["macd"][:25])
    assert out["macd"][25] is not None
    # Signal seeds after 9 MACD values → index 25+8=33
    assert all(v is None for v in out["signal"][:33])
    assert out["signal"][33] is not None
    assert out["hist"][33] == pytest.approx(out["macd"][33] - out["signal"][33])


def test_vwap_simple():
    candles = [
        Candle(time=1, open=1, high=3, low=1, close=2, vol=10),  # typical=2
        Candle(time=2, open=2, high=4, low=2, close=3, vol=10),  # typical=3
    ]
    vwap = compute_vwap(candles)
    assert vwap[0] == pytest.approx(2.0)
    # (2*10 + 3*10) / 20 = 2.5
    assert vwap[1] == pytest.approx(2.5)


def test_vwap_zero_vol_is_none():
    candles = [Candle(time=1, open=1, high=1, low=1, close=1, vol=0)]
    assert compute_vwap(candles) == [None]


def test_atr_wilder_basic():
    from app.analysis.indicators import compute_atr

    # Constant true range of 2.0 → ATR stays 2.0 after seeding
    candles = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=1) for i in range(20)
    ]
    atr = compute_atr(candles, period=14)
    assert atr[13] is None
    assert atr[14] == pytest.approx(2.0)
    assert atr[-1] == pytest.approx(2.0)


def test_atr_too_short_series():
    from app.analysis.indicators import compute_atr

    candles = [Candle(time=i, open=1, high=2, low=1, close=1.5, vol=1) for i in range(5)]
    assert compute_atr(candles, period=14) == [None] * 5


def test_rvol_constant_volume_is_one():
    candles = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=5.0) for i in range(25)
    ]
    rvol, vol_trend = compute_rvol(candles)
    assert rvol == pytest.approx(1.0)
    assert vol_trend == "flat"


def test_rvol_spike_is_above_one():
    # Adapted for L-05: the denominator is now the mean of the 20 PRIOR bars
    # only (excludes the current/breakout bar), so we need period+1 = 21
    # candles total. Prior 20 bars at vol=5.0 → avg=5.0; current bar=50.0
    # → rvol = 50/5 = 10.0 (previously this pinned the old inclusive
    # semantics: (19*5 + 50)/20 = 7.25 → ~6.9x, which understated the spike
    # because the breakout bar inflated its own average).
    candles = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=5.0) for i in range(20)
    ] + [Candle(time=20, open=10, high=11, low=9, close=10, vol=50.0)]
    rvol, vol_trend = compute_rvol(candles)
    assert rvol == pytest.approx(10.0)
    assert vol_trend == "rising"


def test_rvol_short_series_fallback_neutral():
    candles = [Candle(time=i, open=1, high=2, low=1, close=1.5, vol=3.0) for i in range(5)]
    rvol, vol_trend = compute_rvol(candles)
    assert rvol == pytest.approx(1.0)
    assert vol_trend == "flat"


def test_rvol_excludes_current_bar():
    # 20 prior bars at vol=5.0 (period=20), current (breakout) bar at 3x =
    # 15.0. Denominator must be the mean of the 20 PRIOR bars only (5.0),
    # not including the breakout bar itself, so rvol == 15/5 == 3.0 exactly.
    candles = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=5.0) for i in range(20)
    ] + [Candle(time=20, open=10, high=11, low=9, close=10, vol=15.0)]
    rvol, _ = compute_rvol(candles)
    assert rvol == pytest.approx(3.0)


def test_rvol_short_history_fallback():
    # n == period (20 candles) is NOT enough anymore: the denominator needs
    # `period` bars EXCLUDING the current one, i.e. period+1 candles total.
    # With exactly `period` candles there's no valid prior window → fallback.
    candles = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=5.0) for i in range(20)
    ]
    rvol, vol_trend = compute_rvol(candles)
    assert rvol == pytest.approx(1.0)
    assert vol_trend == "flat"


def test_indicator_bundle_never_emits_nonfinite_derived_values():
    candles = [
        Candle(
            time=i,
            open=value,
            high=value,
            low=value,
            close=value,
            vol=value,
        )
        for i in range(61)
        for value in [1e308 if i % 2 else 5e-324]
    ]

    bundle = indicator_bundle(candles)

    def values(node):
        if isinstance(node, dict):
            for child in node.values():
                yield from values(child)
        elif isinstance(node, list):
            for child in node:
                yield from values(child)
        else:
            yield node

    assert all(
        not isinstance(value, float) or math.isfinite(value)
        for value in values(bundle)
    )


# --- Task 16: intra-candle fix — bundle computes on CLOSED bars only ---------


def test_rvol_uses_closed_bar_numerator():
    """indicator_bundle drops the still-forming live bar before rvol, so the
    numerator is the last CLOSED bar's volume, not the half-built live bar
    (M2-01). A live bar that has only accumulated a tiny volume so far must NOT
    drag rvol down and make a real breakout read as 'unconfirmed'."""
    # 20 prior closed bars vol=5.0, one closed breakout bar vol=50.0, then a
    # live (forming) bar that has only booked vol=1.0 so far.
    closed = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=5.0) for i in range(20)
    ] + [Candle(time=20, open=10, high=11, low=9, close=10, vol=50.0)]
    live = Candle(time=21, open=10, high=11, low=9, close=10, vol=1.0)

    bundle = indicator_bundle(closed + [live])

    # rvol must equal the closed-bar computation: 50 / mean(prior 20 = 5) = 10.
    expected_rvol, _ = compute_rvol(closed)
    assert expected_rvol == pytest.approx(10.0)
    assert bundle["rvol"] == pytest.approx(expected_rvol)
    assert bundle["live_bar_dropped"] is True
    assert bundle["as_of_close"] == pytest.approx(10.0)
    assert bundle["as_of_time"] == 20


def test_atr_ignores_live_bar():
    """The live bar's High/Low must not repaint ATR / SL geometry (M2-02).
    A closed series with constant true-range 2.0 yields ATR 2.0; appending a
    wild still-forming bar (huge range) must leave the bundle's atr14 at 2.0."""
    from app.analysis.indicators import compute_atr

    closed = [
        Candle(time=i, open=10, high=11, low=9, close=10, vol=1) for i in range(20)
    ]
    # Forming bar with an enormous intra-candle range — would blow ATR up if used.
    live = Candle(time=20, open=10, high=99, low=1, close=50, vol=1)

    bundle = indicator_bundle(closed + [live])

    expected_atr = compute_atr(closed, 14)[-1]
    assert expected_atr == pytest.approx(2.0)
    assert bundle["last"]["atr14"] == pytest.approx(expected_atr)
