"""Tests for app/orders/trade_manager.py — the PURE rule-evaluation core.

This is money-critical: ``evaluate_rules`` decides when the server autonomously
moves a real stop-loss to break-even. It must be pure (no I/O, no clock — now_ms
is passed in), never move a stop in the loosening direction, and debounce
advisory alarms via ``last_alert_state``.

Settings is duck-typed (only attribute reads), so tests use a lightweight
SimpleNamespace instead of the full pydantic Settings.
"""
from types import SimpleNamespace

import math

from app.orders.be_math import break_even_price
from app.orders.trade_manager import (
    Alert,
    MgmtBaseline,
    MoveSlToBe,
    evaluate_rules,
)


def _settings(**over):
    base = dict(
        tm_be_trigger_r=1.0,
        tm_be_fee_rt=0.0006,
        tm_time_stop_hours=4.0,
        tm_time_stop_min_r=0.5,
        tm_trail_activation_r=1.0,
        tm_trail_atr_mult=2.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgmt(**over):
    base = dict(
        entry=100.0,
        initial_sl=90.0,
        r1=10.0,
        opened_at_ms=0,
        invalidation_price=None,
        armed_rules={},
        be_done=False,
        last_alert_state={},
        high_water=None,
    )
    base.update(over)
    return MgmtBaseline(**base)


# ── unreal_r correctness ────────────────────────────────────────────────────

def test_unreal_r_long_at_one_r_fires_be():
    # entry 100, r1 10 → mark 110 == +1R. armed → BE fires.
    mgmt = _mgmt(armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    moves = [a for a in actions if isinstance(a, MoveSlToBe)]
    assert len(moves) == 1


def test_unreal_r_short_at_one_r_fires_be():
    # short entry 100, r1 10 → mark 90 == +1R (price dropped). armed → BE fires.
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=110.0, mark=90.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    moves = [a for a in actions if isinstance(a, MoveSlToBe)]
    assert len(moves) == 1
    # BE for a short sits BELOW entry.
    assert moves[0].new_sl < 100.0


# ── Auto-BE threshold ───────────────────────────────────────────────────────

def test_auto_be_does_not_fire_below_threshold():
    # mark 109 → +0.9R, below tm_be_trigger_r=1.0 → no move.
    mgmt = _mgmt(armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=109.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_auto_be_fires_exactly_at_threshold():
    mgmt = _mgmt(armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    moves = [a for a in actions if isinstance(a, MoveSlToBe)]
    assert len(moves) == 1
    expected_be = break_even_price(100.0, False, 0.0006)
    assert math.isclose(moves[0].new_sl, expected_be, rel_tol=1e-12)


# ── Protective-direction guard (the dangerous case) ─────────────────────────

def test_be_refused_when_not_more_protective_long():
    # current_sl already at 100.5, above the long BE (100.06) → moving to BE
    # would LOOSEN the stop. Must be refused.
    be = break_even_price(100.0, False, 0.0006)  # ~100.06
    mgmt = _mgmt(armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=be + 1.0, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_be_refused_when_not_more_protective_short():
    # short: BE below entry (~99.94). current_sl already BELOW that → moving to
    # BE would loosen (raise) the stop. Must be refused.
    be = break_even_price(100.0, True, 0.0006)  # ~99.94
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=be - 1.0, mark=90.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_be_allowed_when_current_sl_is_none():
    mgmt = _mgmt(armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=None, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert [a for a in actions if isinstance(a, MoveSlToBe)]


# ── be_done + arming ────────────────────────────────────────────────────────

def test_be_done_suppresses_move():
    mgmt = _mgmt(armed_rules={"auto_be": True}, be_done=True)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_unarmed_position_never_moves_sl():
    # No auto_be arming → even at +5R, no MoveSl. Alerts still allowed.
    mgmt = _mgmt(armed_rules={}, invalidation_price=85.0,
                 last_alert_state={})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=150.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


# ── Thesis alarm (both directions + debounce) ───────────────────────────────

def test_thesis_alarm_long_crossing():
    mgmt = _mgmt(invalidation_price=95.0, last_alert_state={})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=94.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    alerts = [a for a in actions if isinstance(a, Alert) and a.kind == "thesis"]
    assert len(alerts) == 1


def test_thesis_alarm_short_crossing():
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 invalidation_price=105.0, last_alert_state={})
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=110.0, mark=106.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    alerts = [a for a in actions if isinstance(a, Alert) and a.kind == "thesis"]
    assert len(alerts) == 1


def test_thesis_alarm_debounced_when_already_flagged():
    mgmt = _mgmt(invalidation_price=95.0, last_alert_state={"thesis": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=94.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, Alert) and a.kind == "thesis"]


def test_thesis_alarm_not_fired_before_crossing():
    mgmt = _mgmt(invalidation_price=95.0, last_alert_state={})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=96.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, Alert) and a.kind == "thesis"]


# ── Time-stop alarm boundaries ──────────────────────────────────────────────

_HOUR_MS = 3_600_000


def test_time_stop_just_under_hours_no_alarm():
    # opened at 0; 4h threshold. now = 4h - 1ms → not yet.
    mgmt = _mgmt(opened_at_ms=0, last_alert_state={})
    now = 4 * _HOUR_MS - 1
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=102.0,  # +0.2R < 0.5
        mgmt=mgmt, now_ms=now, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, Alert) and a.kind == "time_stop"]


def test_time_stop_at_hours_and_below_min_r_fires():
    mgmt = _mgmt(opened_at_ms=0, last_alert_state={})
    now = 4 * _HOUR_MS
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=102.0,  # +0.2R < 0.5
        mgmt=mgmt, now_ms=now, settings=_settings(),
    )
    alerts = [a for a in actions if isinstance(a, Alert) and a.kind == "time_stop"]
    assert len(alerts) == 1


