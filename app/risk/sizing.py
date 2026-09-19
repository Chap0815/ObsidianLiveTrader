"""Position sizing and RRR helpers.

Risk at stop ≈ vol * contractSize * abs(entry - stop).
G3 may apply a RISK_SLIPPAGE_PCT buffer on the stop distance.
"""

from __future__ import annotations

import math


def _typed_finite_float(value: object) -> float | None:
    """Return a finite float only for an already-typed numeric value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except OverflowError:
        return None
    return parsed if math.isfinite(parsed) else None


def adverse_market_entry(last_price: float, side: str, slippage_pct: float) -> float:
    """Return the worst fill allowed by the configured market slippage cap."""
    last = float(last_price)
    slip = float(slippage_pct) / 100.0
    side_l = (side or "").strip().lower()
    if not math.isfinite(last) or last <= 0:
        raise ValueError("last_price must be a finite number > 0")
    if not math.isfinite(slip) or slip < 0:
        raise ValueError("slippage_pct must be a finite number >= 0")
    if side_l == "long":
        entry = last * (1.0 + slip)
    elif side_l == "short":
        entry = last * (1.0 - slip)
    else:
        raise ValueError("side must be long or short")
    if not math.isfinite(entry) or entry <= 0:
        raise ValueError("adverse market entry is not a finite positive price")
    return entry


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
    max_vol: float | None = None,
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
      - ``max_vol``: the exchange's absolute contract-volume ceiling.

    All new parameters are keyword-only and default to a no-op, so existing
    callers that only pass the base positional args keep their exact legacy
    result.

    Never inflates to min_vol when the risk budget cannot afford it — returns 0
    so the UI/API can show that no gate-safe size exists.
    """
    numeric_inputs = (
        equity,
        risk_pct,
        contract_size,
        entry,
        stop,
        vol_unit,
        min_vol,
        existing_risk_usdt,
        leverage,
        max_notional_pct_of_equity,
        slippage_pct,
    )
    parsed_inputs = tuple(_typed_finite_float(value) for value in numeric_inputs)
    if any(value is None for value in parsed_inputs):
        return 0.0
    (
        equity_f,
        risk_pct_f,
        contract_size_f,
        entry_f,
        stop_f,
        vol_unit_f,
        min_vol_f,
        existing_risk_f,
        leverage_f,
        max_notional_pct_f,
        slippage_pct_f,
    ) = parsed_inputs
    available_f = (
        None if available_usdt is None else _typed_finite_float(available_usdt)
    )
    if available_usdt is not None and available_f is None:
        return 0.0
    max_vol_f = None if max_vol is None else _typed_finite_float(max_vol)
    if max_vol is not None and max_vol_f is None:
        return 0.0
    if (
        equity_f <= 0
        or risk_pct_f <= 0
        or contract_size_f <= 0
        or entry_f <= 0
        or stop_f <= 0
        or vol_unit_f < 0
        or min_vol_f < 0
        or existing_risk_f < 0
        or leverage_f <= 0
        or max_notional_pct_f < 0
        or slippage_pct_f < 0
        or (max_vol_f is not None and max_vol_f <= 0)
    ):
        return 0.0
    distance = abs(entry_f - stop_f)
    if distance <= 0:
        return 0.0

    side_l = (side or "").strip().lower()
    if side_l == "long" and stop_f >= entry_f:
        return 0.0
    if side_l == "short" and stop_f <= entry_f:
        return 0.0

    if slippage_pct_f > 0:
        distance = distance * (1.0 + slippage_pct_f / 100.0)

    budget = equity_f * risk_pct_f / 100.0 - existing_risk_f
    if not math.isfinite(budget) or budget <= 0:
        return 0.0

    risk_per_vol = contract_size_f * distance
    if not math.isfinite(risk_per_vol) or risk_per_vol <= 0:
        return 0.0
    raw = budget / risk_per_vol
    if not math.isfinite(raw) or raw <= 0:
        return 0.0

    notional_per_vol = contract_size_f * entry_f
    if max_notional_pct_f > 0 and notional_per_vol > 0:
        cap_notional = equity_f * max_notional_pct_f / 100.0
        raw = min(raw, cap_notional / notional_per_vol)

    if available_f is not None:
        if available_f <= 0:
            return 0.0
        if notional_per_vol > 0:
            raw = min(raw, (available_f * leverage_f) / notional_per_vol)

    if max_vol_f is not None:
        raw = min(raw, max_vol_f)
    if vol_unit_f > 0 and not math.isfinite(raw / vol_unit_f):
        return 0.0
    rounded = round_down_to_unit(raw, vol_unit_f)
    if rounded <= 0:
        return 0.0
    if min_vol_f > 0 and rounded < min_vol_f:
        return 0.0
    return rounded


def round_down_to_unit(value: float, unit: float) -> float:
    """Floor value to a multiple of unit (contract vol/price step)."""
    if unit is None or unit <= 0:
        return float(value)
    v = float(value)
    u = float(unit)
    step_count = v / u
    if not math.isfinite(step_count):
        return 0.0
    # Avoid float dust: floor(v/u + eps) * u
    steps = math.floor(step_count + 1e-12)
    if steps < 0:
        steps = 0
    return _directed_unit_product(v, u, steps, direction="down")


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
    step_count = v / u
    if not math.isfinite(step_count):
        return 0.0
    d = (direction or "nearest").strip().lower()
    if d in ("down", "floor"):
        steps = math.floor(step_count + 1e-12)
        return _directed_unit_product(v, u, steps, direction="down")
    elif d in ("up", "ceil"):
        steps = math.ceil(step_count - 1e-12)
        return _directed_unit_product(v, u, steps, direction="up")
    else:
        steps = round(step_count)
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
        return 0.0
    return float(f"{x:.12g}")


def _directed_unit_product(
    value: float, unit: float, steps: int, *, direction: str
) -> float:
    """Clean a unit product without reversing floor/ceil direction.

    Twelve-significant-digit cleanup is useful for ordinary exchange values,
    but at large step counts it can cross the original value. The small epsilon
    used before floor/ceil can do the same around an exact boundary. Recheck the
    direction after cleanup and move one whole step when necessary.
    """
    delta = -1 if direction == "down" else 1
    for _ in range(2):
        product = steps * unit
        if not math.isfinite(product):
            return 0.0
        cleaned = _clean_float(product)
        if direction == "down":
            if cleaned <= value:
                return cleaned
            if product <= value:
                return product
        else:
            if cleaned >= value:
                return cleaned
            if product >= value:
                return product
        steps += delta
    return 0.0
