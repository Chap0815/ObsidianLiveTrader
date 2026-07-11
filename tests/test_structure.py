"""Pure unit tests for swing/structure detection (no network)."""

from app.analysis.structure import find_swings, key_levels
from app.models import Candle


def _c(i: int, high: float, low: float, close: float | None = None) -> Candle:
    mid = close if close is not None else (high + low) / 2
    return Candle(
        time=i * 60_000,
        open=mid,
        high=high,
        low=low,
        close=mid,
        vol=1.0,
    )


def test_find_swings_clear_peak_and_trough():
    # left=1, right=1: bar 1 is strict local high and low
    candles = [
        _c(0, high=2.0, low=1.5, close=1.8),
        _c(1, high=5.0, low=0.5, close=3.0),  # swing high 5, swing low 0.5
        _c(2, high=2.0, low=1.5, close=1.8),
    ]
    swings = find_swings(candles, left=1, right=1)
    assert len(swings.highs) == 1
    assert swings.highs[0].index == 1
    assert swings.highs[0].price == 5.0
    assert swings.highs[0].kind == "high"
    assert len(swings.lows) == 1
    assert swings.lows[0].index == 1
    assert swings.lows[0].price == 0.5
    assert swings.lows[0].kind == "low"


def test_find_swings_left_right_2():
    # Peak at index 2: highs [1,2,5,2,1]
    candles = [
        _c(0, high=1, low=0.5, close=0.8),
        _c(1, high=2, low=0.5, close=1.5),
        _c(2, high=5, low=0.5, close=3.0),
        _c(3, high=2, low=0.5, close=1.5),
        _c(4, high=1, low=0.5, close=0.8),
    ]
    swings = find_swings(candles, left=2, right=2)
    assert [s.index for s in swings.highs] == [2]
    assert swings.highs[0].price == 5.0


def test_key_levels_support_resistance():
    # Rising then fall: swing low early, swing high mid, last price in between
    candles = [
        _c(0, high=10, low=9, close=9.5),
        _c(1, high=11, low=8, close=9.0),   # potential swing low at 8 (left1 right1 needs neighbors)
        _c(2, high=12, low=10, close=11),
        _c(3, high=15, low=11, close=14),   # swing high 15 with left=1 right=1 vs 12 and 13
        _c(4, high=13, low=10, close=12),   # last close 12
    ]
    # Use left=right=1 for this short series
    levels = key_levels(candles, left=1, right=1)
    assert levels.last_price == 12.0
    assert levels.range_high == 15.0
    assert levels.range_low == 8.0
    # support: last swing low below 12
    assert levels.support is not None
    assert levels.support < 12.0
    # resistance: last swing high above 12
    assert levels.resistance is not None
    assert levels.resistance > 12.0
    assert len(levels.major_pools) <= 3


def test_key_levels_empty():
    levels = key_levels([])
    assert levels.support is None
    assert levels.resistance is None
    assert levels.major_pools == []


def test_major_pools_last_three_swings():
    # Multiple clear swings with left=right=1
    # lows at 1, highs at 3, lows at 5, highs at 7
    candles = [
        _c(0, high=3, low=2, close=2.5),
        _c(1, high=2.5, low=1, close=1.5),  # swing low 1
        _c(2, high=3, low=2, close=2.5),
        _c(3, high=6, low=3, close=5),  # swing high 6
        _c(4, high=4, low=3, close=3.5),
        _c(5, high=3.5, low=1.5, close=2),  # swing low 1.5
        _c(6, high=4, low=2.5, close=3.5),
        _c(7, high=7, low=4, close=6),  # swing high 7
        _c(8, high=5, low=4, close=4.5),  # last
    ]
    levels = key_levels(candles, left=1, right=1)
    idxs = [p.index for p in levels.major_pools]
    assert len(idxs) == 3
    assert idxs == sorted(idxs)
    # last three swings among detected set (indices 1,3,5,7 → 3,5,7)
    assert idxs == [3, 5, 7]
