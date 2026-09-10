"""Pure rule-evaluation core for the Trade-Management-Layer.

``evaluate_rules`` is the money-critical decision heart: it decides when the
server autonomously moves a real stop-loss to break-even, and when to raise
advisory alarms (thesis-invalidation, time-stop). It is a PURE function — no
I/O, no DB, no wall-clock (``now_ms`` is passed in) — so every path can be
exhaustively unit-tested (see spec §7).

Safety invariants enforced here (also re-checked in the service, spec §3):
- Auto-BE only for an ARMED position (``armed_rules["auto_be"]``), only once
  (``be_done``), only at/above the R threshold.
- A stop is NEVER moved in the loosening direction — long: new SL must be
  strictly greater than current SL; short: strictly less. If BE is not more
  protective than the current SL, no action is emitted.
- Advisory alarms debounce via ``last_alert_state``: the CALLER persists the
  updated state; this function only READS the passed-in state and does not
  mutate its inputs.
- R-based signals require a finite ``r1 > 0``; otherwise they are silently
  unavailable (no crash, no BE, no time-stop-R gate). The thesis alarm does not
  depend on R and still works.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.orders.be_math import break_even_price


@dataclass
class MgmtBaseline:
    """Durable per-position snapshot fixed at arming / first sighting.

    ``r1`` is the initial risk distance ``|entry - initial_sl|`` fixed at
    baseline time so the "+1R" calculation stays stable even after the SL later
    moves (e.g. by auto-BE).
    """

    entry: float
    initial_sl: float | None
    r1: float
    opened_at_ms: int
    invalidation_price: float | None = None
    armed_rules: dict = field(default_factory=dict)
    be_done: bool = False
    last_alert_state: dict = field(default_factory=dict)
    high_water: float | None = None
    # F4: the high-water captured at a MANUAL SL modify. While set, the trail is
    # held (a deliberate loosening isn't immediately overridden) until the
    # high-water surpasses it (long: strictly above; short: strictly below).
    user_override_hw: float | None = None


@dataclass
class MoveSlToBe:
    """Autonomous action: move the stop-loss to the (fee-aware) break-even."""

    new_sl: float
    reason: str


@dataclass
class Alert:
    """Advisory action: surface a message to the user (no order change)."""

    kind: str
    message: str


# Action union — callers isinstance-check MoveSlToBe / Alert.
Action = MoveSlToBe | Alert


def evaluate_rules(
    *,
    side: str,
    entry: float,
    current_sl: float | None,
    mark: float,
    mgmt: MgmtBaseline,
    now_ms: int,
    settings,
    atr: float | None = None,
) -> list[Action]:
    """Evaluate all management rules for one position. Pure; returns actions.

    Does not mutate ``mgmt`` (including its ``last_alert_state``): the caller
    persists any state change derived from the returned actions.
    """
    actions: list[Action] = []
    if side not in ("long", "short"):
        return actions  # unknown/mislabeled side → do nothing (fail-safe, never guess)
    direction = 1 if side == "long" else -1

    # Defensive: a corrupt JSON column could deserialize to a non-dict; coerce so
    # a `.get` can never crash this money-path function on its own (the fail-safe
    # monitor loop would also catch it, but the pure function should be robust).
    armed = mgmt.armed_rules if isinstance(mgmt.armed_rules, dict) else {}
    alert_state = mgmt.last_alert_state if isinstance(mgmt.last_alert_state, dict) else {}

    # R is only available with a finite, strictly positive r1. Otherwise all
    # R-based signals (auto-BE, time-stop-R gate) are unavailable — no crash.
    r1 = mgmt.r1
    r_available = isinstance(r1, (int, float)) and math.isfinite(r1) and r1 > 0
    unreal_r = (mark - entry) * direction / r1 if r_available else None

    # ── Auto-BE (autonomous) ────────────────────────────────────────────────
    if (
        armed.get("auto_be") is True
        and not mgmt.be_done
        and unreal_r is not None
        and unreal_r >= settings.tm_be_trigger_r
    ):
        be = break_even_price(entry, side == "short", settings.tm_be_fee_rt)
        # Emit only if BE (a) tightens the stop AND (b) sits on the correct side
        # of the current price. A tiny-r1 position (fee buffer > +1R distance)
        # would otherwise get a BE past mark — long stop above price / short stop
        # below price — that triggers instantly or is rejected by the exchange.
        be_on_right_side = be is not None and (be < mark if side == "long" else be > mark)
        if be is not None and be_on_right_side and _is_more_protective(side, be, current_sl):
            reason = f"auto-BE @ +{unreal_r:.2f}R"
            actions.append(MoveSlToBe(be, reason))

    # ── Auto-Trailing (autonomous, Chandelier) ──────────────────────────────
    # Independent of Auto-BE: both can fire in one call, each through the same
    # protective guard. No be_done latch — the trail fires repeatedly, but every
    # emitted move is monotonically tightening, so it can never loosen a stop.
    high_water = mgmt.high_water
    hw_ok = isinstance(high_water, (int, float)) and math.isfinite(high_water)
    atr_ok = isinstance(atr, (int, float)) and math.isfinite(atr) and atr > 0
    # F4: a manual SL move parks the then-current high-water here. The trail stays
    # silent until the high-water advances PAST that level, so the user's chosen
    # (looser) stop is respected instead of being restored every cycle. A tighten
    # is unaffected: its own _is_more_protective guard already blocks the trail
    # until a new high, exactly as the override does.
    uo = mgmt.user_override_hw
    uo_active = isinstance(uo, (int, float)) and math.isfinite(uo)
    if (
        armed.get("auto_trail") is True
        and unreal_r is not None
        and unreal_r >= settings.tm_trail_activation_r
        and atr_ok
        and hw_ok
        and (
            not uo_active
            or (high_water > uo if side == "long" else high_water < uo)
        )
    ):
        offset = settings.tm_trail_atr_mult * atr
        trail = high_water - offset if side == "long" else high_water + offset
        # Same discipline as Auto-BE: emit only if it (a) tightens the stop AND
        # (b) sits on the correct side of mark (long trail < mark, short > mark),
        # so the trail can never be placed past price where it would trigger
        # instantly or be rejected.
        trail_on_right_side = trail < mark if side == "long" else trail > mark
        # F3: minimum step — the trail must beat the live SL by at least
        # (tm_trail_min_step_atr * ATR) before we spend a modify round-trip. With
        # no current SL any protective trail qualifies; min_step=0 collapses to
        # the old any-improvement behavior (the _is_more_protective guard below
        # still forbids equal/loosening moves).
        min_step = settings.tm_trail_min_step_atr * atr
        if side == "long":
            beats_min_step = current_sl is None or (trail - current_sl) >= min_step
        else:
            beats_min_step = current_sl is None or (current_sl - trail) >= min_step
        if (
            trail_on_right_side
            and beats_min_step
            and _is_more_protective(side, trail, current_sl)
        ):
            actions.append(MoveSlToBe(trail, "auto-trail"))

    # ── Thesis-invalidation alarm (advisory, R-independent) ──────────────────
    inval = mgmt.invalidation_price
    if inval is not None and not alert_state.get("thesis"):
        crossed = mark <= inval if direction == 1 else mark >= inval
        if crossed:
            actions.append(
                Alert(
                    "thesis",
                    (
                        f"Thesis invalidated: {side.upper()} @ mark {mark:g} "
                        f"crossed invalidation price {inval:g} — check the position."
                    ),
                )
            )

    # ── Time-stop alarm (advisory, needs R) ─────────────────────────────────
    time_ms = settings.tm_time_stop_hours * 3_600_000
    if (
        not alert_state.get("time_stop")
        and (now_ms - mgmt.opened_at_ms) >= time_ms
        and unreal_r is not None
        and unreal_r < settings.tm_time_stop_min_r
    ):
        actions.append(
            Alert(
                "time_stop",
                (
                    f"Time stop: {side.upper()} has been open for "
                    f"{settings.tm_time_stop_hours:g}h and is only at "
                    f"{unreal_r:+.2f}R (< {settings.tm_time_stop_min_r:g}R) "
                    "— check the position."
                ),
            )
        )

    return actions


def _is_more_protective(
    side: str, candidate_sl: float, current_sl: float | None
) -> bool:
    """True only if ``candidate_sl`` tightens the stop (never loosens it).

    long  → candidate must be strictly ABOVE current; short → strictly BELOW.
    A missing current SL means any protective stop is an improvement.
    """
    if current_sl is None:
        return True
    if side == "long":
        return candidate_sl > current_sl
    return candidate_sl < current_sl
