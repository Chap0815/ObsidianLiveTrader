"""Block 2 / TP2 Task P2 — pure Confidence-Recalibration helper tests.

Invariant under test (the USER'S HARD LINE): recalibrate() only adjusts a
DISPLAY label + a size multiplier + a note. It NEVER emits an action, a veto, a
STAY_OUT, or anything that could block a trade. Downgrade-only, Wilson-LB gated,
n>=min_sample gated, fail-safe on empty stats.
"""

from __future__ import annotations

from app.llm.recalibration import recalibrate


def _stats(tier: str, *, ci_lo: float, ci_hi: float, sample: int) -> dict:
    """A minimal build_stats_response-shaped stats dict with one confidence
    group (win_rate_ci95 lower bound + sample), which is what recalibrate reads."""
    return {
        "by_confidence": {
            tier: {"sample": sample, "win_rate_ci95": [ci_lo, ci_hi]},
        }
    }


def test_downgrade_on_weak_lower_bound_with_size_factor():
    # high group floors at 46 % (Wilson-LB) over n=40 -> below the 0.50 high
    # floor -> DISPLAY downgraded high->medium, sizing SUGGESTION halved.
    out = recalibrate(
        "high", "breakout", "btcUp/volNormal",
        _stats("high", ci_lo=0.46, ci_hi=0.62, sample=40), min_sample=20,
    )
    assert out["calibrated_confidence"] == "medium"
    assert out["size_factor"] == 0.5
    assert out["size_factor"] < 1.0
    assert "kalibriert: medium" in out["note"]
    assert "46 % WR" in out["note"]
    assert "n=40" in out["note"]


def test_medium_downgrades_to_low_on_weak():
    out = recalibrate(
        "medium", None, None,
        _stats("medium", ci_lo=0.30, ci_hi=0.50, sample=25), min_sample=20,
    )
    assert out["calibrated_confidence"] == "low"
    assert out["size_factor"] == 0.5


def test_unchanged_on_strong_lower_bound():
    # high group floors at 60 % -> above the 0.50 floor -> keep, size 1.0.
    out = recalibrate(
        "high", "breakout", None,
        _stats("high", ci_lo=0.60, ci_hi=0.78, sample=40), min_sample=20,
    )
    assert out["calibrated_confidence"] == "high"
    assert out["size_factor"] == 1.0
    assert out["note"] is None


def test_raw_when_below_min_sample():
    # Only n=5 in the high group -> NO change, raw tier, honest note.
    out = recalibrate(
        "high", None, None,
        _stats("high", ci_lo=0.10, ci_hi=0.40, sample=5), min_sample=20,
    )
    assert out["calibrated_confidence"] == "high"
    assert out["size_factor"] == 1.0
    assert out["note"] == "zu wenig Daten (n=5)"


def test_never_upgrades_above_emitted_tier():
    # Even a stellar low-group win rate must NOT upgrade the display above "low".
    out = recalibrate(
        "low", None, None,
        _stats("low", ci_lo=0.95, ci_hi=0.99, sample=50), min_sample=20,
    )
    assert out["calibrated_confidence"] == "low"
    assert out["size_factor"] == 1.0


def test_never_blocks_or_forces_stay_out_even_on_terrible_stats():
    # A catastrophic 0 % lower bound still only DOWNGRADES a label + shrinks
    # sizing. It MUST NOT emit any action/veto/STAY_OUT key.
    out = recalibrate(
        "high", "breakout", "btcDown/volHigh",
        _stats("high", ci_lo=0.0, ci_hi=0.15, sample=100), min_sample=20,
    )
    assert set(out.keys()) == {"calibrated_confidence", "size_factor", "note"}
    assert out["calibrated_confidence"] in ("low", "medium")  # a tier, never an action
    assert out["calibrated_confidence"] != "STAY_OUT"
    assert "STAY_OUT" not in str(out)
    assert "action" not in out
    # low emitted with terrible stats also never blocks — stays low, size 1.0.
    low = recalibrate(
        "low", None, None,
        _stats("low", ci_lo=0.0, ci_hi=0.10, sample=100), min_sample=20,
    )
    assert low["calibrated_confidence"] == "low"
    assert low["size_factor"] == 1.0
    assert "action" not in low


def test_fail_safe_on_empty_or_missing_stats():
    for stats in ({}, None, {"by_confidence": {}}, {"garbage": 1}):
        out = recalibrate("high", None, None, stats, min_sample=20)
        assert out["calibrated_confidence"] == "high"
        assert out["size_factor"] == 1.0
        assert out["note"] is None


def test_accepts_track_record_shape_win_rate_lo_and_n():
    # Tolerant of the compact track_record block shape (win_rate_lo + n).
    stats = {"by_confidence": {"high": {"n": 30, "win_rate_lo": 0.40}}}
    out = recalibrate("high", None, None, stats, min_sample=20)
    assert out["calibrated_confidence"] == "medium"
    assert out["size_factor"] == 0.5
