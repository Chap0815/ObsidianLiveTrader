"""Tests for app/orders/be_math.py — must match trade-math.js:breakEvenPrice
EXACTLY (money-adjacent).

JS reference (app/static/trade-math.js):

    function breakEvenPrice(entry, isShort, feeRt) {
      const e = Number(entry);
      if (!Number.isFinite(e) || e <= 0) return null;
      const rt = feeRt == null ? 0.0006 : Number(feeRt);
      return isShort ? e * (1 - rt) : e * (1 + rt);
    }

  long:  entry * (1 + feeRt)
  short: entry * (1 - feeRt)
  default feeRt = 0.0006 (0.06% round-trip taker fee)
  non-finite/non-positive entry -> None (JS: null)
"""
import math

from app.orders.be_math import break_even_price


def test_long_be_above_entry_short_be_below_entry():
    # Long: fees push break-even ABOVE entry (need a higher exit to cover
    # costs). Short: break-even sits BELOW entry.
    entry = 100.0
    long_be = break_even_price(entry, is_short=False, fee_rt=0.0006)
    short_be = break_even_price(entry, is_short=True, fee_rt=0.0006)
    assert long_be > entry
    assert short_be < entry


def test_zero_fee_rt_returns_entry_unchanged():
    entry = 234.567
    assert break_even_price(entry, is_short=False, fee_rt=0.0) == entry
    assert break_even_price(entry, is_short=True, fee_rt=0.0) == entry


def test_reference_values_match_hand_computed_js_formula():
    # entry * (1 + feeRt) / entry * (1 - feeRt), default feeRt=0.0006
    entry = 50000.0
    fee_rt = 0.0006
    expected_long = entry * (1 + fee_rt)   # 50030.0
    expected_short = entry * (1 - fee_rt)  # 49970.0

    assert math.isclose(
        break_even_price(entry, is_short=False), expected_long, rel_tol=1e-12
    )
    assert math.isclose(
        break_even_price(entry, is_short=True), expected_short, rel_tol=1e-12
    )

    # A second reference case with an explicit non-default fee_rt.
    entry2 = 3123.45
    fee_rt2 = 0.001
    expected_long2 = entry2 * (1 + fee_rt2)
    expected_short2 = entry2 * (1 - fee_rt2)
    assert math.isclose(
        break_even_price(entry2, is_short=False, fee_rt=fee_rt2),
        expected_long2,
        rel_tol=1e-12,
    )
    assert math.isclose(
        break_even_price(entry2, is_short=True, fee_rt=fee_rt2),
        expected_short2,
        rel_tol=1e-12,
    )


def test_non_finite_or_non_positive_entry_returns_none():
    # Mirrors JS: !Number.isFinite(e) || e <= 0 -> null
    assert break_even_price(float("nan"), is_short=False) is None
    assert break_even_price(float("inf"), is_short=False) is None
    assert break_even_price(float("-inf"), is_short=True) is None
    assert break_even_price(0.0, is_short=False) is None
    assert break_even_price(-100.0, is_short=True) is None
