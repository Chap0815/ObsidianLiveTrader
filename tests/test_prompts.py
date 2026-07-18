"""Task 14a — prompt structure / dedup / no-chase-relation tests.

These lock in the structural refactor of app/llm/prompts.py:
 - the authoritative DECISION block is delimited by an XML `<decision_policy>` tag;
 - the RRR-band rule is DEFINED exactly once (other mentions are references);
 - the no-chase 0.5x / 0.85x constants are one coherent rule.
"""
from __future__ import annotations

from app.llm.prompts import build_system_prompt


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
