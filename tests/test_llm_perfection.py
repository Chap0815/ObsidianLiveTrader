"""Tests for the LLM-perfection pass (audit A1-A9 / B1-B6):

- scanner->analyzer verdict handoff threaded into build_llm_context (B1)
- coherence hint + price<->OI positioning label (B5/B6)
- conditional OI prompt block (B5a) and single coherent STAY_OUT DECISION (B3)
- inverted-geometry -> STAY_OUT downgrade (A4) and HTF-ATR-scaled plausibility
  warning instead of silent deletion (A5/B4)
- indicators_last dropped from the compact payload (A8)
"""

import json

from app.config import Settings
from app.llm.client import (
    annotate_proposal,
    build_llm_context,
    compact_tf_for_llm,
    parse_proposal,
)
from app.llm.prompts import build_system_prompt

VALID_BUY = {
    "htf_trend": "bullish",
    "ltf_trend": "bullish",
    "key_levels": {
        "immediate_support": 64000.0,
        "immediate_resistance": 68000.0,
        "major_liquidity_pools": [63000, 70000],
    },
    "volume_momentum": "rising on push",
    "action": "BUY",
    "trigger_entry_zone": "65000-65200",
    "entry_price": 65100.0,
    "tp1": 67000.0,
    "tp2": 68000.0,
    "tp3": None,
    "stop_loss": 64100.0,
    "rrr": 1.9,
    "recommended_leverage": "5-10x isolated",
    "position_sizing_note": "max 1% risk",
    "funding_alert": "neutral",
    "management": {"move_sl_to_be": "after TP1", "early_invalidation": "close below 64000"},
    "rationale": "HTF uptrend pullback hold.",
}


def _tf(close, e20, e50, e200, *, atr=1.0, candles=None):
    """Minimal TF slice_dict whose compact form yields a known ema_stack."""
    return {
        "tf": "x",
        "candles": candles or [{"close": close}],
        "indicators": {"last": {"ema20": e20, "ema50": e50, "ema200": e200, "atr14": atr}},
        "structure": {},
    }


_BULL = dict(close=4, e20=3, e50=2, e200=1)   # e20>e50>e200, close>e50 -> bullish
_BEAR = dict(close=0.5, e20=1, e50=2, e200=3)  # bearish


def _market(daily, htf, ltf, *, market=None):
    return {
        "symbol": "BTC",
        "last_price": ltf["candles"][-1]["close"],
        "funding": {},
        "contract": {},
        "market": market or {"open_interest": None},
        "daily": daily,
        "htf": htf,
        "ltf": ltf,
    }


# --- B1: scanner verdict threading -----------------------------------------


def test_scanner_verdict_threaded_into_context():
    mk = _market(_tf(**_BULL), _tf(**_BULL), _tf(**_BULL))
    verdict = {"bias": "long", "setup": "pullback", "key_level": 100.0, "score": 7}
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False), scanner_verdict=verdict)
    assert ctx["scanner_verdict"] == {
        "bias": "long", "setup": "pullback", "key_level": 100.0, "score": 7
    }


def test_scanner_verdict_absent_when_not_supplied():
    mk = _market(_tf(**_BULL), _tf(**_BULL), _tf(**_BULL))
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False))
    assert "scanner_verdict" not in ctx


def test_scanner_verdict_sanitizes_bad_bias():
    mk = _market(_tf(**_BULL), _tf(**_BULL), _tf(**_BULL))
    verdict = {"bias": "sideways", "setup": "pullback"}  # bad bias dropped
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False), scanner_verdict=verdict)
    assert ctx["scanner_verdict"] == {"setup": "pullback"}


# --- B6: coherence hint -----------------------------------------------------


def test_coherence_aligned_bull():
    mk = _market(_tf(**_BULL), _tf(**_BULL), _tf(**_BULL))
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False))
    coh = ctx["coherence"]
    assert coh["ema_stack_htf"] == "bullish"
    assert coh["regime_alignment"] == "aligned_bull"


def test_coherence_conflict_daily_vs_htf():
    mk = _market(_tf(**_BEAR), _tf(**_BULL), _tf(**_BULL))
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False))
    assert ctx["coherence"]["regime_alignment"] == "conflict"


# --- B5b: price<->OI positioning label -------------------------------------


