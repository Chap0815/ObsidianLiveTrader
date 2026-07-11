"""RISK_PROFILE preset behaviour (audit I1/#2, test T3)."""

import pytest

from app.config import Settings

_RISK_VARS = (
    "RISK_PROFILE",
    "MAX_LEVERAGE",
    "MAX_RISK_PCT",
    "MIN_RRR",
    "STRICT_RRR",
    "MAX_NOTIONAL_PCT_OF_EQUITY",
    "STRICT_AVAILABLE_MARGIN",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in _RISK_VARS:
        monkeypatch.delenv(var, raising=False)


def test_risk_profile_fills_only_unset_fields():
    s = Settings(_env_file=None, risk_profile="conservative")
    assert s.max_risk_pct == 1.0
    assert s.max_leverage == 20
    assert s.min_rrr == 2.0
    assert s.strict_rrr is True

    f = Settings(_env_file=None, risk_profile="free")
    assert f.max_risk_pct == 15.0
    assert f.max_leverage == 100
    assert f.max_notional_pct_of_equity == 0.0
    assert f.strict_rrr is False


def test_risk_profile_explicit_value_wins_over_preset():
    # Explicit field is in model_fields_set -> preset must NOT override it.
    s = Settings(_env_file=None, risk_profile="conservative", max_risk_pct=7.0)
    assert s.max_risk_pct == 7.0
    # non-set fields still come from the conservative preset
    assert s.max_leverage == 20


def test_default_field_defaults_match_balanced_preset():
    s = Settings(_env_file=None, risk_profile="balanced")
    assert (s.max_risk_pct, s.max_leverage, s.strict_rrr) == (5.0, 50, False)
