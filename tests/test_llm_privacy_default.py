"""F-15 (privacy): INCLUDE_ACCOUNT_IN_LLM must default to false.

Equity, available margin and full position details are sensitive account
data. Sending them to an external LLM must be an explicit opt-in, not the
out-of-the-box default.
"""

import pytest

from app.config import Settings
from app.llm.client import build_llm_context


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # A developer .env (or leaked env var) must not mask the field default
    # under test — same isolation pattern as tests/test_risk_profile.py.
    monkeypatch.delenv("INCLUDE_ACCOUNT_IN_LLM", raising=False)

MARKET = {
    "symbol": "BTC_USDT",
    "last_price": 100.0,
    "funding": {},
    "contract": {},
    "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
    "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
}

ACCOUNT = {
    "equity_usdt": 12345.0,
    "available_usdt": 6789.0,
    "positions": [{"symbol": "BTC_USDT", "side": "long", "hold_vol": 5}],
    "error": None,
}


def test_include_account_in_llm_defaults_to_false():
    """Field default must be false (privacy-by-default)."""
    assert Settings(_env_file=None).include_account_in_llm is False


def test_llm_context_omits_account_by_default():
    """With INCLUDE_ACCOUNT_IN_LLM unset, the built context must NOT leak
    equity/available/positions to the external LLM."""
    settings = Settings(_env_file=None)
    ctx = build_llm_context(MARKET, ACCOUNT, settings)

    assert ctx["account"] == {
        "omitted": True,
        "reason": "INCLUDE_ACCOUNT_IN_LLM=false",
    }
    # Belt and suspenders: no equity/available/positions values anywhere
    # under the "account" key.
    assert "equity_usdt" not in ctx["account"]
    assert "available_usdt" not in ctx["account"]
    assert "positions" not in ctx["account"]


def test_llm_context_includes_account_when_explicitly_enabled():
    """Setting INCLUDE_ACCOUNT_IN_LLM=true keeps full existing behavior —
    no functional change for users who opt in."""
    settings = Settings(_env_file=None, include_account_in_llm=True)
    ctx = build_llm_context(MARKET, ACCOUNT, settings)

    assert ctx["account"]["equity_usdt"] == 12345.0
    assert ctx["account"]["available_usdt"] == 6789.0
    assert ctx["account"]["positions"] == ACCOUNT["positions"]
