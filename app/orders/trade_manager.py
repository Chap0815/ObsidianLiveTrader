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
) -> list[Action]:
    """Evaluate all management rules for one position. Pure; returns actions.

    Does not mutate ``mgmt`` (including its ``last_alert_state``): the caller
    persists any state change derived from the returned actions.
    """
    actions: list[Action] = []
    direction = 1 if side == "long" else -1

    # R is only available with a finite, strictly positive r1. Otherwise all
    # R-based signals (auto-BE, time-stop-R gate) are unavailable — no crash.
    r1 = mgmt.r1
    r_available = isinstance(r1, (int, float)) and math.isfinite(r1) and r1 > 0
    unreal_r = (mark - entry) * direction / r1 if r_available else None

    # ── Auto-BE (autonomous) ────────────────────────────────────────────────
    if (
        mgmt.armed_rules.get("auto_be")
        and not mgmt.be_done
        and unreal_r is not None
        and unreal_r >= settings.tm_be_trigger_r
    ):
        be = break_even_price(entry, side == "short", settings.tm_be_fee_rt)
        if be is not None and _is_more_protective(side, be, current_sl):
            reason = f"auto-BE @ +{unreal_r:.2f}R"
            actions.append(MoveSlToBe(be, reason))

    # ── Thesis-invalidation alarm (advisory, R-independent) ──────────────────
    inval = mgmt.invalidation_price
    if inval is not None and not mgmt.last_alert_state.get("thesis"):
        crossed = mark <= inval if direction == 1 else mark >= inval
        if crossed:
            actions.append(
                Alert(
                    "thesis",
                    (
                        f"Thesis invalidiert: {side.upper()} @ mark {mark:g} "
                        f"hat Invalidierungspreis {inval:g} gekreuzt "
                        "— Position pruefen."
                    ),
                )
            )

    # ── Time-stop alarm (advisory, needs R) ─────────────────────────────────
    time_ms = settings.tm_time_stop_hours * 3_600_000
    if (
        not mgmt.last_alert_state.get("time_stop")
        and (now_ms - mgmt.opened_at_ms) >= time_ms
        and unreal_r is not None
        and unreal_r < settings.tm_time_stop_min_r
    ):
        actions.append(
            Alert(
                "time_stop",
                (
                    f"Time-Stop: {side.upper()} laeuft seit "
                    f"{settings.tm_time_stop_hours:g}h und steht erst bei "
                    f"{unreal_r:+.2f}R (< {settings.tm_time_stop_min_r:g}R) "
                    "— Position pruefen."
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