def test_time_stop_not_fired_when_r_above_min():
    # Past the time threshold but +2R (>= min_r 0.5) → position is working, no alarm.
    mgmt = _mgmt(opened_at_ms=0, last_alert_state={})
    now = 5 * _HOUR_MS
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=120.0,  # +2R
        mgmt=mgmt, now_ms=now, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, Alert) and a.kind == "time_stop"]


def test_time_stop_debounced_when_already_flagged():
    mgmt = _mgmt(opened_at_ms=0, last_alert_state={"time_stop": True})
    now = 5 * _HOUR_MS
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=102.0,
        mgmt=mgmt, now_ms=now, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, Alert) and a.kind == "time_stop"]


# ── r1 <= 0 / non-finite → no R-signal, no crash ────────────────────────────

def test_r1_zero_no_r_signal_no_crash():
    # r1 = 0 → no BE, no time-stop-R gate. Thesis (R-independent) still works.
    mgmt = _mgmt(r1=0.0, armed_rules={"auto_be": True},
                 invalidation_price=95.0, last_alert_state={})
    now = 5 * _HOUR_MS
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=94.0,
        mgmt=mgmt, now_ms=now, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]
    assert not [a for a in actions if isinstance(a, Alert) and a.kind == "time_stop"]
    # thesis crossing does NOT need R → still fires.
    assert [a for a in actions if isinstance(a, Alert) and a.kind == "thesis"]


def test_r1_non_finite_no_crash():
    for bad in (float("nan"), float("inf"), -5.0):
        mgmt = _mgmt(r1=bad, armed_rules={"auto_be": True})
        actions = evaluate_rules(
            side="long", entry=100.0, current_sl=90.0, mark=110.0,
            mgmt=mgmt, now_ms=0, settings=_settings(),
        )
        assert not [a for a in actions if isinstance(a, MoveSlToBe)]


# ── Purity: input not mutated ───────────────────────────────────────────────

def test_input_mgmt_not_mutated():
    las = {}
    mgmt = _mgmt(armed_rules={"auto_be": True}, invalidation_price=95.0,
                 last_alert_state=las)
    evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=94.0,
        mgmt=mgmt, now_ms=5 * _HOUR_MS, settings=_settings(),
    )
    # last_alert_state must be untouched (caller persists, not this function).
    assert las == {}
    assert mgmt.last_alert_state == {}
    assert mgmt.be_done is False


