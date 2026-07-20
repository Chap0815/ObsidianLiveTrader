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
    except (TypeError, ValueError):
        n = 0
    lo = block.get("win_rate_lo")
    if lo is None:
        ci = block.get("win_rate_ci95")
        if isinstance(ci, (list, tuple)) and len(ci) == 2:
            lo = ci[0]
    try:
        lo_f = float(lo) if lo is not None else None
    except (TypeError, ValueError):
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

    # We recalibrate the emitted tier against the realized win-rate of PAST calls
    # the KI labelled at THAT SAME tier (by_confidence group). setup_type/regime
    # are accepted per the design signature and folded into the note for
    # transparency; the threshold logic keys off the confidence group only
    # (single-dimension, robust — no fragile intersected groups required).
    by_conf = (stats or {}).get("by_confidence") if isinstance(stats, dict) else None
    block = by_conf.get(emitted) if isinstance(by_conf, dict) else None
    win_rate_lo, n = _lb_and_n(block)

    # Not enough data (or group absent) -> NO change. Show raw + honest note.
    if n < int(min_sample):
        note = f"zu wenig Daten (n={n})" if n > 0 else None
        return _raw(emitted_confidence, note)

    floor = _TIER_FLOOR[emitted]
    # Strong enough (or no LB available) -> keep the KI tier (never upgrade).
    if win_rate_lo is None or win_rate_lo >= floor:
        return _raw(emitted_confidence)

    # Clearly weak for this tier -> DOWNGRADE one step (never below "low"),
    # shrink the sizing SUGGESTION. action is untouched — this is display only.
    idx = _TIERS.index(emitted)
    calibrated = _TIERS[max(0, idx - 1)]
    if calibrated == emitted:  # emitted == "low" -> nothing to downgrade
        return _raw(emitted_confidence)

    wr_pct = round(win_rate_lo * 100)
    seg = f"deine {emitted}-Setups"
    ctx = "/".join(p for p in (setup_type or "", regime or "") if p)
    if ctx:
        seg += f" ({ctx})"
    note = (
        f"KI: {emitted} · kalibriert: {calibrated} — "
        f"{seg}: {wr_pct} % WR (Wilson-LB, n={n})"
    )
    return {
        "calibrated_confidence": calibrated,
        "size_factor": _DOWNGRADE_SIZE_FACTOR,
        "note": note,
    }
