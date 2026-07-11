"""Price structure: swing highs/lows and key support/resistance levels."""

from __future__ import annotations

from app.models import Candle, KeyLevels, SwingPoint, SwingPoints


def find_swings(
    candles: list[Candle], left: int = 2, right: int = 2
) -> SwingPoints:
    """Detect swing highs/lows with a left/right confirmation window.

    Swing high at i if high[i] is strictly greater than all highs in
    [i-left, i) and (i, i+right]. Same (strictly lower) for swing lows.
    """
    n = len(candles)
    highs: list[SwingPoint] = []
    lows: list[SwingPoint] = []
    if n == 0 or left < 0 or right < 0:
        return SwingPoints(highs=highs, lows=lows)

    for i in range(left, n - right):
        h = candles[i].high
        l = candles[i].low
        is_high = True
        is_low = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if candles[j].high >= h:
                is_high = False
            if candles[j].low <= l:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            highs.append(
                SwingPoint(index=i, time=candles[i].time, price=h, kind="high")
            )
        if is_low:
            lows.append(
                SwingPoint(index=i, time=candles[i].time, price=l, kind="low")
            )
    return SwingPoints(highs=highs, lows=lows)


def key_levels(candles: list[Candle], left: int = 2, right: int = 2) -> KeyLevels:
    """Nearest support/resistance from swings + range; last 3 swings as pools."""
    if not candles:
        return KeyLevels(
            support=None,
            resistance=None,
            range_high=None,
            range_low=None,
            last_price=None,
            major_pools=[],
        )

    last_price = candles[-1].close
    range_high = max(c.high for c in candles)
    range_low = min(c.low for c in candles)
    swings = find_swings(candles, left=left, right=right)

    # Nearest support: most recent swing low strictly below last price
    support: float | None = None
    for sp in reversed(swings.lows):
        if sp.price < last_price:
            support = sp.price
            break
    if support is None and range_low < last_price:
        support = range_low

    # Nearest resistance: most recent swing high strictly above last price
    resistance: float | None = None
    for sp in reversed(swings.highs):
        if sp.price > last_price:
            resistance = sp.price
            break
    if resistance is None and range_high > last_price:
        resistance = range_high

    # Major pools: last 3 swings chronologically (highs + lows merged by index)
    merged = sorted(
        list(swings.highs) + list(swings.lows),
        key=lambda s: s.index,
    )
    major = merged[-3:] if merged else []

    return KeyLevels(
        support=support,
        resistance=resistance,
        range_high=range_high,
        range_low=range_low,
        last_price=last_price,
        major_pools=major,
        swings=swings,
    )
