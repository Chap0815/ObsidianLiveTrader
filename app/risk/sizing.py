"""Position sizing and RRR helpers.

Risk at stop ≈ vol * contractSize * abs(entry - stop).
G3 may apply a RISK_SLIPPAGE_PCT buffer on the stop distance.
"""

from __future__ import annotations

import math


def calc_rrr(side: str, entry: float, stop: float, tp: float) -> float:
    """Reward:risk. long: (tp-entry)/(entry-stop); short inverted.

    Raises ValueError if distances are non-positive / invalid geometry.
    """
    side_l = (side or "").lower()
    if entry <= 0 or stop <= 0 or tp <= 0:
        raise ValueError("entry, stop, tp must be positive")

    if side_l == "long":
        risk = entry - stop
        reward = tp - entry
    elif side_l == "short":
        risk = stop - entry
        reward = entry - tp
    else:
        raise ValueError(f"side must be long or short, got {side!r}")

    if risk <= 0:
        raise ValueError("stop must be below entry for long / above for short")
    if reward <= 0:
        raise ValueError("tp must be above entry for long / below for short")
    return reward / risk


def risk_usdt(
    vol: float,
    contract_size: float,
    entry: float,
    stop: float,
    *,
    slippage_pct: float = 0.0,
) -> float:
    """USDT risk at SL for `vol` contracts (optionally buffered stop distance).

    slippage_pct is percent points (0.05 → 0.05%), applied as
    distance * (1 + slippage_pct/100).
    """
    distance = abs(float(entry) - float(stop))
    if distance <= 0 or vol <= 0 or contract_size <= 0:
        return 0.0
    if slippage_pct and slippage_pct > 0:
        distance = distance * (1.0 + float(slippage_pct) / 100.0)
    return abs(vol) * float(contract_size) * distance


def suggest_vol(
    equity: float,
    risk_pct: float,
    contract_size: float,
    entry: float,
    stop: float,
    vol_unit: float,
    min_vol: float,
    *,
    side: str | None = None,
    slippage_pct: float = 0.0,
    existing_risk_usdt: float = 0.0,
    available_usdt: float | None = None,
    leverage: float = 1.0,
    max_notional_pct_of_equity: float = 0.0,
) -> float:
    """Largest vol (floored to vol_unit) that risks ≈ risk_pct of equity at SL.

    Mirrors the SAME clamps app.risk.gates.validate_order applies at
    Preview/Confirm, so a suggested size can never exceed what the real gate
    would accept:

      - ``slippage_pct``: RISK_SLIPPAGE_PCT buffer on the SL distance (same
        as ``risk_usdt`` above / G3 in gates.py).
      - ``side``: directional SL geometry (long SL must be below entry,
        short SL must be above entry) — an inverted stop is invalid
        geometry, not "more risk", and must suggest 0, not a bogus size.
      - ``existing_risk_usdt``: existing same-side open risk is deducted
        from the risk budget first (aggregate MAX_RISK_PCT, like G3).
      - ``max_notional_pct_of_equity`` / ``available_usdt`` + ``leverage``:
        the equity-relative notional cap and the available-margin cap both
        clamp vol further, same as the notional/margin checks in gates.py.

    All new parameters are keyword-only and default to a no-op, so existing
    callers that only pass the base positional args keep their exact legacy
    result.

    Never inflates to min_vol when the risk budget cannot afford it — returns 0
    so the UI/API can show that no gate-safe size exists.
    """
    if equity <= 0 or risk_pct <= 0 or contract_size <= 0:
        return 0.0
    distance = abs(float(entry) - float(stop))
    if distance <= 0:
        return 0.0

    side_l = (side or "").strip().lower()
    if side_l == "long" and float(stop) >= float(entry):
        return 0.0
    if side_l == "short" and float(stop) <= float(entry):
        return 0.0

    if slippage_pct and slippage_pct > 0:
        distance = distance * (1.0 + float(slippage_pct) / 100.0)

    budget = float(equity) * float(risk_pct) / 100.0 - max(0.0, float(existing_risk_usdt or 0.0))
    if budget <= 0:
        return 0.0

    raw = budget / (float(contract_size) * distance)

    notional_per_vol = float(contract_size) * float(entry)
    if max_notional_pct_of_equity and max_notional_pct_of_equity > 0 and notional_per_vol > 0:
        cap_notional = float(equity) * float(max_notional_pct_of_equity) / 100.0
        raw = min(raw, cap_notional / notional_per_vol)

    if available_usdt is not None:
        if float(available_usdt) <= 0:
            return 0.0
        if leverage and float(leverage) > 0 and notional_per_vol > 0:
            raw = min(raw, (float(available_usdt) * float(leverage)) / notional_per_vol)

    rounded = round_down_to_unit(raw, vol_unit)
    if rounded <= 0:
        return 0.0
    if min_vol and rounded < float(min_vol):
        return 0.0
    return rounded


def round_down_to_unit(value: float, unit: float) -> float:
    """Floor value to a multiple of unit (contract vol/price step)."""
    if unit is None or unit <= 0:
        return float(value)
    v = float(value)
    u = float(unit)
    # Avoid float dust: floor(v/u + eps) * u
    steps = math.floor(v / u + 1e-12)
    if steps < 0:
        steps = 0
    return _clean_float(steps * u)


def round_to_unit(value: float, unit: float, direction: str = "nearest") -> float:
    """Round to a multiple of ``unit``.

    direction:
      "nearest" (default) — Python round(); UNCHANGED legacy behaviour, kept so
        every existing caller keeps its exact semantics.
      "down" / "floor" — toward -inf (never exceeds value).
      "up" / "ceil"    — toward +inf (never below value).

    Half-even rounding ("nearest") can drift a trigger to the exchange-unfriendly
    side of the intended price. For side-aware SL/TP rounding use
    ``round_trigger_to_unit`` below, which picks floor/ceil so the locally
    rounded trigger is never LESS protective than intended (risk never
    understated, RRR never overstated).
    """
    if unit is None or unit <= 0:
        return float(value)
    v = float(value)
    u = float(unit)
    d = (direction or "nearest").strip().lower()
    if d in ("down", "floor"):
        steps = math.floor(v / u + 1e-12)
    elif d in ("up", "ceil"):
        steps = math.ceil(v / u - 1e-12)
    else:
        steps = round(v / u)
    return _clean_float(steps * u)


def round_trigger_to_unit(
    value: float, unit: float, *, side: str, kind: str
) -> float:
    """Side-aware, conservative rounding for an SL/TP trigger price.

    Rounds so the resulting trigger is never *less* protective than ``value``:

      stop-loss   long  → floor (widen below entry; risk never understated)
                  short → ceil  (widen above entry)
      take-profit long  → floor (pull toward entry; RRR never overstated)
                  short → ceil

    Falls back to nearest rounding for unknown side/kind so it can never behave
    worse than the legacy path.
    """
    s = (side or "").strip().lower()
    k = (kind or "").strip().lower()
    if k in ("sl", "stop", "stop_loss", "stoploss"):
        if s == "long":
            return round_to_unit(value, unit, "down")
        if s == "short":
            return round_to_unit(value, unit, "up")
    elif k in ("tp", "take_profit", "takeprofit"):
        if s == "long":
            return round_to_unit(value, unit, "down")
        if s == "short":
            return round_to_unit(value, unit, "up")
    return round_to_unit(value, unit)


def _clean_float(x: float) -> float:
    """Reduce binary float noise for display/API (e.g. 0.30000000004)."""
    if not math.isfinite(x):
        return x
    return float(f"{x:.12g}")
