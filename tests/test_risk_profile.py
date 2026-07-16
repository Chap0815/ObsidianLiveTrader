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
    "MAX_PRICE_DRIFT_PCT",
    "STRICT_AGGREGATE_RISK",
    "AGGREGATE_POS_RISK_CAP_PCT",
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


def test_balanced_defaults_after_calibration():
    """R-07/L-01-Config: calibrated defaults for the balanced (default) preset."""
    s = Settings(_env_file=None, risk_profile="balanced")
    assert s.min_rrr == 1.5
    assert s.max_price_drift_pct == 1.0
    assert s.strict_aggregate_risk is False
    assert s.aggregate_pos_risk_cap_pct == 2.0


def test_conservative_keeps_strict():
    """conservative stays the tight profile: min_rrr 2.0, both strict flags True."""
    s = Settings(_env_file=None, risk_profile="conservative")
    assert s.min_rrr == 2.0
    assert s.strict_rrr is True
    assert s.strict_aggregate_risk is True


def test_explicit_env_overrides_preset():
    """An explicitly-set field always wins over the preset fill."""
    s = Settings(_env_file=None, risk_profile="balanced", max_price_drift_pct=0.3)
    assert s.max_price_drift_pct == 0.3
