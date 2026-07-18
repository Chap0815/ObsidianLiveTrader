"""Task 14a — prompt structure / dedup / no-chase-relation tests.

These lock in the structural refactor of app/llm/prompts.py:
 - the authoritative DECISION block is delimited by an XML `<decision_policy>` tag;
 - the RRR-band rule is DEFINED exactly once (other mentions are references);
 - the no-chase 0.5x / 0.85x constants are one coherent rule.
"""
from __future__ import annotations

from app.llm.prompts import build_reevaluate_system_prompt, build_system_prompt


def test_prompt_has_decision_policy_tag():
    p = build_system_prompt()
    assert '<decision_policy authoritative="true">' in p
    assert "</decision_policy>" in p


def test_rrr_band_defined_once():
    p = build_system_prompt()
    # the canonical RRR-band rule is labelled once; step 5 / Rule 9 only reference it
    assert p.count("RRR-BAND RULE (authoritative") == 1
    # its core veto sentence is stated exactly once (elsewhere it is a reference)
    assert p.count("the ONLY rrr veto") == 1


def test_no_chase_relates_fresh_trigger_to_veto():
    p = build_system_prompt()
    # both constants are present...
    assert "0.5 x LTF ATR14" in p
    assert "0.85 x LTF ATR14" in p
    # ...and explicitly related: 0.5x defines the fresh trigger used by the veto
    assert "fresh trigger" in p and 'what "fresh trigger" means in\n   decision_policy' in p


# --- Task 14b — calibration / anti-overtrading / new schema fields ---


def test_confidence_is_derived_not_defaulted():
    """P2-05 + P2-12: no 'Default medium' anchor in either prompt; confidence
    and the numeric conviction_score are DERIVED from the evidence."""
    p = build_system_prompt()
    r = build_reevaluate_system_prompt()
    # the mode-collapse anchor is gone from BOTH prompts
    assert 'Default "medium"' not in p
    assert "Default medium" not in p
    assert 'Default "medium"' not in r
    assert "Default medium" not in r
    # analysis prompt derives confidence and exposes the numeric score
    assert "conviction_score" in p
    assert 'Do NOT anchor on "medium"' in p
    # score->label mapping is spelled out
    assert '0-3' in p and '4-6' in p and '7-10' in p
    # reevaluate prompt also refuses to default to medium
    assert 'do NOT default or anchor on "medium"' in r


def test_single_protrade_nudge_with_sizing():
    """P2-06 + P2-14: exactly ONE pro-trade nudge, coupled to a sizing ladder;
    the four other 'do not hide in STAY_OUT' nudges are gone."""
    p = build_system_prompt()
    # exactly one canonical nudge (other mentions are references to it)
    assert p.count("SIZE TO CONVICTION (the single pro-trade nudge") == 1
    dp = p.split('<decision_policy')[1].split("</decision_policy>")[0]
    assert "SIZE TO CONVICTION (the single pro-trade nudge" in dp
    # sizing ladder named across the three tiers
    assert "full risk budget" in p
    assert "starter" in p
    assert "position_sizing_note" in p
    # the redundant hortatory nudge is fully removed (no surviving duplicates)
    assert "do NOT hide in STAY_OUT" not in p


def test_schema_has_confluences_and_new_fields():
    """P2-07 + P2-08: new Pydantic fields exist (Optional) and the prompt schema
    documents them."""
    from app.models import Confluence, TradeProposal

    fields = TradeProposal.model_fields
    for name in ("confluences", "conviction_score", "alternative_scenario", "time_horizon"):
        assert name in fields, name
    # confluences carry type + evidence
    cf = Confluence.model_fields
    assert "type" in cf and "evidence" in cf
    # new fields are Optional so old parse paths (missing field) still validate
    tp = TradeProposal(htf_trend="ranging", ltf_trend="ranging", action="STAY_OUT")
    assert tp.confluences == []
    assert tp.conviction_score is None
    assert tp.alternative_scenario is None
    assert tp.time_horizon is None
    # prompt schema documents the new fields
    p = build_system_prompt()
    for name in ("confluences", "conviction_score", "alternative_scenario", "time_horizon"):
        assert name in p, name
