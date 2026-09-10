"""Block 2 / TP2 Task P2 — active Confidence-Recalibration (pure, fail-safe).

The USER'S HARD LINE: this layer recalibrates the DISPLAYED confidence and the
SIZING SUGGESTION from the KI's own realized hit-rate, but it NEVER blocks,
vetoes, forces STAY_OUT, changes `action`, or tightens any gate. It only produces
a (possibly downgraded) label, a size multiplier <= 1.0, and a transparent note.

Design (conservative by construction):
  * Uses the Wilson LOWER bound of the realized win rate (never the point
    estimate) — a 2/2 = 100 % group has a low floor and can never read as edge.
  * Only acts when the relevant group has n >= min_sample. Below that -> raw
    output (no change), because a tiny sample must not move the display.
  * DOWNGRADE ONLY: high -> medium -> low, low stays low. NEVER upgrades above
    the KI-emitted tier.
  * PURE: no I/O, no LLM call. Missing/empty stats -> raw output, never a crash.
"""

from __future__ import annotations

import math
from typing import Any

# Tier order for downgrade steps (index-based: one step weaker per downgrade).
_TIERS = ("low", "medium", "high")

# "Weak" threshold per emitted tier: the realized Wilson LOWER bound the tier's
# past calls must clear to KEEP that tier. A "high"-labelled call whose own
# high-group only floors at < 50 % WR (Wilson-LB) is not behaving like a high —
# so its DISPLAY (not its action) is downgraded one step. Documented, tunable.
# "low" has floor 0.0 -> never downgraded (it is already the floor tier).
_TIER_FLOOR: dict[str, float] = {"high": 0.50, "medium": 0.40, "low": 0.0}

# Size multiplier applied to the sizing SUGGESTION on a one-tier downgrade.
# Advisory only — it scales a recommendation, never a gate/limit.
_DOWNGRADE_SIZE_FACTOR = 0.5

# Block 2/TP2 P2b — REGIME axis floor. The realized Wilson LOWER bound the
# CURRENT live regime (btc-trend x vol bucket, e.g. "btcDown/volHigh") must
# clear to be trusted. Below it (at n >= min_sample) the regime is treated as
# "signal-weak" and the DISPLAY is downgraded one step — independent of the
# confidence-group verdict. Deliberately < _TIER_FLOOR["high"] (0.45 vs 0.50):
# the regime is a CONTEXT modifier, not a primary gate. Documented, tunable.
_REGIME_FLOOR = 0.45

# Hard floor for the advisory size multiplier so a combined downgrade can never
# collapse a suggestion toward zero. Defensive: with the current fixed per-axis
# factor (0.5) the min() never actually drops below this, but it bounds any
# future per-axis factor tuning. size stays in [_SIZE_FLOOR, 1.0].
_SIZE_FLOOR = 0.25


def _raw(emitted: str, note: str | None = None) -> dict[str, Any]:
    return {"calibrated_confidence": emitted, "size_factor": 1.0, "note": note}


def _lb_and_n(block: Any) -> tuple[float | None, int]:
    """Extract (win_rate_lo, n) from one stats group block, tolerant of both the
    build_stats_response shape (`win_rate_ci95` + `sample`) and the compact
    track_record shape (`win_rate_lo` + `n`). Fail-safe: bad input -> (None, 0)."""
    if not isinstance(block, dict):
        return None, 0
    try:
        n = int(block.get("sample") or block.get("n") or 0)
    except (TypeError, ValueError, OverflowError):
        n = 0
    lo = block.get("win_rate_lo")
    if lo is None:
        ci = block.get("win_rate_ci95")
        if isinstance(ci, (list, tuple)) and len(ci) == 2:
            lo = ci[0]
    try:
        lo_f = float(lo) if lo is not None else None
    except (TypeError, ValueError, OverflowError):
        lo_f = None
    if lo_f is not None and (not math.isfinite(lo_f) or not 0 <= lo_f <= 1):
        lo_f = None
    return lo_f, n


