"""Pure stats math for the journal feedback-loop (no I/O, no new dependency).

Honest by construction: every rate ships with its raw sample size, a Wilson
95% score interval on the overall win rate, and a low_sample flag so a 2/2=100%
is never mistaken for signal. See app/journal/resolver.py for the shadow-fill
limitations that these numbers inherit (fill at entry_price, no fees/slippage,
only tp1 tracked, intrabar ambiguity resolved pessimistically to LOSS).
"""

from __future__ import annotations

import math
from typing import Any

_Z = 1.96  # 95% two-sided normal quantile


def wilson_ci(wins: int, losses: int, z: float = _Z) -> list[float] | None:
    """Wilson score interval for a binomial proportion. Closed-form, stdlib
    math only — behaves at small n and near 0/1 (this app's early regime).
    Returns [lo, hi] clamped to [0,1], or None when there is no sample."""
    n = wins + losses
    if n <= 0:
        return None
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return [round(lo, 3), round(hi, 3)]


def _round(x: float | None, digits: int = 3) -> float | None:
    return None if x is None else round(x, digits)


def _block(
    wins: int, losses: int, sum_r: float, min_sample: int
) -> dict[str, Any]:
    """One {wins, losses, sample, win_rate, avg_realized_rrr, low_sample} block."""
    sample = wins + losses
    win_rate = _round(wins / sample) if sample else None
    avg_r = _round(sum_r / sample) if sample else None
    return {
        "wins": wins,
        "losses": losses,
        "sample": sample,
        "win_rate": win_rate,
        "avg_realized_rrr": avg_r,
        "low_sample": sample < min_sample,
    }


def _groups(raw_groups: dict[str, Any], min_sample: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, g in (raw_groups or {}).items():
        out[name] = _block(
            int(g.get("wins", 0)),
            int(g.get("losses", 0)),
            float(g.get("sum_r", 0.0)),
            min_sample,
        )
    return out


def build_stats_response(raw: dict[str, Any], *, min_sample: int) -> dict[str, Any]:
    """Shape db.journal_stats() output into the /api/journal/stats response.

    Empty DB -> zeros and nulls, never an error.
    """
    total = int(raw.get("total", 0))
    stay_out = int(raw.get("stay_out", 0))
    wins = int(raw.get("wins", 0))
    losses = int(raw.get("losses", 0))
    sample = wins + losses

    by_confidence = _groups(raw.get("by_confidence", {}), min_sample)
    by_action = _groups(raw.get("by_action", {}), min_sample)
    by_provider = _groups(raw.get("by_provider", {}), min_sample)

    # Caveats: name any non-empty group below the sample threshold, plus the
    # standing shadow-fill disclaimer so the rates are never misread as PnL.
    low_named: list[str] = []
    for grp in (by_confidence, by_action, by_provider):
        for name, block in grp.items():
            if 0 < block["sample"] < min_sample:
                low_named.append(name)
    caveats: list[str] = []
    if low_named:
        caveats.append(
            f"Sample < {min_sample} in: {', '.join(low_named)} — treat rates as noise."
        )
    caveats.append(
        "Shadow eval assumes fill at entry_price, no fees/slippage; only tp1 tracked."
    )

    return {
        "totals": {
            "proposals": total,
            "stay_out": stay_out,
            "stay_out_rate": _round(stay_out / total) if total else None,
            "resolved": sample,
            "pending": int(raw.get("pending", 0)),
            "expired": int(raw.get("expired", 0)),
            "skipped": int(raw.get("skipped", 0)),
        },
        "overall": {
            "wins": wins,
            "losses": losses,
            "sample": sample,
            "win_rate": _round(wins / sample) if sample else None,
            "win_rate_ci95": wilson_ci(wins, losses),
            "avg_realized_rrr": _round(float(raw.get("overall_sum_r", 0.0)) / sample)
            if sample
            else None,
            "low_sample": sample < min_sample,
        },
        "by_confidence": by_confidence,
        "by_action": by_action,
        "by_provider": by_provider,
        "caveats": caveats,
    }
