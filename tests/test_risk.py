"""Unit tests for sizing helpers and risk gates (no network)."""

import pytest

from app.config import Settings
from app.models import ContractMeta, OrderTicket
from app.risk.gates import validate_order
from app.risk.sizing import calc_rrr, risk_usdt, suggest_vol


def _settings(**kwargs) -> Settings:
    base = dict(
        trading_enabled=True,
        max_leverage=20,
        max_risk_pct=1.0,
        min_rrr=2.0,
        strict_rrr=True,
        risk_slippage_pct=0.05,
        allow_unprotected_entry=False,
        max_notional_usdt=500.0,
        local_api_token="test-token",
    )
    base.update(kwargs)
    return Settings(**base)


def _contract(**kwargs) -> ContractMeta:
    base = dict(
        symbol="BTC_USDT",
        contract_size=0.0001,
        price_unit=0.1,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1_000_000.0,
        max_leverage=125,
        min_leverage=1,
        api_allowed=True,
    )
    base.update(kwargs)
    return ContractMeta(**base)


def _ticket(**kwargs) -> OrderTicket:
    base = dict(
        symbol="BTC_USDT",
        side="long",
        order_type="limit",
        vol=1.0,
        leverage=5,
        price=100_000.0,
        entry=100_000.0,
        stop_loss=99_000.0,
        take_profit=102_000.0,  # RRR = 2.0
        open_type=1,
    )
    base.update(kwargs)
    return OrderTicket(**base)


def test_calc_rrr_long_and_short():
    assert calc_rrr("long", 100, 90, 120) == pytest.approx(2.0)
    assert calc_rrr("short", 100, 110, 80) == pytest.approx(2.0)


def test_calc_rrr_invalid():
    with pytest.raises(ValueError):
        calc_rrr("long", 100, 110, 120)  # stop above entry
    with pytest.raises(ValueError):
        calc_rrr("long", 100, 90, 95)  # tp below entry


def test_risk_usdt_basic_and_slippage():
    r = risk_usdt(10, 0.0001, 100_000, 99_000)
    # 10 * 0.0001 * 1000 = 1.0
    assert r == pytest.approx(1.0)
    r_buf = risk_usdt(10, 0.0001, 100_000, 99_000, slippage_pct=0.05)
    assert r_buf == pytest.approx(1.0 * 1.0005)


def test_suggest_vol_floors_to_unit():
    # budget 1% of 10_000 = 100; distance 1000; cs 0.0001 → raw = 100/(0.1)=1000
    v = suggest_vol(10_000, 1.0, 0.0001, 100_000, 99_000, vol_unit=1.0, min_vol=1.0)
    assert v == 1000.0


def test_suggest_vol_does_not_inflate_to_min_vol():
    # budget tiny: 1% of 10 = 0.1 USDT; distance 1000; cs 0.0001 → raw = 0.1/0.1 = 1
    # but with larger distance / smaller budget → 0, never forced to min_vol
    v = suggest_vol(10.0, 1.0, 0.0001, 100_000, 50_000, vol_unit=1.0, min_vol=10.0)
    # raw = 0.1 / (0.0001 * 50000) = 0.1/5 = 0.02 → floor to 0 < min_vol 10
    assert v == 0.0