def test_be_none_when_entry_non_positive_no_crash():
    # break_even_price returns None for entry <= 0 → no MoveSl, no crash.
    mgmt = _mgmt(entry=-1.0, r1=10.0, armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=-1.0, current_sl=None, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


# ── Review nits (2026-07-19): self-defeating BE, robustness ─────────────────

def test_be_above_mark_refused_for_tiny_r1_long():
    """Important review fix: a tight initial stop makes r1 small enough that the
    fee buffer pushes BE PAST the current price. A long stop above mark would
    trigger instantly / be rejected — evaluate_rules must refuse it."""
    # entry 100, initial_sl 99.95 → r1 0.05; mark 100.055 == +1.1R.
    # BE = 100 * 1.0006 = 100.06, which is ABOVE mark 100.055 → must be refused.
    mgmt = _mgmt(entry=100.0, initial_sl=99.95, r1=0.05,
                 armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=99.95, mark=100.055,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_be_above_mark_refused_for_tiny_r1_short():
    # short entry 100, initial_sl 100.05 → r1 0.05; mark 99.945 == +1.1R.
    # BE = 100 * 0.9994 = 99.94, which is BELOW mark 99.945 → must be refused.
    mgmt = _mgmt(entry=100.0, initial_sl=100.05, r1=0.05,
                 armed_rules={"auto_be": True})
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=100.05, mark=99.945,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_non_dict_armed_and_alert_state_no_crash():
    """A corrupt JSON column deserializing to a non-dict must not crash the
    money-path function — armed_rules/last_alert_state coerce to {}."""
    mgmt = _mgmt(armed_rules=None, last_alert_state=None,
                 invalidation_price=95.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    # armed coerced to {} → no auto-BE; no crash on the thesis/time-stop .get.
    assert not [a for a in actions if isinstance(a, MoveSlToBe)]


def test_unknown_side_yields_no_actions():
    """A mislabeled side must not be silently treated as short — do nothing."""
    mgmt = _mgmt(armed_rules={"auto_be": True}, invalidation_price=95.0)
    actions = evaluate_rules(
        side="buy", entry=100.0, current_sl=90.0, mark=110.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert actions == []


# ── Auto-Trailing (ATR / Chandelier) ────────────────────────────────────────
# Money-critical second autonomous action. Chandelier trail:
#   long:  trail = high_water - tm_trail_atr_mult * atr
#   short: trail = high_water + tm_trail_atr_mult * atr
# Emitted ONLY if it tightens the stop (_is_more_protective) AND sits on the
# correct side of mark. No be_done latch — fires repeatedly.

def _trails(actions):
    return [a for a in actions if isinstance(a, MoveSlToBe) and a.reason == "auto-trail"]


def test_trail_does_not_fire_below_activation_r():
    # activation_r=2.0; mark 119 == +1.9R → below → no trail.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=119.0,
        mgmt=mgmt, now_ms=0, settings=_settings(tm_trail_activation_r=2.0),
        atr=5.0,
    )
    assert not _trails(actions)


def test_trail_fires_at_activation_r_long():
    # activation_r=2.0; mark 120 == +2.0R. hw 125, atr 5, mult 2 → trail 115.
    # 115 > current_sl 90 (tightens) and 115 < mark 120 → fires.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(tm_trail_activation_r=2.0),
        atr=5.0,
    )
    trails = _trails(actions)
    assert len(trails) == 1
    assert math.isclose(trails[0].new_sl, 115.0, rel_tol=1e-12)


def test_trail_fires_short():
    # short entry 100, r1 10, mark 75 == +2.5R. hw 75 (the low), atr 5, mult 2
    # → trail 85. 85 < current_sl 90 (tightens for short) and 85 > mark 75 → fires.
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 armed_rules={"auto_trail": True}, high_water=75.0)
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=90.0, mark=75.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    trails = _trails(actions)
    assert len(trails) == 1
    assert math.isclose(trails[0].new_sl, 85.0, rel_tol=1e-12)


def test_trail_refused_when_not_more_protective_long():
    # trail 115 would LOOSEN a current_sl already at 118 → refused (tighten-only).
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=118.0, mark=125.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_trail_refused_when_not_more_protective_short():
    # short trail 85 would LOOSEN (raise) a current_sl already at 80 → refused.
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 armed_rules={"auto_trail": True}, high_water=75.0)
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=80.0, mark=75.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_trail_refused_wrong_side_of_mark_long():
    # hw 125 near mark 120, tiny atr 0.1, mult 2 → trail 124.8, ABOVE mark 120.
    # A long stop above price triggers instantly → must be refused even though it
    # tightens vs current_sl 90.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=0.1,
    )
    assert not _trails(actions)


def test_trail_no_move_when_atr_missing_or_non_finite():
    for bad in (None, 0.0, -1.0, float("nan"), float("inf")):
        mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
        actions = evaluate_rules(
            side="long", entry=100.0, current_sl=90.0, mark=125.0,
            mgmt=mgmt, now_ms=0, settings=_settings(), atr=bad,
        )
        assert not _trails(actions), f"atr={bad!r} must not trail"


def test_trail_no_move_when_high_water_none():
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=None)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=125.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_trail_no_move_when_high_water_non_finite():
    for bad in (float("nan"), float("inf")):
        mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=bad)
        actions = evaluate_rules(
            side="long", entry=100.0, current_sl=90.0, mark=125.0,
            mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
        )
        assert not _trails(actions)


def test_trail_never_fires_when_unarmed():
    # auto_trail not armed → even at +5R with valid atr/hw, no trail.
    mgmt = _mgmt(armed_rules={}, high_water=150.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=150.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_trail_default_atr_none_keeps_existing_callers_working():
    # atr param omitted (default None) → no trail, no crash. Auto-BE still fires.
    mgmt = _mgmt(armed_rules={"auto_be": True, "auto_trail": True},
                 high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(),
    )
    assert not _trails(actions)
    assert [a for a in actions if isinstance(a, MoveSlToBe) and a.reason.startswith("auto-BE")]


def test_auto_be_and_auto_trail_both_fire_through_guard():
    # Both armed. mark 120 == +2R. BE ~100.06 tightens vs current_sl 90 → fires.
    # Trail = hw 125 - 2*5 = 115, tightens vs 90 and < mark 120 → fires too.
    # Neither suppresses the other; both pass the protective guard.
    mgmt = _mgmt(armed_rules={"auto_be": True, "auto_trail": True},
                 high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=90.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    moves = [a for a in actions if isinstance(a, MoveSlToBe)]
    reasons = {m.reason.split(" ")[0] for m in moves}
    assert "auto-trail" in reasons
    assert any(r.startswith("auto-BE") for r in reasons)
    assert len(moves) == 2
