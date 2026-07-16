"""Regression tests for the LLM proposal audit fixes:

1. setup_confidence field (schema + tolerant parsing).
2. Salvage/truncation recovery for the main proposal JSON parse path.
3. Server-side price plausibility check (entry/SL vs last_price/ATR).
4. Prompt/schema consistency for the new fields.
"""

import json

import pytest
from pydantic import ValidationError

from app.llm.client import (
    annotate_proposal,
    parse_proposal,
    salvage_proposal_json,
)
from app.llm.prompts import build_system_prompt
from app.models import TradeProposal

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
    "management": {
        "move_sl_to_be": "after TP1",
        "early_invalidation": "close below 64000",
    },
    "rationale": "HTF uptrend pullback hold.",
}


# --- setup_confidence field ------------------------------------------------


def test_setup_confidence_defaults_to_medium_when_missing():
    p = parse_proposal(json.dumps(VALID_BUY))
    assert p.setup_confidence == "medium"


@pytest.mark.parametrize("value", ["low", "medium", "high"])
def test_setup_confidence_accepts_valid_values(value):
    data = {**VALID_BUY, "setup_confidence": value}
    p = parse_proposal(json.dumps(data))
    assert p.setup_confidence == value


def test_setup_confidence_case_insensitive_normalization():
    data = {**VALID_BUY, "setup_confidence": "LOW"}
    p = parse_proposal(json.dumps(data))
    assert p.setup_confidence == "low"


def test_setup_confidence_invalid_value_falls_back_to_default():
    """An out-of-enum value from a sloppy LLM must not hard-fail parsing."""
    data = {**VALID_BUY, "setup_confidence": "extremely-high"}
    p = parse_proposal(json.dumps(data))
    assert p.setup_confidence == "medium"


def test_model_direct_rejects_invalid_literal():
    """Pydantic model itself still enforces the enum when constructed directly
    (the tolerant coercion lives in parse_proposal, not the schema)."""
    with pytest.raises(ValidationError):
        TradeProposal(
            htf_trend="bullish",
            ltf_trend="bullish",
            action="STAY_OUT",
            setup_confidence="nonsense",
        )


# --- invalidation_price / invalidation_tf (optional structured fields) -----


def test_invalidation_fields_default_null_and_empty():
    p = parse_proposal(json.dumps(VALID_BUY))
    assert p.invalidation_price is None
    assert p.invalidation_tf == ""


def test_invalidation_fields_round_trip():
    data = {**VALID_BUY, "invalidation_price": 63500.0, "invalidation_tf": "15m"}
    p = parse_proposal(json.dumps(data))
    assert p.invalidation_price == 63500.0
    assert p.invalidation_tf == "15m"


# --- truncated main-proposal JSON salvage ----------------------------------


def test_salvage_truncated_at_last_field_recovers_core_fields():
    """Simulates a max_tokens cutoff mid-rationale: earlier fields must survive."""
    full = json.dumps(VALID_BUY)
    cut_idx = full.index('"rationale"')
    truncated = full[:cut_idx] + '"rationale": "HTF uptrend pullback ho'
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)  # sanity: this really is broken JSON
    p = parse_proposal(truncated)
    assert p.action == "BUY"
    assert p.entry_price == 65100.0
    assert p.stop_loss == 64100.0
    assert p.tp1 == 67000.0
    assert p.rationale == ""  # dropped incomplete field -> model default


def test_salvage_truncated_mid_nested_object_drops_whole_key():
    """Cutoff inside a nested object (management) drops that key cleanly,
    still recovering everything before it."""
    full = json.dumps(VALID_BUY)
    cut_idx = full.index('"management"')
    truncated = full[:cut_idx] + '"management": {"move_sl_to_be": "after TP1"'
    p = parse_proposal(truncated)
    assert p.action == "BUY"
    assert p.entry_price == 65100.0
    assert p.management.move_sl_to_be == ""  # whole incomplete key dropped


def test_salvage_proposal_json_returns_none_when_unrecoverable():
    assert salvage_proposal_json('{"only_key": "cut off mid str') is None


def test_unrecoverable_truncation_still_raises_jsondecodeerror():
    with pytest.raises(json.JSONDecodeError):
        parse_proposal('{"only_key": "cut off mid str')


# --- price plausibility (entry/SL vs last_price/ATR) -----------------------


def _ctx(last_price: float, atr: float) -> dict:
    return {"last_price": last_price, "ltf": {"read": {"atr14": atr}}}