def test_oi_read_label_price_up_oi_up():
    rising = [{"close": 100.0 + i} for i in range(8)]  # up over last 4
    mk = _market(
        _tf(**_BULL), _tf(**_BULL), _tf(**_BULL, candles=rising),
        market={"open_interest": 1000.0, "oi_change_pct_1h": 5.0},
    )
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False))
    assert ctx["market"]["oi_read"] == "price_up_oi_up_real_trend"


def test_oi_read_label_absent_when_oi_null():
    rising = [{"close": 100.0 + i} for i in range(8)]
    mk = _market(
        _tf(**_BULL), _tf(**_BULL), _tf(**_BULL, candles=rising),
        market={"open_interest": None, "oi_change_pct_1h": 5.0},
    )
    ctx = build_llm_context(mk, {}, Settings(include_account_in_llm=False))
    assert "oi_read" not in ctx["market"]


# --- B5a: conditional OI prompt block --------------------------------------


def test_oi_block_omitted_when_oi_null():
    p = build_system_prompt({"market": {"open_interest": None}})
    assert "Open interest (positioning)" not in p
    assert "SKIP this step" not in p


def test_oi_block_present_when_oi_present():
    p = build_system_prompt({"market": {"open_interest": 123.0}})
    assert "Open interest (positioning)" in p


def test_oi_block_present_by_default_without_context():
    assert "Open interest (positioning)" in build_system_prompt()


# --- B3: single coherent STAY_OUT / sub-min-RRR decision -------------------


def test_prompt_has_single_decision_block():
    p = build_system_prompt()
    assert "DECISION — action vs STAY_OUT" in p


def test_prompt_submin_rrr_is_a_veto_not_a_low_confidence_factor():
    p = build_system_prompt()
    # sub-min-RRR must map to STAY_OUT, never to "trade it at low"
    assert "rrr < risk_policy.min_rrr" in p
    # the old contradictory low-confidence trigger (c) is gone
    assert "the achievable rrr is below" not in p


def test_prompt_strong_action_requires_medium_confidence():
    p = build_system_prompt()
    assert "REQUIRES setup_confidence" in p


def test_prompt_mentions_scanner_handoff_and_coherence():
    p = build_system_prompt()
    assert "SCANNER HANDOFF" in p and "CONFIRM or REFUTE" in p
    assert "COHERENCE" in p and "regime_alignment" in p


# --- A4: inverted geometry -> STAY_OUT -------------------------------------


def test_inverted_buy_geometry_downgraded_to_stay_out():
    data = {**VALID_BUY, "stop_loss": 66000.0}  # stop ABOVE entry on a BUY -> inverted
    out = annotate_proposal(parse_proposal(json.dumps(data)))
    assert out.action == "STAY_OUT"
    assert out.entry_price is None and out.stop_loss is None and out.rrr is None
    assert "inverted" in out.rationale.lower()


def test_inverted_sell_geometry_downgraded_to_stay_out():
    data = {
        **VALID_BUY, "action": "SELL",
        "entry_price": 100.0, "tp1": 110.0, "stop_loss": 95.0,  # TP above entry on a SELL
    }
    out = annotate_proposal(parse_proposal(json.dumps(data)))
    assert out.action == "STAY_OUT"


def test_incomplete_geometry_nulls_rrr_but_keeps_action():
    """A merely-missing TP leg must NOT be downgraded — only its rrr is cleared."""
    data = {**VALID_BUY, "tp1": None, "rrr": 2.0}
    out = annotate_proposal(parse_proposal(json.dumps(data)))
    assert out.action == "BUY"
    assert out.rrr is None


# --- A5/B4: HTF-ATR-scaled plausibility (warn, don't silently delete) ------


def _ctx(last, ltf_atr, htf_atr=None):
    ctx = {"last_price": last, "ltf": {"read": {"atr14": ltf_atr}}}
    if htf_atr is not None:
        ctx["htf"] = {"read": {"atr14": htf_atr}}
    return ctx


def test_deep_limit_entry_within_htf_atr_warns_not_stay_out():
    data = {**VALID_BUY, "entry_price": 95.0, "stop_loss": 93.0, "tp1": 105.0}
    out = annotate_proposal(parse_proposal(json.dumps(data)), _ctx(100.0, 1.0, htf_atr=10.0))
    assert out.action == "BUY"            # kept, not deleted
    assert out.entry_price == 95.0
    assert out.setup_confidence == "low"  # confidence capped
    assert "Warning" in out.rationale


