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
