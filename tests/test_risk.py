"""Unit tests for sizing helpers and risk gates (no network)."""

import json
import math

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import ContractMeta, OrderTicket
from app.risk.gates import validate_order
from app.risk.sizing import (
    adverse_market_entry,
    calc_rrr,
    risk_usdt,
    round_down_to_unit,
    round_to_unit,
    suggest_vol,
)


def _settings(**kwargs) -> Settings:
    base = dict(
        exchange="mexc",
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
        state=0,
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


def test_suggest_vol_rejects_negative_existing_risk_like_order_gate():
    v = suggest_vol(
        10_000,
        1.0,
        0.0001,
        100_000,
        99_000,
        vol_unit=1.0,
        min_vol=1.0,
        existing_risk_usdt=-1.0,
    )

    assert v == 0.0


@pytest.mark.parametrize(
    "args",
    [
        (1e308, 100.0, 1.0, 100.0, 99.0, 1.0, 1.0),
        (1e308, 1.0, 1e-308, 2.0, 1.0, 1.0, 1.0),
        (1e308, 1.0, 1.0, 1.01, 1.0, 1e-308, 1.0),
        (100.0, 1.0, 1e-308, 2e-308, 1e-308, 1.0, 1.0),
    ],
)
def test_suggest_vol_fails_closed_when_finite_intermediate_overflows(args):
    assert suggest_vol(*args) == 0.0


@pytest.mark.parametrize(
    "rounder",
    [
        round_down_to_unit,
        round_to_unit,
        lambda value, unit: round_to_unit(value, unit, "up"),
    ],
)
def test_unit_rounding_fails_closed_when_finite_step_count_overflows(rounder):
    assert rounder(1e308, 1e-308) == 0.0


@pytest.mark.parametrize("direction", ["nearest", "up"])
def test_unit_rounding_fails_closed_when_finite_rounded_product_overflows(direction):
    assert round_to_unit(1.7e308, 1e308, direction) == 0.0


def test_gate_blocks_when_finite_volume_step_count_overflows():
    gate = validate_order(
        _ticket(vol=1e308),
        _contract(vol_unit=1e-308, max_vol=1e308),
        equity=10_000.0,
        settings=_settings(),
        last_price=100_000.0,
    )

    assert gate.ok is False
    assert gate.rounded_vol == 0.0
    assert any("rounds to 0" in error for error in gate.errors)


def test_gate_result_stays_json_safe_when_finite_derived_values_overflow():
    gate = validate_order(
        _ticket(
            vol=1e308,
            price=1e308,
            entry=1e308,
            stop_loss=1e307,
            take_profit=1.1e308,
            leverage=1,
        ),
        _contract(
            contract_size=1.0,
            price_unit=0.0,
            vol_unit=1.0,
            max_vol=1e308,
        ),
        equity=1e308,
        settings=_settings(
            max_risk_pct=100.0,
            min_rrr=0.0,
            strict_rrr=False,
            max_notional_usdt=0.0,
            max_notional_pct_of_equity=0.0,
        ),
        available_usdt=1e308,
    )

    assert gate.ok is False
    assert any("not finite" in error for error in gate.errors)
    json.dumps(gate.to_dict(), allow_nan=False)


def test_gate_result_stays_json_safe_when_rrr_overflows():
    entry = 1e-308
    stop = math.nextafter(entry, 0.0)
    gate = validate_order(
        _ticket(
            vol=1.0,
            price=entry,
            entry=entry,
            stop_loss=stop,
            take_profit=1.0,
            leverage=1,
        ),
        _contract(
            contract_size=1.0,
            price_unit=0.0,
            vol_unit=1.0,
            min_vol=1.0,
            max_vol=10.0,
        ),
        equity=1e308,
        settings=_settings(
            max_risk_pct=100.0,
            min_rrr=0.0,
            strict_rrr=False,
            max_notional_usdt=0.0,
            max_notional_pct_of_equity=0.0,
        ),
    )

    assert gate.ok is False
    assert any("RRR" in error and "finite" in error for error in gate.errors)
    json.dumps(gate.to_dict(), allow_nan=False)


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


@pytest.mark.parametrize("state", [None, 1, 2, 3, 4])
def test_mexc_non_enabled_contract_state_rejected(state):
    g = validate_order(
        _ticket(),
        _contract(state=state),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is False
    assert any("state" in e.lower() for e in g.errors)


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


@pytest.mark.parametrize(
    "bad_risk", [float("nan"), float("inf"), float("-inf"), -1.0]
)
def test_gate_fails_closed_on_invalid_existing_same_side_risk(bad_risk):
    g = validate_order(
        _ticket(),
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
        existing_same_side_risk_usdt=bad_risk,
    )
    assert g.ok is False
    assert any("existing same-side risk" in e.lower() for e in g.errors)


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


@pytest.mark.parametrize(
    "field",
    ["contract_size", "price_unit", "vol_unit", "min_vol", "max_vol", "min_notional"],
)
def test_gate_fails_closed_on_nonfinite_contract_filter(field):
    g = validate_order(
        _ticket(),
        _contract(**{field: float("nan")}),
        10_000.0,
        _settings(),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any(field in error and "non-finite" in error for error in g.errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vol_unit", 0.0),
        ("min_vol", 0.0),
        ("max_vol", -1.0),
        ("price_unit", -0.1),
        ("min_notional", -1.0),
    ],
)
def test_gate_fails_closed_on_invalid_contract_filter(field, value):
    g = validate_order(
        _ticket(),
        _contract(**{field: value}),
        10_000.0,
        _settings(),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any(field in error for error in g.errors)


@pytest.mark.parametrize(
    "contract",
    [
        _contract(min_vol=10, max_vol=5),
        _contract(min_leverage=20, max_leverage=10),
        _contract(min_leverage=0),
        _contract(max_leverage=0),
    ],
)
def test_gate_fails_closed_on_invalid_contract_bounds(contract):
    g = validate_order(
        _ticket(), contract, 10_000.0, _settings(), last_price=100_000.0
    )
    assert g.ok is False
    assert any("bounds" in error for error in g.errors)


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


def test_gate_uses_conservative_trigger_rounding():
    """R-02: nearest/half-even rounding a long stop-loss to the tick size can
    shift it up to half a tick TOWARD entry (round(99000.6) == 99001),
    understating risk. The gate must use round_trigger_to_unit's side-aware
    floor instead, so the long SL only ever rounds AWAY from entry (99000.0)."""
    g = validate_order(
        _ticket(stop_loss=99_000.6, take_profit=102_000.0),
        _contract(price_unit=1.0),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is True, g.errors
    assert g.rounded_stop == pytest.approx(99_000.0)


# ── Defect B (CRITICAL): NaN/Inf stop_loss must NOT fail the gate open ──


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_order_ticket_rejects_nonfinite_stop_loss(bad):
    """Root cause: OrderTicket must reject a non-finite stop_loss at
    construction (pydantic), so the HTTP path 422s instead of carrying a NaN
    stop into the risk gate where every `>`/`<` comparison silently passes."""
    with pytest.raises(ValidationError):
        _ticket(stop_loss=bad)


@pytest.mark.parametrize(
    "field",
    ["stop_loss", "take_profit", "tp2", "entry", "price", "vol", "leverage"],
)
def test_order_ticket_rejects_nonfinite_numeric_fields(field):
    """Every numeric price/level/size field that feeds risk or geometry must
    reject NaN/Inf. (leverage is int → Inf/NaN rejected by float bound too.)"""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            _ticket(**{field: bad})


@pytest.mark.parametrize(
    "field",
    [
        "vol",
        "leverage",
        "proposal_id",
        "open_type",
        "price",
        "entry",
        "stop_loss",
        "take_profit",
        "tp2",
        "risk_pct",
    ],
)
def test_order_ticket_rejects_boolean_numeric_fields(field):
    with pytest.raises(ValidationError):
        _ticket(**{field: True})


def test_order_ticket_keeps_optional_none():
    """The new validator must not reject legitimate absent optionals."""
    t = _ticket(stop_loss=None, take_profit=None, tp2=None, entry=None)
    assert t.stop_loss is None
    assert t.take_profit is None


def test_gate_fails_closed_on_nan_stop_loss_hl_contract():
    """Defense-in-depth: even if a non-HTTP caller bypasses pydantic and hands
    the gate a NaN stop on an HL-style contract (price_unit=0 → side-aware
    rounding skipped) with a huge notional, the gate must return ok=False —
    NOT ok=True with empty errors (fail-open). Reproduces the CRITICAL defect."""
    # Bypass the OrderTicket validator to simulate a non-HTTP caller.
    t = _ticket(order_type="market", price=None, vol=50_000, take_profit=200_000)
    object.__setattr__(t, "stop_loss", float("nan"))
    g = validate_order(
        t,
        _contract(price_unit=0.0),  # Hyperliquid-style: no tick rounding
        equity=10_000,
        settings=_settings(allow_unprotected_entry=False),
        last_price=100_000,
    )
    assert g.ok is False
    assert g.errors  # must not be an empty error list
    assert math.isfinite(g.risk_pct)  # no NaN leaks into the reported risk


@pytest.mark.parametrize("bad_equity", [float("nan"), float("inf"), float("-inf")])
def test_gate_fails_closed_on_nonfinite_equity(bad_equity):
    g = validate_order(
        _ticket(order_type="market", price=None, take_profit=103_000.0),
        _contract(),
        equity=bad_equity,
        settings=_settings(),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any("equity unknown" in e for e in g.errors)


def test_manual_trigger_blocked_in_gate_when_disabled():
    """R-04: ALLOW_MANUAL_TRIGGER must be enforced already in the gate — a
    manual ticket fails in the PREVIEW (for_confirm=False) when the flag is
    off, so no one-shot token is ever wasted on a ticket confirm will reject."""
    g = validate_order(
        _ticket(trigger_mode="manual"),
        _contract(),
        equity=10_000,
        settings=_settings(allow_manual_trigger=False),
        last_price=100_000,
        for_confirm=False,
    )
    assert g.ok is False
    assert any("ALLOW_MANUAL_TRIGGER" in e for e in g.errors)


# ── Money-path request models: non-finite must be rejected (gt=0 lets +Inf pass) ──


def test_order_ticket_rejects_unknown_order_type_field():
    with pytest.raises(ValidationError):
        OrderTicket(symbol="BTC_USDT", side="long", vol=1.0, orderType="limit")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_modify_sl_request_rejects_nonfinite(bad):
    """ModifySLRequest.new_sl (gt=0) lets +Infinity through (inf > 0). A non-finite
    stop must never reach place_stop_order — a short WITHOUT an existing SL can't
    be clamped by the mark-geometry guard."""
    from app.models import ModifySLRequest

    with pytest.raises(ValidationError):
        ModifySLRequest(symbol="BTC_USDT", side="long", new_sl=bad)


def test_modify_sl_request_accepts_finite():
    from app.models import ModifySLRequest

    assert ModifySLRequest(symbol="BTC_USDT", side="long", new_sl=100.0).new_sl == 100.0


def test_modify_sl_request_rejects_boolean_price():
    from app.models import ModifySLRequest

    with pytest.raises(ValidationError):
        ModifySLRequest(symbol="BTC_USDT", side="long", new_sl=True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_close_position_request_rejects_nonfinite_vol(bad):
    from app.models import ClosePositionRequest

    with pytest.raises(ValidationError):
        ClosePositionRequest(symbol="BTC_USDT", side="long", vol=bad)


def test_close_position_request_accepts_finite_and_none():
    from app.models import ClosePositionRequest

    assert ClosePositionRequest(symbol="BTC_USDT", side="long", vol=None).vol is None
    assert ClosePositionRequest(symbol="BTC_USDT", side="long", vol=0.5).vol == 0.5


def test_close_position_request_rejects_unknown_size_field():
    from app.models import ClosePositionRequest

    with pytest.raises(ValidationError):
        ClosePositionRequest(symbol="BTC_USDT", side="long", volume=0.5)


@pytest.mark.parametrize("field", ["vol", "fraction"])
def test_close_position_request_rejects_boolean_amounts(field):
    from app.models import ClosePositionRequest

    with pytest.raises(ValidationError):
        ClosePositionRequest(symbol="BTC_USDT", side="long", **{field: True})


@pytest.mark.parametrize("field", ["order_id", "orderId"])
def test_cancel_request_rejects_boolean_order_ids(field):
    from app.models import CancelRequest

    with pytest.raises(ValidationError):
        CancelRequest(**{field: True})


def test_cancel_request_rejects_two_order_id_fields():
    from app.models import CancelRequest

    assert CancelRequest(order_id=7).resolved_order_id() == 7
    assert CancelRequest(orderId=8).resolved_order_id() == 8
    with pytest.raises(ValidationError):
        CancelRequest(order_id=7, orderId=8)


def test_market_order_without_last_price_blocks_spoofed_entry():
    """Enforcement-bypass fix: a MARKET order with no usable last_price (degraded
    ticker) must NOT fall back to the caller-controlled ticket.entry. A spoofed
    entry nudged toward the SL understates MAX_RISK_PCT/RRR/notional while the real
    market order fills at the true market — so the gate must fail CLOSED, not pass
    with only a warning (previous behavior)."""
    # entry 99_010 sits razor-thin above SL 99_000 → tiny risk if trusted.
    t = _ticket(
        order_type="market",
        price=None,
        entry=99_010.0,
        stop_loss=99_000.0,
        take_profit=100_000.0,  # RRR huge, so only the entry-ref matters
        vol=1.0,
    )
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=None,
    )
    assert g.ok is False
    assert any("reference price" in e.lower() for e in g.errors)
    # ticket.entry must NOT have been adopted as the risk reference.
    assert g.entry_for_risk is None


def test_market_order_without_last_price_blocks_on_confirm():
    """Preview/Confirm consistency: the same fail-closed block applies on confirm
    (for_confirm=True) — a degraded confirm ticker must never quietly re-open the
    ticket.entry bypass."""
    t = _ticket(
        order_type="market",
        price=None,
        entry=99_010.0,
        stop_loss=99_000.0,
        take_profit=100_000.0,
        vol=1.0,
    )
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=None,
        for_confirm=True,
        preview_last_price=100_000,
    )
    assert g.ok is False
    assert any("reference price" in e.lower() for e in g.errors)


def test_market_order_uses_adverse_slippage_fill_for_risk():
    """Market risk is measured at the configured worst admissible fill."""
    t = _ticket(
        order_type="market",
        price=None,
        entry=100_000.0,
        stop_loss=99_000.0,
        take_profit=102_500.0,
        vol=1.0,
    )
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
    )
    assert g.ok is True
    assert g.entry_for_risk == adverse_market_entry(100_000.0, "long", 0.15)


def test_market_order_zero_last_price_blocks():
    """Exact <=0 edge of the fail-closed rule: a last_price of 0.0 is not a real
    market price and must NOT be adopted as the risk reference — it fails closed
    just like None (guards a future >0 -> >=0 slip)."""
    t = _ticket(
        order_type="market",
        price=None,
        entry=99_010.0,
        stop_loss=99_000.0,
        take_profit=100_000.0,
        vol=1.0,
    )
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=0.0,
    )
    assert g.ok is False
    assert any("reference price" in e.lower() for e in g.errors)
    assert g.entry_for_risk is None


def test_market_order_subcent_positive_last_price_adopts_reference():
    """A real sub-cent price (>0) must be ACCEPTED as the risk reference, not
    swept up by the <=0 fail-closed rule — the block targets missing prices, not
    small ones."""
    t = _ticket(
        order_type="market",
        price=None,
        entry=0.0000001,
        stop_loss=0.00000009,
        take_profit=0.00000012,
        vol=1.0,
    )
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=0.0000001,
    )
    # The reference was adopted from last_price (>0 branch), not blocked.
    assert g.entry_for_risk == adverse_market_entry(0.0000001, "long", 0.15)


def test_hyperliquid_limit_entry_blocked_without_fill_watcher():
    t = _ticket(order_type="limit", price=100_000.0)
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(exchange="hyperliquid"),
        last_price=100_000.0,
    )
    assert g.ok is False
    assert any("later/partial fills" in e for e in g.errors)


def test_short_market_uses_adverse_lower_fill():
    g = validate_order(
        _ticket(
            side="short",
            order_type="market",
            price=None,
            stop_loss=101_000.0,
            take_profit=97_500.0,
        ),
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=100_000.0,
    )
    assert g.entry_for_risk == adverse_market_entry(100_000.0, "short", 0.15)


def test_limit_order_without_last_price_still_validates():
    """The fix is market-only: a LIMIT order derives its risk reference from the
    limit price and is unaffected by last_price being absent."""
    t = _ticket(order_type="limit", price=100_000.0)  # limit defaults are valid
    g = validate_order(
        t,
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=None,
    )
    assert g.ok is True
    assert g.entry_for_risk == 100_000.0


def test_confirm_warns_when_preview_baseline_price_missing():
    """Drift can't be checked when the preview captured no baseline price — the
    gate must SURFACE that as a warning (not silently skip), and must not error."""
    g = validate_order(
        _ticket(),
        _contract(),
        equity=10_000,
        settings=_settings(),
        last_price=100_000,
        for_confirm=True,
        preview_last_price=None,
    )
    assert any("Price drift cannot be checked" in w for w in g.warnings)
    assert not any("drift" in e.lower() for e in g.errors)
