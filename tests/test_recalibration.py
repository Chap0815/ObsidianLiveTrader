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
    assert "calibrated: medium" in out["note"]
    assert "46% WR" in out["note"]
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
    assert out["note"] == "insufficient data (n=5)"


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


def test_fail_safe_on_overflowed_stats_numbers():
    cases = [
        {"sample": float("inf"), "win_rate_ci95": [0.2, 0.4]},
        {"sample": 40, "win_rate_lo": 10**400},
    ]
    for block in cases:
        stats = {"by_confidence": {"high": block}}
        out = recalibrate("high", None, None, stats, min_sample=20)
        assert out["calibrated_confidence"] == "high"
        assert out["size_factor"] == 1.0


def test_fail_safe_on_impossible_wilson_lower_bound():
    for bad_lower_bound in (-0.1, 1.1, float("nan"), float("inf"), float("-inf")):
        out = recalibrate(
            "high",
            None,
            None,
            _stats("high", ci_lo=bad_lower_bound, ci_hi=0.99, sample=40),
            min_sample=20,
        )
        assert out["calibrated_confidence"] == "high"
        assert out["size_factor"] == 1.0


def test_accepts_track_record_shape_win_rate_lo_and_n():
    # Tolerant of the compact track_record block shape (win_rate_lo + n).
    stats = {"by_confidence": {"high": {"n": 30, "win_rate_lo": 0.40}}}
    out = recalibrate("high", None, None, stats, min_sample=20)
    assert out["calibrated_confidence"] == "medium"
    assert out["size_factor"] == 0.5


# --------------------------------------------------------------------------- #
# Block 2/TP2 P2b — REGIME axis (regime-aware recalibration).
# The confidence axis keys on by_confidence[emitted]; the NEW regime axis keys
# on by_regime[<current live regime tag>] against _REGIME_FLOOR (0.45). Same
# hard line: downgrade-only, size <= 1.0, transparent note, never an action.
# --------------------------------------------------------------------------- #


def _stats_cr(
    *,
    conf_tier: str,
    conf_lo: float,
    conf_n: int,
    regime: str,
    reg_lo: float,
    reg_n: int,
) -> dict:
    """A build_stats_response-shaped dict carrying BOTH a confidence group and a
    regime group (win_rate_ci95 lower bound + sample), which is what the two
    recalibrate axes read."""
    return {
        "by_confidence": {conf_tier: {"sample": conf_n, "win_rate_ci95": [conf_lo, 0.99]}},
        "by_regime": {regime: {"sample": reg_n, "win_rate_ci95": [reg_lo, 0.99]}},
    }


def test_regime_axis_alone_downgrades_and_cuts_size():
    # Confidence group is STRONG (0.60 >= 0.50) yet the CURRENT regime's realized
    # WR-LB is weak (0.30 < 0.45, n=30) -> the regime axis alone downgrades the
    # DISPLAY high->medium and cuts the sizing SUGGESTION, even though the tier
    # group looks fine on its own. action untouched.
    out = recalibrate(
        "high", "breakout", "btcDown/volHigh",
        _stats_cr(conf_tier="high", conf_lo=0.60, conf_n=40,
                  regime="btcDown/volHigh", reg_lo=0.30, reg_n=30),
        min_sample=20,
    )
    assert out["calibrated_confidence"] == "medium"
    assert out["size_factor"] == 0.5
    assert "Regime btcDown/volHigh" in out["note"]
    assert "30% WR" in out["note"]
    assert "n=30" in out["note"]
    # confidence-group clause must NOT appear (that axis did not fire)
    assert "your high setups" not in out["note"]
    assert "action" not in out


def test_confidence_axis_alone_unchanged_when_regime_strong():
    # Confidence weak (0.46 < 0.50) but the current regime is STRONG (0.70) ->
    # only the confidence axis fires; note is byte-identical to the pre-regime
    # behaviour (no regime clause).
    out = recalibrate(
        "high", "breakout", "btcUp/volNormal",
        _stats_cr(conf_tier="high", conf_lo=0.46, conf_n=40,
                  regime="btcUp/volNormal", reg_lo=0.70, reg_n=40),
        min_sample=20,
    )
    assert out["calibrated_confidence"] == "medium"
    assert out["size_factor"] == 0.5
    assert "your high setups overall: 46% WR" in out["note"]
    assert "Regime" not in out["note"]