def recalibrate(
    emitted_confidence: str,
    setup_type: str | None,
    regime: str | None,
    stats: dict[str, Any] | None,
    min_sample: int,
) -> dict[str, Any]:
    """Recalibrate a KI-emitted confidence tier from the realized hit-rate.

    Returns {calibrated_confidence: str, size_factor: float, note: str | None}.

    NEVER returns an action/veto/STAY_OUT — only a LABEL, a size multiplier
    (<= 1.0), and a transparent note. size_factor is 1.0 unless a downgrade
    fired. Fully fail-safe: any missing/empty stats -> raw output.
    """
    emitted = str(emitted_confidence or "").strip().lower()
    # Unknown tier (never should happen — SetupConfidence is a Literal) -> raw.
    if emitted not in _TIER_FLOOR:
        return _raw(emitted_confidence)

    min_n = int(min_sample)
    stats_d = stats if isinstance(stats, dict) else {}

    # --- CONFIDENCE axis (unchanged semantics) -----------------------------
    # Realized WR-LB of PAST calls the KI labelled at THIS SAME tier
    # (by_confidence group — single-dimension, robust, no fragile intersection).
    by_conf = stats_d.get("by_confidence")
    conf_block = by_conf.get(emitted) if isinstance(by_conf, dict) else None
    conf_lo, conf_n = _lb_and_n(conf_block)
    conf_fires = (
        conf_n >= min_n and conf_lo is not None and conf_lo < _TIER_FLOOR[emitted]
    )

    # --- REGIME axis (new) -------------------------------------------------
    # Realized WR-LB of the CURRENT live regime tag (by_regime group). Inert for
    # a missing/"unknown" tag, an absent/broken group, or n < min_sample — i.e.
    # EXACTLY the old behaviour whenever the regime signal is not trustworthy.
    regime_key = str(regime or "").strip()
    reg_lo: float | None = None
    reg_n = 0
    if regime_key and regime_key.lower() != "unknown":
        by_regime = stats_d.get("by_regime")
        reg_block = by_regime.get(regime_key) if isinstance(by_regime, dict) else None
        reg_lo, reg_n = _lb_and_n(reg_block)
    regime_fires = reg_n >= min_n and reg_lo is not None and reg_lo < _REGIME_FLOOR

    # --- Combine: STRONGER (not summed) downgrade --------------------------
    # At most ONE tier step per call regardless of how many axes fire; size is
    # the MIN of the per-axis factors, hard-floored so it never collapses to 0.
    if not (conf_fires or regime_fires):
        # Nothing fired -> raw. Preserve the honest "too little data" note the
        # confidence group showed when it existed but was under-sampled.
        note = f"insufficient data (n={conf_n})" if 0 < conf_n < min_n else None
        return _raw(emitted_confidence, note)

    idx = _TIERS.index(emitted)
    calibrated = _TIERS[max(0, idx - 1)]
    if calibrated == emitted:  # emitted == "low" -> nothing to downgrade
        return _raw(emitted_confidence)

    conf_factor = _DOWNGRADE_SIZE_FACTOR if conf_fires else 1.0
    reg_factor = _DOWNGRADE_SIZE_FACTOR if regime_fires else 1.0
    size_factor = max(_SIZE_FLOOR, min(conf_factor, reg_factor))

    # Note names the ACTUALLY triggering axis/axes honestly. The confidence
    # win-rate is the tier-wide by_confidence number (all setups & regimes); the
    # regime win-rate is scoped to the current regime tag — never conflated.
    parts: list[str] = []
    if conf_fires and conf_lo is not None:
        parts.append(
            f"your {emitted} setups overall: "
            f"{round(conf_lo * 100)}% WR (Wilson LB, n={conf_n})"
        )
    if regime_fires and reg_lo is not None:
        parts.append(
            f"Regime {regime_key}: {round(reg_lo * 100)}% WR (Wilson LB, n={reg_n})"
        )
    note = f"AI: {emitted} · calibrated: {calibrated} — " + " · ".join(parts)
    return {
        "calibrated_confidence": calibrated,
        "size_factor": size_factor,
        "note": note,
    }
