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
    "positions": [
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "hold_vol": 5,
            "entry_price": 100.0,
        }
    ],
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


def test_llm_context_requires_literal_true_for_account_opt_in():
    settings = Settings(_env_file=None, include_account_in_llm=False)
    settings.include_account_in_llm = "false"  # type: ignore[assignment]

    ctx = build_llm_context(MARKET, ACCOUNT, settings)

    assert ctx["account"] == {
        "omitted": True,
        "reason": "INCLUDE_ACCOUNT_IN_LLM=false",
    }
    assert "remaining_risk_budget_pct" not in ctx


def test_llm_context_includes_account_when_explicitly_enabled():
    """Explicit opt-in includes the normalized public account fields."""
    settings = Settings(_env_file=None, include_account_in_llm=True)
    ctx = build_llm_context(MARKET, ACCOUNT, settings)

    assert ctx["account"]["equity_usdt"] == 12345.0
    assert ctx["account"]["available_usdt"] == 6789.0
    assert ctx["account"]["positions"] == ACCOUNT["positions"]


def test_llm_context_drops_unexpected_position_fields_with_explicit_opt_in():
    marker = "SYNTHETIC_PRIVATE_POSITION_DIAGNOSTIC"
    account = {
        **ACCOUNT,
        "positions": [
            {
                **ACCOUNT["positions"][0],
                "position_id": marker,
                "margin_ratio": {"raw": marker},
                "private_diagnostic": {"raw": marker},
            }
        ],
    }

    ctx = build_llm_context(
        MARKET,
        account,
        Settings(_env_file=None, include_account_in_llm=True),
    )

    assert ctx["account"]["positions"] == ACCOUNT["positions"]
    assert marker not in str(ctx)
    assert account["positions"][0]["private_diagnostic"] == {"raw": marker}


def test_llm_context_sanitizes_account_envelope_with_explicit_opt_in():
    marker = "SYNTHETIC_PRIVATE_ACCOUNT_DIAGNOSTIC"
    account = {
        **ACCOUNT,
        "equity_usdt": {"raw": marker},
        "available_usdt": marker,
        "error": marker,
    }

    ctx = build_llm_context(
        MARKET,
        account,
        Settings(_env_file=None, include_account_in_llm=True),
    )

    assert ctx["account"] == {
        "equity_usdt": None,
        "available_usdt": None,
        "positions": ACCOUNT["positions"],
        "error": "Account data unavailable",
    }
    assert "remaining_risk_budget_pct" not in ctx
    assert marker not in str(ctx)
    assert account["equity_usdt"] == {"raw": marker}
