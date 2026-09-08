"""Unit tests for Grok TradeProposal parsing (no live xAI calls)."""

import json

import pytest
from pydantic import ValidationError

from app.llm.grok import (
    annotate_proposal,
    compute_simple_rrr,
    parse_proposal,
    strip_markdown_fences,
)
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

VALID_STAY_OUT = {
    "htf_trend": "ranging",
    "ltf_trend": "ranging",
    "key_levels": {
        "immediate_support": None,
        "immediate_resistance": None,
        "major_liquidity_pools": [],
    },
    "volume_momentum": "flat",
    "action": "STAY_OUT",
    "trigger_entry_zone": "",
    "entry_price": None,
    "tp1": None,
    "tp2": None,
    "tp3": None,
    "stop_loss": None,
    "rrr": None,
    "recommended_leverage": "",
    "position_sizing_note": "no trade",
    "funding_alert": "",
    "management": {"move_sl_to_be": "", "early_invalidation": ""},
    "rationale": "Choppy range, poor RRR.",
}


def test_parse_valid_json():
    p = parse_proposal(json.dumps(VALID_BUY))
    assert isinstance(p, TradeProposal)
    assert p.action == "BUY"
    assert p.htf_trend == "bullish"
    assert p.entry_price == 65100.0
    assert p.stop_loss == 64100.0
    assert p.key_levels.immediate_support == 64000.0


def test_parse_fenced_json():
    raw = "```json\n" + json.dumps(VALID_BUY) + "\n```"
    p = parse_proposal(raw)
    assert p.action == "BUY"
    assert p.ltf_trend == "bullish"


def test_parse_fenced_without_lang():
    raw = "```\n" + json.dumps(VALID_STAY_OUT) + "\n```"
    p = parse_proposal(raw)
    assert p.action == "STAY_OUT"
    assert p.entry_price is None
    assert p.tp1 is None
    assert p.stop_loss is None
    assert p.rrr is None


def test_stay_out_nulls_allowed():
    p = parse_proposal(json.dumps(VALID_STAY_OUT))
    assert p.action == "STAY_OUT"
    assert p.entry_price is None
    assert p.tp1 is None
    assert p.tp2 is None
    assert p.tp3 is None
    assert p.stop_loss is None


def test_missing_required_fields_raises():
    bad = {"action": "BUY"}  # missing trends
    with pytest.raises(ValidationError):
        parse_proposal(json.dumps(bad))


def test_invalid_action_enum():
    bad = {**VALID_BUY, "action": "HOLD"}
    with pytest.raises(ValidationError):
        parse_proposal(json.dumps(bad))


def test_invalid_trend_enum():
    bad = {**VALID_BUY, "htf_trend": "up"}
    with pytest.raises(ValidationError):
        parse_proposal(json.dumps(bad))


def test_invalid_json_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_proposal("not json at all")


def test_strip_fences_plain():
    s = '{"a": 1}'
    assert strip_markdown_fences(s) == s


def test_all_actions_accepted():
    for action in ("STRONG_BUY", "BUY", "STAY_OUT", "SELL", "STRONG_SHORT"):
        data = {**VALID_STAY_OUT, "action": action}
        if action != "STAY_OUT":
            data = {
                **VALID_BUY,
                "action": action,
                "htf_trend": "bearish" if "SELL" in action or "SHORT" in action else "bullish",
                "ltf_trend": "bearish" if "SELL" in action or "SHORT" in action else "bullish",
            }
        p = parse_proposal(json.dumps(data))
        assert p.action == action


def test_compute_simple_rrr_long():
    # entry 100, sl 90, tp 120 → risk 10, reward 20 → 2.0
    assert compute_simple_rrr(100.0, 90.0, 120.0) == pytest.approx(2.0)
    assert compute_simple_rrr(100.0, 90.0, 120.0, action="BUY") == pytest.approx(2.0)


def test_compute_simple_rrr_short():
    # entry 100, sl 110, tp 80 → risk 10, reward 20 → 2.0
    assert compute_simple_rrr(100.0, 110.0, 80.0) == pytest.approx(2.0)
    assert compute_simple_rrr(100.0, 110.0, 80.0, action="SELL") == pytest.approx(2.0)


def test_compute_simple_rrr_overflow_cannot_escape_annotation():
    entry = 1e-308
    stop = 5e-324
    target = 1e308

    assert compute_simple_rrr(entry, stop, target, action="BUY") is None

    proposal = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=entry,
        stop_loss=stop,
        tp1=target,
        rrr=2.0,
    )
    annotated = annotate_proposal(proposal)

    assert annotated.action == "STAY_OUT"
    assert annotated.rrr is None


