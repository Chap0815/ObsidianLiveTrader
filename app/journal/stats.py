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


def _fill_rate(resolved: int, no_fill: int) -> float | None:
    """Share of this group's shadow-tracked LIMIT setups that actually filled:
    resolved / (resolved + NO_FILL). None when the group has neither. NO_FILL
    rows are excluded from win rate (they never became trades), so without this
    the fill bias — a setup that wins often but rarely gets reached — is hidden.
    """
    denom = resolved + no_fill
    return _round(resolved / denom) if denom else None


def _block(
    wins: int,
    losses: int,
    sum_r: float,
    min_sample: int,
    *,
    ambiguous: int = 0,
    clean_wins: int = 0,
    clean_losses: int = 0,
    no_fill: int = 0,
) -> dict[str, Any]:
    """One per-group stats block.

    F2-09: `win_rate_ci95` (Wilson) so a small-n group ships its uncertainty,
    not just a point estimate. F2-10: `ambiguous` (intrabar tp1&sl ties, counted
    pessimistically as LOSS by the resolver) plus a `clean_win_rate` that drops
    those rows entirely — so the ambiguity is visible and correctable.
    Lern-Loop-Härtung: `no_fill` + `fill_rate` expose the NO_FILL bias per group
    (win rate is computed on the resolved rows ONLY; fill_rate says how often the
    setup was actually reachable).
    """
    sample = wins + losses
    win_rate = _round(wins / sample) if sample else None
    avg_r = _round(sum_r / sample) if sample else None
    clean_sample = clean_wins + clean_losses
    clean_win_rate = _round(clean_wins / clean_sample) if clean_sample else None
    return {
        "wins": wins,
        "losses": losses,
        "sample": sample,
        "win_rate": win_rate,
        "win_rate_ci95": wilson_ci(wins, losses),
        "avg_realized_rrr": avg_r,
        "ambiguous": ambiguous,
        "clean_win_rate": clean_win_rate,
        "no_fill": no_fill,
        "fill_rate": _fill_rate(sample, no_fill),
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
            ambiguous=int(g.get("ambiguous", 0)),
            clean_wins=int(g.get("clean_wins", 0)),
            clean_losses=int(g.get("clean_losses", 0)),
            no_fill=int(g.get("no_fill", 0)),
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
    # F2-07: dedicated NET denominator — only WIN/LOSS rows that actually carry
    # a net value (pre-migration rows have NULL net and must not dilute it).
    net_sample = int(raw.get("overall_net_sample", 0))

    by_confidence = _groups(raw.get("by_confidence", {}), min_sample)
    by_action = _groups(raw.get("by_action", {}), min_sample)
    by_provider = _groups(raw.get("by_provider", {}), min_sample)
    by_setup = _groups(raw.get("by_setup", {}), min_sample)
    # Block 2/TP2 Task P1: segment by the persisted regime tag (btc-trend x
    # vol bucket), mirroring by_setup exactly.
    by_regime = _groups(raw.get("by_regime", {}), min_sample)

    # Caveats: name any non-empty group below the sample threshold, plus the
    # standing shadow-fill disclaimer so the rates are never misread as PnL.
    low_named: list[str] = []
    for grp in (by_confidence, by_action, by_provider, by_setup, by_regime):
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
            # F2-02: limit entries that never filled — NOT resolved trades,
            # excluded from win-rate, surfaced separately.
            "no_fill": int(raw.get("no_fill", 0)),
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
            # F2-07: NET expectancy (after round-trip costs) — Task 21 learns
            # on this, not the gross avg above. Uses a DEDICATED denominator
            # (only rows with a non-NULL net) so pre-migration WIN/LOSS rows
            # (NULL net) don't dilute the average toward 0.
            "avg_realized_rrr_net": _round(
                float(raw.get("overall_sum_r_net", 0.0)) / net_sample
            )
            if net_sample
            else None,
            "net_sample": net_sample,
            # Lern-Loop-Härtung: overall NO_FILL count + fill_rate (resolved /
            # resolved+NO_FILL) so the win rate above is read WITH its selection
            # bias, not as if every setup was reachable.
            "no_fill": int(raw.get("no_fill", 0)),
            "fill_rate": _fill_rate(sample, int(raw.get("no_fill", 0))),
            "low_sample": sample < min_sample,
        },
        "by_confidence": by_confidence,
        "by_action": by_action,
        "by_provider": by_provider,
        "by_setup": by_setup,
        "by_regime": by_regime,
        "caveats": caveats,
    }


def _lower_bound(block: dict[str, Any]) -> float | None:
    """Wilson LOWER bound of a group's win rate, or None when no sample.

    The lower bound (not the point win_rate) is the honest number to feed the
    model: a 2/2 = 100% group has a low Wilson floor, so it can never read as
    edge. See build_track_record for why this matters given weak journal dedupe.
    """
    ci = block.get("win_rate_ci95")
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        return ci[0]
    return None


def build_track_record(stats: dict[str, Any], *, min_sample: int) -> dict[str, Any] | None:
    """Task 21 (K2-01/F2-01): a COMPACT, honest calibration block for the
    analyze prompt, derived from an already-built build_stats_response() output
    (never recomputed).

    Shape (only the fields the model needs to CALIBRATE its own confidence):
      overall: {n, net_expectancy_r, win_rate_lo, fill}
      by_confidence / by_setup: {name: {n, win_rate_lo, avg_r, fill}} — only groups
        whose own n >= min_sample (small groups are dropped, never shown as edge).
      `fill` = resolved / (resolved + NO_FILL): win_rate_lo is on filled rows only,
        so a low fill flags an edge that is often unreachable (selection bias).

    Returns None when the overall resolved sample is below min_sample — the
    whole block is omitted rather than presenting noise as ground truth.

    Honesty guards (the journal dedupe is weak — context_hash includes
    last_price, so rows can be correlated and the Wilson interval optimistically
    tight): (a) gate on overall n >= min_sample, (b) ship the Wilson LOWER bound
    not the point estimate, (c) the prompt frames this as a SOFT hint the model
    weighs, never a veto/threshold. See app/llm/prompts.py _TRACK_RECORD_RULE.
    """
    overall = stats.get("overall") or {}
    n = int(overall.get("sample") or 0)
    if n < min_sample:
        return None

    def _groups(grp: dict[str, Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, block in (grp or {}).items():
            gn = int(block.get("sample") or 0)
            if gn >= min_sample:
                out[name] = {
                    "n": gn,
                    "win_rate_lo": _lower_bound(block),
                    "avg_r": block.get("avg_realized_rrr"),
                    # `fill` = fraction of this group's LIMIT setups that filled;
                    # the win_rate_lo above is on the filled rows ONLY, so a low
                    # `fill` flags an edge that is often unreachable. Compact by
                    # design (one extra number per group) — token-cheap.
                    "fill": block.get("fill_rate"),
                }
        return out

    return {
        "overall": {
            "n": n,
            "net_expectancy_r": overall.get("avg_realized_rrr_net"),
            "win_rate_lo": _lower_bound(overall),
            "fill": overall.get("fill_rate"),
        },
        "by_confidence": _groups(stats.get("by_confidence")),
        "by_setup": _groups(stats.get("by_setup")),
    }