def test_price_plausibility_entry_far_from_last_price_downgrades():
    p = parse_proposal(json.dumps(VALID_BUY))  # entry 65100, atr small below
    out = annotate_proposal(p, _ctx(last_price=50000.0, atr=100.0))  # 151x ATR away
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"
    assert out.entry_price is None
    assert out.stop_loss is None
    assert out.rrr is None
    assert "Auto-downgraded" in out.rationale


def test_price_plausibility_sl_too_tight_is_warn_not_stayout():
    # L-04: entry 65100, sl 64100 -> distance 1000; ATR huge so 1000 <
    # 0.3*ATR. An unusably tight SL is as often a legitimate tight scalp as a
    # hallucination, so this is now a WARN (kept, confidence capped at
    # "medium"), not a hard STAY_OUT.
    p = parse_proposal(json.dumps(VALID_BUY))
    out = annotate_proposal(p, _ctx(last_price=65100.0, atr=10000.0))
    assert out.action == "BUY"
    assert out.entry_price == 65100.0
    assert out.setup_confidence == "medium"
    assert "Warning" in out.rationale


def test_price_plausibility_sl_too_wide_downgrades():
    # entry 65100, sl 64100 -> distance 1000; tiny ATR so 1000 > 8*ATR (L-04
    # raised the hard wide-SL cutoff from 5x to 8x ref ATR) -- still a real
    # STAY_OUT at this magnitude.
    p = parse_proposal(json.dumps(VALID_BUY))
    out = annotate_proposal(p, _ctx(last_price=65100.0, atr=10.0))
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"


def test_tight_sl_is_warn_not_stayout():
    """L-04 (task-11 brief): sl_dist just under 0.3x LTF ATR keeps the
    proposal directional (WARN), confidence capped at <= 'medium', not a
    hard STAY_OUT."""
    data = {**VALID_BUY, "stop_loss": 64810.0}  # |65100-64810|=290 < 0.3*1000
    p = parse_proposal(json.dumps(data))
    out = annotate_proposal(p, _ctx(last_price=65100.0, atr=1000.0))
    assert out.action == "BUY"
    assert out.entry_price == 65100.0
    assert out.stop_loss == 64810.0
    assert out.setup_confidence in ("low", "medium")
    assert "Warning" in out.rationale


def test_price_plausibility_within_thresholds_not_downgraded():
    # entry 65100 vs last_price 65000 (100 away), sl distance 1000; ATR 300 ->
    # entry dist 100 < 3*300=900 OK; sl dist 1000 within [0.3*300=90, 8*300=2400]
    p = parse_proposal(json.dumps(VALID_BUY))
    out = annotate_proposal(p, _ctx(last_price=65000.0, atr=300.0))
    assert out.action == "BUY"
    assert out.entry_price == 65100.0


def test_price_plausibility_skips_stay_out_proposals():
    stay_out = {
        **VALID_BUY,
        "action": "STAY_OUT",
        "entry_price": None,
        "tp1": None,
        "tp2": None,
        "tp3": None,
        "stop_loss": None,
        "rrr": None,
    }
    p = parse_proposal(json.dumps(stay_out))
    out = annotate_proposal(p, _ctx(last_price=1.0, atr=0.01))
    assert out.action == "STAY_OUT"


def test_price_plausibility_skipped_without_context():
    """Backward compat: annotate_proposal(p) with no context must not downgrade
    (existing callers/tests rely on the single-arg form)."""
    p = parse_proposal(json.dumps(VALID_BUY))
    out = annotate_proposal(p)
    assert out.action == "BUY"


def test_price_plausibility_missing_atr_or_last_price_skips_check():
    p = parse_proposal(json.dumps(VALID_BUY))
    out = annotate_proposal(p, {"last_price": None, "ltf": {"read": {}}})
    assert out.action == "BUY"


# --- prompt / schema consistency -------------------------------------------


def test_prompt_schema_mentions_setup_confidence():
    prompt = build_system_prompt()
    assert "setup_confidence" in prompt
    assert '"low|medium|high"' in prompt


def test_prompt_schema_mentions_invalidation_fields():
    prompt = build_system_prompt()
    assert "invalidation_price" in prompt
    assert "invalidation_tf" in prompt


def test_prompt_no_longer_calls_low_rrr_tradeable_unconditionally():
    """Regression for the RRR/prompt contradiction: the prompt must not tell
    the LLM a below-minimum-RRR setup is simply 'tradeable' without
    qualification (that's what caused the BNB rrr=1.75 gate rejection)."""
    prompt = build_system_prompt()
    assert "is still tradeable — propose it and state the rrr" not in prompt
    assert "STAY_OUT" in prompt and "min_rrr" in prompt