def test_both_axes_weak_single_tier_step_and_min_size_floor():
    # BOTH axes weak -> still only ONE tier step (high->medium, never straight to
    # low), size_factor = min of the two axis factors (>= hard floor 0.25), and
    # the note names BOTH triggering axes honestly.
    out = recalibrate(
        "high", "breakout", "btcDown/volHigh",
        _stats_cr(conf_tier="high", conf_lo=0.40, conf_n=40,
                  regime="btcDown/volHigh", reg_lo=0.20, reg_n=40),
        min_sample=20,
    )
    assert out["calibrated_confidence"] == "medium"  # ONE step, not "low"
    assert out["calibrated_confidence"] != "low"
    assert out["size_factor"] == 0.5
    assert out["size_factor"] >= 0.25
    assert "your high setups" in out["note"]
    assert "Regime btcDown/volHigh" in out["note"]


def test_regime_below_min_sample_has_no_effect():
    # Regime group is weak but under-sampled (n=5 < 20) -> regime axis inert;
    # confidence group strong -> raw KI tier, size 1.0 (exact old behaviour).
    out = recalibrate(
        "high", "breakout", "btcDown/volHigh",
        _stats_cr(conf_tier="high", conf_lo=0.60, conf_n=40,
                  regime="btcDown/volHigh", reg_lo=0.10, reg_n=5),
        min_sample=20,
    )
    assert out["calibrated_confidence"] == "high"
    assert out["size_factor"] == 1.0
    assert out["note"] is None


def test_emitted_low_never_below_low_even_with_weak_regime():
    # emitted "low" + catastrophic regime -> label cannot go below low; size 1.0.
    out = recalibrate(
        "low", "breakout", "btcDown/volHigh",
        _stats_cr(conf_tier="low", conf_lo=0.0, conf_n=40,
                  regime="btcDown/volHigh", reg_lo=0.05, reg_n=40),
        min_sample=20,
    )
    assert out["calibrated_confidence"] == "low"
    assert out["size_factor"] == 1.0


def test_regime_axis_fail_safe_broken_or_unknown():
    # Broken/absent by_regime and the "unknown" / None regime tag must all be
    # inert (never crash), leaving the strong-confidence output raw.
    strong_conf = {"by_confidence": {"high": {"sample": 40, "win_rate_ci95": [0.60, 0.99]}}}
    cases = [
        ("btcDown/volHigh", {**strong_conf, "by_regime": ["not", "a", "dict"]}),
        ("btcDown/volHigh", {**strong_conf, "by_regime": None}),
        ("btcDown/volHigh", {**strong_conf, "by_regime": {"btcDown/volHigh": "broken"}}),
        ("unknown", {**strong_conf, "by_regime": {"unknown": {"sample": 40, "win_rate_ci95": [0.0, 0.1]}}}),
        (None, strong_conf),
    ]
    for regime, stats in cases:
        out = recalibrate("high", "breakout", regime, stats, min_sample=20)
        assert out["calibrated_confidence"] == "high"
        assert out["size_factor"] == 1.0
        assert "action" not in out


def test_regime_axis_fires_even_when_confidence_group_undersampled():
    # The two axes are INDEPENDENT: a weak regime (n=40) downgrades even while
    # the confidence group is under-sampled (n=5) -> no "zu wenig Daten" note,
    # the regime axis carries it.
    out = recalibrate(
        "high", "breakout", "btcDown/volHigh",
        _stats_cr(conf_tier="high", conf_lo=0.10, conf_n=5,
                  regime="btcDown/volHigh", reg_lo=0.20, reg_n=40),
        min_sample=20,
    )
    assert out["calibrated_confidence"] == "medium"
    assert out["size_factor"] == 0.5
    assert "Regime btcDown/volHigh" in out["note"]
    assert "insufficient data" not in out["note"]