def test_over_leverage_rejected():
    g = validate_order(
        _ticket(leverage=50),
        _contract(),
        equity=10_000,
        settings=_settings(max_leverage=20),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("MAX_LEVERAGE" in e for e in g.errors)


def test_leverage_above_contract_max_rejected():
    g = validate_order(
        _ticket(leverage=30),
        _contract(max_leverage=25),
        equity=10_000,
        settings=_settings(max_leverage=50),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("maxLeverage" in e for e in g.errors)


def test_risk_over_max_rejected():
    # vol huge → risk >> 1%
    # risk = vol * 0.0001 * 1000 * 1.0005 ≈ vol * 0.10005
    # for equity 10_000, 1% = 100 → vol max ~999
    g = validate_order(
        _ticket(vol=50_000, take_profit=200_000),  # large RRR ok
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("MAX_RISK_PCT" in e for e in g.errors)


def test_low_rrr_strict_rejected():
    # entry 100k, stop 99k, tp 100.6k → reward 600 / risk 1000 = 0.6
    g = validate_order(
        _ticket(take_profit=100_600.0),
        _contract(),
        equity=10_000,
        settings=_settings(strict_rrr=True, min_rrr=2.0),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("RRR" in e and "STRICT" in e for e in g.errors)


def test_low_rrr_non_strict_warning_only():
    g = validate_order(
        _ticket(take_profit=100_600.0),
        _contract(),
        equity=10_000,
        settings=_settings(strict_rrr=False, min_rrr=2.0),
        last_price=100_000,
    )
    assert g.ok is True
    assert any("RRR" in w for w in g.warnings)


def test_unprotected_without_sl_rejected():
    g = validate_order(
        _ticket(stop_loss=None, take_profit=None),
        _contract(),
        equity=10_000,
        settings=_settings(allow_unprotected_entry=False),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("stop_loss" in e.lower() or "ALLOW_UNPROTECTED" in e for e in g.errors)


def test_unprotected_allowed_when_flag_true():
    g = validate_order(
        _ticket(stop_loss=None, take_profit=None, vol=1),
        _contract(),
        equity=10_000,
        settings=_settings(allow_unprotected_entry=True),
        last_price=100_000,
    )
    assert g.ok is True
    assert any("unprotected" in w.lower() for w in g.warnings)


def test_api_allowed_false_rejected():
    g = validate_order(
        _ticket(),
        _contract(api_allowed=False),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("apiAllowed" in e for e in g.errors)


def test_disarmed_preview_rejected():
    g = validate_order(
        _ticket(),
        _contract(),
        equity=10_000,
        settings=_settings(trading_enabled=False),
        last_price=100_000,
        for_confirm=False,
    )
    assert g.ok is False
    assert any("DISARMED" in e for e in g.errors)


def test_vol_precision_min():
    g = validate_order(
        _ticket(vol=0.5),  # min_vol=1
        _contract(min_vol=1.0, vol_unit=1.0),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("minVol" in e for e in g.errors)


def test_max_notional_usdt_is_soft_warning_not_block():
    # Fixed USDT cap no longer blocks (free size) — it only warns.
    # notional = 100 * 0.0001 * 100000 = 1000 > 500
    g = validate_order(
        _ticket(vol=100, take_profit=102_000),
        _contract(),
        equity=1_000_000,  # risk % ok
        settings=_settings(max_notional_usdt=500, max_notional_pct_of_equity=0.0),
        last_price=100_000,
    )
    assert g.ok is True
    assert any("MAX_NOTIONAL_USDT" in w for w in g.warnings)


def test_max_notional_pct_of_equity_hard_cap():
    # notional 1000; cap = equity 1000 * 50% = 500 → hard block.
    g = validate_order(
        _ticket(vol=100, take_profit=102_000),
        _contract(),
        equity=1_000,
        settings=_settings(
            max_risk_pct=100.0, max_notional_usdt=0, max_notional_pct_of_equity=50.0
        ),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("MAX_NOTIONAL_PCT_OF_EQUITY" in e for e in g.errors)


def test_gate_fails_closed_on_nonfinite_max_risk_pct():
    """F-06 defense-in-depth: Settings() now rejects NaN at construction, but
    if a non-finite value ever reaches the gate (e.g. via attribute mutation
    after construction, since validate_assignment is off), the risk gate must
    fail CLOSED instead of silently letting risk_pct > nan (always False)
    disable the check."""
    s = _settings(max_risk_pct=1.0)
    object.__setattr__(s, "max_risk_pct", float("nan"))
    g = validate_order(
        _ticket(vol=50_000, take_profit=200_000),  # would normally exceed 1%
        _contract(),
        equity=10_000,
        settings=s,
        last_price=100_000,
    )
    assert g.ok is False
    assert any("MAX_RISK_PCT" in e for e in g.errors)


def test_gate_fails_closed_on_nonfinite_notional_pct_cap():
    s = _settings(max_risk_pct=100.0, max_notional_usdt=0)
    object.__setattr__(s, "max_notional_pct_of_equity", float("inf"))
    g = validate_order(
        _ticket(vol=100, take_profit=102_000),
        _contract(),
        equity=1_000,
        settings=s,
        last_price=100_000,
    )
    assert g.ok is False
    assert any("MAX_NOTIONAL_PCT_OF_EQUITY" in e for e in g.errors)


def test_happy_path_gate_ok():
    g = validate_order(
        _ticket(),
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is True
    assert g.rounded_vol == 1.0
    assert g.rrr == pytest.approx(2.0)
    assert g.risk_usdt > 0
