"""Task 3: pure resolve_entry geometry (long/short WIN/LOSS/EXPIRED, both-hit, degenerate)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.journal.resolver import (
    EXPIRED,
    LOSS,
    NO_FILL,
    PENDING,
    SKIPPED,
    WIN,
    resolution_window_s,
    resolve_entry,
)

T0 = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
T0_ISO = T0.isoformat()
T0_MS = int(T0.timestamp() * 1000)
NOW_FAR = T0 + timedelta(hours=48)
WINDOW = 24 * 3600


def _candle(offset_min, high, low):
    return {"time": T0_MS + offset_min * 60_000, "high": high, "low": low, "open": low, "close": high}


def _covered(*candles):
    """History reaching t0 without triggering either terminal level."""
    return [_candle(0, 100.0, 100.0), *candles]


def _long(**kw):
    base = dict(
        direction="long", entry_price=100.0, stop_loss=99.0, tp1=102.0,
        created_at=T0_ISO, now=NOW_FAR, window_s=WINDOW,
    )
    base.update(kw)
    return resolve_entry(**base)


def _short(**kw):
    base = dict(
        direction="short", entry_price=100.0, stop_loss=101.0, tp1=98.0,
        created_at=T0_ISO, now=NOW_FAR, window_s=WINDOW,
    )
    base.update(kw)
    return resolve_entry(**base)


def test_long_win():
    o = _long(candles=_covered(_candle(5, 100.5, 100.0), _candle(10, 102.5, 101.0)))
    assert o.status == WIN
    assert o.resolved_price == 102.0
    assert o.realized_r == pytest.approx(2.0)  # |102-100|/|100-99|
    assert o.ambiguous is False


def test_long_loss():
    o = _long(candles=_covered(_candle(5, 100.2, 98.5)))
    assert o.status == LOSS
    assert o.resolved_price == 99.0
    assert o.realized_r == -1.0


def test_short_win():
    o = _short(candles=_covered(_candle(5, 100.0, 97.5)))
    assert o.status == WIN
    assert o.resolved_price == 98.0
    assert o.realized_r == pytest.approx(2.0)  # |98-100|/|100-101|


def test_short_loss():
    o = _short(candles=_covered(_candle(5, 101.5, 99.0)))
    assert o.status == LOSS
    assert o.resolved_price == 101.0
    assert o.realized_r == -1.0


def test_both_hit_same_candle_is_loss_ambiguous():
    # long: candle spans both tp1 (102) and sl (99)
    o = _long(candles=_covered(_candle(5, 103.0, 98.0)))
    assert o.status == LOSS
    assert o.ambiguous is True
    assert o.resolved_price == 99.0
    assert o.realized_r == -1.0


def test_first_terminal_candle_wins():
    # A losing candle first, then a winning one: LOSS must win.
    o = _long(candles=_covered(_candle(5, 100.1, 98.9), _candle(10, 103.0, 101.0)))
    assert o.status == LOSS


def test_neither_within_window_expired():
    # Coverage reaches t0 (candle at offset 0) -> a real EXPIRED is safe to report.
    o = _long(candles=[_candle(0, 100.5, 99.5), _candle(5, 100.5, 99.5)])  # never touches
    assert o.status == EXPIRED
    assert o.realized_r is None


def test_neither_but_window_open_stays_pending():
    o = _long(candles=[_candle(5, 100.5, 99.5)], now=T0 + timedelta(hours=1))
    assert o.status == PENDING


def test_incomplete_coverage_stays_pending_not_expired():
    # Candle set starts well AFTER t0 (e.g. resolver was down / fetch window too
    # short to reach back to created_at): even though window_s has elapsed and
    # no touch is seen in what we DID fetch, we must not fabricate EXPIRED --
    # a real WIN/LOSS could have happened before our earliest fetched candle.
    o = _long(candles=[_candle(120, 100.5, 99.5)])  # first candle 2h after t0
    assert o.status == PENDING


def test_incomplete_coverage_stays_pending_even_with_empty_candles():
    # No candle data at all (e.g. delisted symbol / empty payload): never
    # fabricate a terminal outcome, regardless of how much time has passed.
    o = _long(candles=[])
    assert o.status == PENDING


def test_terminal_touch_after_resolution_deadline_is_ignored():
    o = _long(candles=_covered(_candle(24 * 60 + 5, 103.0, 100.0)))
    assert o.status == EXPIRED


def test_later_terminal_touch_without_t0_coverage_stays_pending():
    o = _long(candles=[_candle(120, 103.0, 100.0)])
    assert o.status == PENDING


def test_full_coverage_expired_after_window():
    # Coverage reaches back to (before) t0 explicitly -> EXPIRED as before.
    pre = _candle(0, 100.5, 99.5)
    o = _long(candles=[pre, _candle(30, 100.2, 99.8)])
    assert o.status == EXPIRED


def test_candle_before_t0_ignored():
    # A pre-t0 candle would have hit tp1, but it's before entry -> ignored.
    pre = {"time": T0_MS - 60_000, "high": 103.0, "low": 101.0}
    o = _long(candles=[pre], now=T0 + timedelta(hours=1))
    assert o.status == PENDING


def test_degenerate_long_geometry_skipped():
    o = _long(tp1=99.5, candles=[_candle(5, 103.0, 90.0)])  # tp1 <= entry
    assert o.status == SKIPPED


def test_degenerate_short_geometry_skipped():
    o = _short(stop_loss=99.0, candles=[_candle(5, 103.0, 90.0)])  # sl <= entry
    assert o.status == SKIPPED


def test_missing_levels_skipped():
    o = _long(tp1=None, candles=[_candle(5, 103.0, 101.0)])
    assert o.status == SKIPPED


@pytest.mark.parametrize(
    ("entry", "stop", "tp1"),
    [
        (float("nan"), 99.0, 102.0),
        (float("inf"), 99.0, 102.0),
        (100.0, float("-inf"), 102.0),
        (100.0, 99.0, float("inf")),
        (True, 0.0, 2.0),
        (100.0, 0.0, 102.0),
        (-100.0, -101.0, -98.0),
    ],
)
def test_invalid_price_levels_are_skipped(entry, stop, tp1):
    outcome = resolve_entry(
        direction="long",
        entry_price=entry,
        stop_loss=stop,
        tp1=tp1,
        created_at=T0_ISO,
        candles=_covered(_candle(5, 103.0, 98.0)),
        now=NOW_FAR,
        window_s=WINDOW,
    )
    assert outcome.status == SKIPPED
    assert outcome.realized_r is None
    assert outcome.realized_r_net is None


# ── Task 19 / F2-02: entry-touch (NO_FILL) ───────────────────────────────────
def test_untouched_entry_is_no_fill():
    # Price gaps ABOVE the limit entry (100) and never trades back down to it,
    # so the limit never fills -- even though it later trades through tp1 (102).
    # Old resolver miscounted this as a WIN; it must now be NO_FILL (excluded
    # from win/loss), with no fictional R.
    o = _long(candles=[_candle(0, 105.0, 103.0), _candle(30, 106.0, 104.0)])
    assert o.status == NO_FILL
    assert o.realized_r is None
    assert o.realized_r_net is None


# ── Task 19 / F2-06: tf-scaled resolution window ─────────────────────────────
def test_window_scales_with_tf():
    base = 24 * 3600  # 24h floor
    # Short tf: 96 * 300s = 8h < 24h -> floored at 24h.
    assert resolution_window_s("5m", base) == base
    # 15m: 96 * 900s = 24h exactly -> equals the floor (loop tests rely on this).
    assert resolution_window_s("15m", base) == base
    # Higher tf must scale UP so slow winners aren't cut off (EXPIRED bias).
    assert resolution_window_s("4H", base) == 96 * 14_400
    assert resolution_window_s("1D", base) == 96 * 86_400
    assert resolution_window_s("1D", base) > base
    # Unknown tf falls back to the base window, never 0.
    assert resolution_window_s("bogus", base) == base


# ── Task 19 / F2-07: net R subtracts round-trip costs ────────────────────────
def test_realized_r_net_subtracts_costs():
    # long entry 100, sl 99 (risk 1.0), tp1 102 (gross +2R). One candle spans
    # entry and tp1 (fills, then wins). cost_frac = 2*taker + slip = 0.003;
    # in R = 0.003 * entry/risk = 0.003 * 100/1 = 0.3 -> net = 2.0 - 0.3 = 1.7.
    o = _long(
        candles=_covered(_candle(5, 102.5, 99.5)),
        taker_fee=0.001,
        slippage_frac=0.001,
    )
    assert o.status == WIN
    assert o.realized_r == pytest.approx(2.0)
    assert o.realized_r_net == pytest.approx(1.7)
    assert o.realized_r_net < o.realized_r
