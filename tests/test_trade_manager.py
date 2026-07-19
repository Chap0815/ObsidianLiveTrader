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