def test_compute_simple_rrr_missing():
    assert compute_simple_rrr(None, 90.0, 120.0) is None
    assert compute_simple_rrr(100.0, None, 120.0) is None
    assert compute_simple_rrr(100.0, 90.0, None) is None


def test_compute_simple_rrr_same_side_invalid():
    # SL and TP both below entry — abs() would lie; must be None
    assert compute_simple_rrr(100.0, 90.0, 80.0) is None
    assert compute_simple_rrr(100.0, 90.0, 80.0, action="BUY") is None
    # BUY with SL above entry
    assert compute_simple_rrr(100.0, 110.0, 120.0, action="BUY") is None


def test_annotate_overwrites_rrr():
    p = parse_proposal(json.dumps({**VALID_BUY, "rrr": 99.0}))
    # entry 65100, sl 64100 (risk 1000), tp1 67000 (reward 1900) → 1.9
    out = annotate_proposal(p)
    assert out.rrr == pytest.approx(1.9)


def test_annotate_clears_rrr_on_bad_geometry():
    bad = {
        **VALID_BUY,
        "stop_loss": 66000.0,  # above entry for BUY
        "tp1": 67000.0,
        "rrr": 3.0,
    }
    p = parse_proposal(json.dumps(bad))
    out = annotate_proposal(p)
    assert out.rrr is None


def test_annotate_stay_out_no_rrr():
    p = parse_proposal(json.dumps(VALID_STAY_OUT))
    out = annotate_proposal(p)
    assert out.rrr is None


# ── Inverted SL side must downgrade even when tp1 is missing (audit A4 gap) ──
# A directional call with the stop on the WRONG side of entry is untradeable
# (the apply-path risk gate blocks it), so it must be downgraded to STAY_OUT
# and NOT surfaced as a full-confidence BUY/SELL — even if tp1 is None and
# compute_simple_rrr therefore can't run.


def test_annotate_inverted_sl_buy_no_tp1_downgrades():
    p = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100.0,
        stop_loss=105.0,  # BUY stop ABOVE entry -> inverted
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"
    assert out.entry_price is None and out.stop_loss is None
    assert out.rrr is None
    assert "inverted" in out.rationale.lower()