def test_far_limit_entry_without_htf_atr_still_hard_stay_out():
    data = {**VALID_BUY, "entry_price": 95.0, "stop_loss": 93.0, "tp1": 105.0}
    out = annotate_proposal(parse_proposal(json.dumps(data)), _ctx(100.0, 1.0))
    assert out.action == "STAY_OUT"


def test_hallucinated_entry_hard_stay_out_even_with_htf_atr():
    """Beyond 3x even the HTF ATR is a hallucination, not a deep pullback."""
    data = {**VALID_BUY, "entry_price": 1000.0, "stop_loss": 990.0, "tp1": 1100.0}
    out = annotate_proposal(parse_proposal(json.dumps(data)), _ctx(100.0, 1.0, htf_atr=10.0))
    assert out.action == "STAY_OUT"


# --- A8: indicators_last dropped -------------------------------------------


def test_compact_tf_no_longer_emits_indicators_last():
    out = compact_tf_for_llm(_tf(**_BULL))
    assert "indicators_last" not in out
    assert "read" in out and "indicators_tail" in out


# --- B3/I2: daily ATR feeds the plausibility reference band ----------------


def test_deep_daily_anchored_entry_survives_via_daily_atr():
    """A deep daily-pullback limit that is far in LTF/HTF ATR terms but well
    inside the DAILY ATR band must be KEPT (warn + low), not auto-STAY_OUT."""
    data = {**VALID_BUY, "entry_price": 85.0, "stop_loss": 83.0, "tp1": 120.0}
    ctx = {
        "last_price": 100.0,
        "ltf": {"read": {"atr14": 1.0}},
        "htf": {"read": {"atr14": 2.0}},
        "daily": {"read": {"atr14": 8.0}},  # 15 away = 1.9x daily ATR -> ok
    }
    out = annotate_proposal(parse_proposal(json.dumps(data)), ctx)
    assert out.action == "BUY"            # kept, not nuked
    assert out.entry_price == 85.0
    assert out.setup_confidence == "low"  # capped, with a warning
    assert "Warning" in out.rationale


def test_same_deep_entry_without_daily_atr_is_hard_stay_out():
    """Control: identical entry with NO daily ATR (only LTF/HTF) is beyond 3x
    the reference band and is correctly hard-downgraded to STAY_OUT."""
    data = {**VALID_BUY, "entry_price": 85.0, "stop_loss": 83.0, "tp1": 120.0}
    ctx = {
        "last_price": 100.0,
        "ltf": {"read": {"atr14": 1.0}},
        "htf": {"read": {"atr14": 2.0}},
    }
    out = annotate_proposal(parse_proposal(json.dumps(data)), ctx)
    assert out.action == "STAY_OUT"


def test_compact_daily_read_includes_atr14():
    from app.llm.client import compact_daily_for_llm

    slice_dict = {
        "tf": "1D",
        "candles": [{"close": 110.0}],
        "indicators": {"last": {"ema20": 100.0, "ema50": 95.0, "ema200": 90.0, "atr14": 7.5}},
        "structure": {},
    }
    assert compact_daily_for_llm(slice_dict)["read"]["atr14"] == 7.5


# --- F2/F3/I4: prompt disambiguation + OI counting confluence --------------


def test_prompt_confluence_counts_colocated_signal_once():
    p = build_system_prompt()
    assert "counts ONCE" in p and "ONE confluence, not two" in p


def test_prompt_uses_stop_anchor_not_overloaded_invalidation():
    p = build_system_prompt()
    # DECISION veto #2 is now the disambiguated 'stop-anchor', and the handoff
    # list matches it — the old overloaded 'no clean invalidation' is gone.
    assert "no valid stop-anchor" in p
    assert "no clean invalidation" not in p
    # and a null early-invalidation must NOT be read as a STAY_OUT trigger
    assert "does NOT by itself force STAY_OUT" in p


def test_oi_step_counts_confirming_oi_as_confluence():
    p = build_system_prompt({"market": {"open_interest": 123.0}})
    assert "one independent positioning confluence" in p
    # ...and stays absent from the null-OI prompt (no wasted instruction)
    assert "one independent positioning confluence" not in build_system_prompt(
        {"market": {"open_interest": None}}
    )