def test_annotate_inverted_sl_sell_no_tp1_downgrades():
    p = TradeProposal(
        htf_trend="bearish",
        ltf_trend="bearish",
        action="SELL",
        entry_price=100.0,
        stop_loss=95.0,  # SELL stop BELOW entry -> inverted
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"
    assert "inverted" in out.rationale.lower()


def test_annotate_legit_sl_no_tp1_unchanged():
    # BUY with a correctly-placed stop below entry and tp1 missing: an
    # incomplete-geometry proposal, NOT inverted -> keep it directional.
    p = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "BUY"
    assert out.setup_confidence == "high"
    assert out.entry_price == 100.0 and out.stop_loss == 95.0


def test_annotate_inverted_sl_with_tp1_still_downgrades():
    # Existing A4 path (all three legs set, inverted) must be unchanged.
    p = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100.0,
        stop_loss=105.0,
        tp1=110.0,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"
    assert out.rrr is None


def test_annotate_inverted_sl_strong_buy_no_tp1_downgrades():
    # Guard the STRONG_* enum coverage of _DIRECTIONAL: a STRONG_BUY with the
    # stop above entry must be caught exactly like a plain BUY.
    p = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="STRONG_BUY",
        entry_price=100.0,
        stop_loss=105.0,  # STRONG_BUY stop ABOVE entry -> inverted
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"
    assert "inverted" in out.rationale.lower()


def test_annotate_inverted_sl_strong_short_no_tp1_downgrades():
    p = TradeProposal(
        htf_trend="bearish",
        ltf_trend="bearish",
        action="STRONG_SHORT",
        entry_price=100.0,
        stop_loss=95.0,  # STRONG_SHORT stop BELOW entry -> inverted
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "STAY_OUT"
    assert out.setup_confidence == "low"
    assert "inverted" in out.rationale.lower()


def test_annotate_legit_sl_strong_short_no_tp1_unchanged():
    # STRONG_SHORT with a correctly-placed stop ABOVE entry must NOT downgrade.
    p = TradeProposal(
        htf_trend="bearish",
        ltf_trend="bearish",
        action="STRONG_SHORT",
        entry_price=100.0,
        stop_loss=105.0,
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "STRONG_SHORT"
    assert out.setup_confidence == "high"


def test_annotate_entry_equals_sl_no_tp1_not_downgraded():
    # Degenerate (entry == stop) with tp1 missing is NOT an unambiguous
    # inversion -> fail-safe: do not downgrade a possibly-legit proposal.
    p = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100.0,
        stop_loss=100.0,
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "BUY"


def test_annotate_missing_sl_no_tp1_not_downgraded():
    # No stop at all -> cannot determine inversion -> leave directional (fail-safe).
    p = TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100.0,
        stop_loss=None,
        tp1=None,
        setup_confidence="high",
        rationale="thesis",
    )
    out = annotate_proposal(p)
    assert out.action == "BUY"


def test_extract_json_trailing_prose():
    from app.llm.client import extract_json_object, parse_proposal

    raw = json.dumps(VALID_BUY) + "\n\nHope this helps!"
    p = parse_proposal(raw)
    assert p.action == "BUY"
    assert extract_json_object("Here you go:\n" + json.dumps(VALID_STAY_OUT) + "\nend")[
        :1
    ] == "{"


def test_build_llm_context_htf_before_ltf():
    from app.config import Settings
    from app.llm.client import build_llm_context

    market = {
        "symbol": "BTC",
        "last_price": 100.0,
        "funding": {},
        "contract": {},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }
    ctx = build_llm_context(market, {}, Settings(include_account_in_llm=False))
    keys = list(ctx.keys())
    assert keys.index("htf") < keys.index("ltf")


# ── NaN/Inf on untrusted LLM geometry must be rejected (advisory-net bypass) ──


@pytest.mark.parametrize("field", ["entry_price", "tp1", "tp2", "tp3", "stop_loss", "rrr", "invalidation_price"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_trade_proposal_rejects_nonfinite_geometry(field, bad):
    """A NaN/Inf geometry leg from the model would SILENTLY bypass the STAY_OUT
    downgrade nets (every NaN comparison is False). The model must reject it so
    it surfaces as a clean LlmError, never a fake-tradeable proposal."""
    data = dict(VALID_BUY)
    data[field] = bad
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(data)


def test_parse_proposal_rejects_nan_token_from_json():
    """stdlib json.loads accepts the bare NaN token — parse_proposal must still
    reject it via the model validator (not carry a NaN proposal downstream)."""
    raw = json.dumps(VALID_BUY).replace("64100.0", "NaN")  # stop_loss -> NaN
    assert "NaN" in raw
    with pytest.raises(ValidationError):
        parse_proposal(raw)


def test_trade_proposal_accepts_finite_and_none():
    """No false positives: the valid finite BUY still parses, a very-large finite
    level passes, and a STAY_OUT with all-None geometry is untouched."""
    assert TradeProposal.model_validate(VALID_BUY).action == "BUY"
    big = dict(VALID_BUY, entry_price=1e12, tp1=1.1e12, stop_loss=0.9e12)
    assert TradeProposal.model_validate(big).entry_price == 1e12  # large finite OK
    p = TradeProposal.model_validate(VALID_STAY_OUT)
    assert p.entry_price is None and p.stop_loss is None


@pytest.mark.parametrize(
    "field",
    [
        "conviction_score",
        "entry_price",
        "tp1",
        "tp2",
        "tp3",
        "stop_loss",
        "rrr",
        "invalidation_price",
    ],
)
def test_trade_proposal_rejects_boolean_numeric_fields(field):
    data = dict(VALID_BUY)
    data[field] = True

    with pytest.raises(ValidationError):
        TradeProposal.model_validate(data)


def test_proposal_key_levels_drop_boolean_numbers():
    from app.models import ProposalKeyLevels

    levels = ProposalKeyLevels.model_validate(
        {
            "immediate_support": True,
            "immediate_resistance": False,
            "major_liquidity_pools": [True, 10.0, False],
        }
    )

    assert levels.immediate_support is None
    assert levels.immediate_resistance is None
    assert levels.major_liquidity_pools == [10.0]


def test_parse_content_to_proposal_maps_nan_to_llmerror():
    """The production contract: a NaN leg must degrade to a clean LlmError (HTTP
    502), NEVER a 500 or a fake-tradeable proposal."""
    from app.llm.client import LlmError, _parse_content_to_proposal

    raw = json.dumps(VALID_BUY).replace("64100.0", "NaN")  # stop_loss -> NaN
    with pytest.raises(LlmError):
        _parse_content_to_proposal(raw, provider="grok")


@pytest.mark.parametrize("field", ["new_sl", "new_tp", "partial_close_pct"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reevaluate_proposal_rejects_nonfinite(field, bad):
    from app.models import ReevaluateProposal

    base = {"action": "HOLD", "new_sl": None, "new_tp": None}
    base[field] = bad
    with pytest.raises(ValidationError):
        ReevaluateProposal.model_validate(base)


@pytest.mark.parametrize("field", ["new_sl", "new_tp", "partial_close_pct"])
def test_reevaluate_proposal_rejects_boolean_numeric_fields(field):
    from app.models import ReevaluateProposal

    base = {"action": "HOLD", "new_sl": None, "new_tp": None}
    base[field] = True
    with pytest.raises(ValidationError):
        ReevaluateProposal.model_validate(base)


def _reeval(action="MOVE_SL_BE", **kw):
    from app.models import ReevaluateProposal

    base = dict(action=action, confidence="high", reason="x", new_sl=None, new_tp=None)
    base.update(kw)
    return ReevaluateProposal.model_validate(base)


def _reeval_ctx(side="long", last_price=100.0, atr=1.0):
    return {
        "last_price": last_price,
        "position": {"side": side, "current_price": last_price},
        "ltf": {"read": {"atr14": atr}},
    }


def test_reevaluation_wrong_side_sl_caps_confidence_and_warns():
    """Finding 2: a new_sl on the WRONG side of the current price for the
    position direction (long, sl ABOVE price) must have its confidence capped
    and a warning attached — never hard-rejected (the apply path re-validates)."""
    from app.llm.client import annotate_reevaluation

    p = _reeval(new_sl=105.0)  # long, price 100 -> stop above price is inverted
    out = annotate_reevaluation(p, _reeval_ctx(side="long", last_price=100.0))
    assert out.confidence == "low"
    assert out.risk_notes  # a warning was attached
    # NOT hard-rejected: the action and the (implausible) level are preserved.
    assert out.action == "MOVE_SL_BE"
    assert out.new_sl == 105.0


def test_reevaluation_wrong_side_tp_short_caps_confidence():
    from app.llm.client import annotate_reevaluation

    # short position: a valid new_tp is BELOW price; 106 (above) is wrong-side.
    p = _reeval(action="HOLD", new_tp=106.0)
    out = annotate_reevaluation(p, _reeval_ctx(side="short", last_price=100.0))
    assert out.confidence == "low"
    assert out.risk_notes


def test_reevaluation_valid_geometry_unchanged():
    from app.llm.client import annotate_reevaluation

    p = _reeval(new_sl=98.0, new_tp=104.0)  # long, both on the correct side
    out = annotate_reevaluation(p, _reeval_ctx(side="long", last_price=100.0))
    assert out.confidence == "high"
    assert out.risk_notes == ""  # untouched


def test_parse_content_reevaluation_applies_plausibility_cap():
    """The cap is wired into the shared parse helper, so every provider path
    (claude/xai/openai/ollama) gets the same net when context is supplied."""
    from app.llm.client import _parse_content_to_reevaluation

    raw = '{"action":"MOVE_SL_BE","confidence":"high","new_sl":105.0,"reason":"x"}'
    out = _parse_content_to_reevaluation(
        raw, provider="test", context=_reeval_ctx(side="long", last_price=100.0)
    )
    assert out.confidence == "low"
    assert out.risk_notes


def test_parse_reevaluation_rejects_nan_token():
    """Symmetry with the proposal side: the bare NaN token in a reevaluation
    response is rejected at parse, not carried downstream."""
    from app.llm.client import parse_reevaluation

    raw = '{"action": "MOVE_SL_BE", "new_sl": NaN, "reason": "x"}'
    with pytest.raises(ValidationError):
        parse_reevaluation(raw)


def test_key_levels_coerce_nonfinite_to_none():
    """Display-only key_levels: a NaN/Inf from untrusted LLM output is coerced to
    None (support/resistance) or filtered (pools) — no reject, no NaN into the DB."""
    from app.models import ProposalKeyLevels

    kl = ProposalKeyLevels.model_validate(
        {
            "immediate_support": float("nan"),
            "immediate_resistance": float("inf"),
            "major_liquidity_pools": [100.0, float("nan"), "psych 50k"],
        }
    )
    assert kl.immediate_support is None and kl.immediate_resistance is None
    assert kl.major_liquidity_pools == [100.0, "psych 50k"]  # NaN filtered, str kept
