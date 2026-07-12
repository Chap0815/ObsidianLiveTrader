"""POST /api/sizing/suggest — must size for the CLIENT-requested risk %,
hard-capped at settings.max_risk_pct. Regression test for a bug where the
endpoint always sized for max_risk_pct (10%) while the UI labeled the
suggestion "2% risk".
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models import ContractMeta, Ticker
from app.risk.sizing import suggest_vol

EQUITY = 10_000.0
ENTRY = 100_000.0
STOP = 99_000.0
CONTRACT_SIZE = 0.0001
VOL_UNIT = 1.0
MIN_VOL = 1.0


def _contract_meta() -> ContractMeta:
    return ContractMeta(
        symbol="BTC_USDT",
        contract_size=CONTRACT_SIZE,
        price_unit=0.5,
        vol_unit=VOL_UNIT,
        min_vol=MIN_VOL,
        max_vol=1_000_000,
        max_leverage=125,
    )


AVAILABLE = 1_000_000.0  # ample margin so the margin/notional caps don't bind


def _mock_client(*, available: float = AVAILABLE, positions: list | None = None) -> MagicMock:
    mock = MagicMock()
    mock.contract_meta = AsyncMock(return_value=_contract_meta())
    mock.ticker = AsyncMock(
        return_value=Ticker(symbol="BTC_USDT", last_price=ENTRY)
    )
    mock.account_snapshot = AsyncMock(
        return_value={
            "equity_usdt": EQUITY,
            "available_usdt": available,
            "positions": positions or [],
        }
    )
    return mock


def _settings(max_risk_pct: float, **kwargs) -> Settings:
    return Settings(
        exchange="mexc",
        mexc_api_key="k",
        mexc_api_secret="s",
        max_risk_pct=max_risk_pct,
        **kwargs,
    )


def _post(
    monkeypatch,
    payload: dict,
    max_risk_pct: float = 10.0,
    *,
    mock: MagicMock | None = None,
    settings_kwargs: dict | None = None,
):
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: _settings(max_risk_pct, **(settings_kwargs or {})),
    )
    mock = mock or _mock_client()
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.post(
            "/api/sizing/suggest",
            json=dict(
                {
                    "symbol": "BTC_USDT",
                    "side": "long",
                    "vol": 1,
                    "stop_loss": STOP,
                },
                **payload,
            ),
        )
    return r


def _risk_entry(max_risk_pct: float = 10.0) -> float:
    """The same market-adverse-slip-buffered entry validate_order (G3) and
    the endpoint use for risk math on a market long — NOT the raw last price.
    """
    settings = _settings(max_risk_pct)
    slip = settings.market_entry_slippage_pct
    return ENTRY * (1.0 + slip / 100.0)


def _expected_vol(
    risk_pct: float,
    max_risk_pct: float = 10.0,
    *,
    available: float = AVAILABLE,
    existing_risk: float = 0.0,
    leverage: float = 5,
) -> float:
    """Gate-consistent expected suggestion: same clamps the endpoint now
    threads into suggest_vol() (F-09) — market-slip-adjusted entry,
    RISK_SLIPPAGE_PCT buffer, existing same-side risk, available margin and
    the equity-relative notional cap.
    """
    settings = _settings(max_risk_pct)
    return suggest_vol(
        EQUITY,
        risk_pct,
        CONTRACT_SIZE,
        _risk_entry(max_risk_pct),
        STOP,
        VOL_UNIT,
        MIN_VOL,
        side="long",
        slippage_pct=settings.risk_slippage_pct,
        existing_risk_usdt=existing_risk,
        available_usdt=available,
        leverage=leverage,
        max_notional_pct_of_equity=settings.max_notional_pct_of_equity,
    )


def test_sizing_suggest_uses_requested_risk_pct_not_max(monkeypatch):
    """risk_pct=2.0 sent, max_risk_pct=10 -> sizes for 2%, not 10%."""
    r = _post(monkeypatch, {"risk_pct": 2.0}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()

    expected_vol = _expected_vol(2.0, max_risk_pct=10.0)
    assert body["vol"] == expected_vol
    assert body["risk_pct"] == 2.0

    # Sanity: the returned size must never risk MORE than ~2% of equity at
    # the SL (slippage buffering only makes this more conservative, never
    # less) — and must be far below the old bug's 10% sizing.
    risk_at_sl = body["vol"] * CONTRACT_SIZE * abs(ENTRY - STOP)
    assert risk_at_sl <= EQUITY * 0.02 + 1e-6
    # Not fully starved out by the buffers — a real (non-zero) size remains.
    assert risk_at_sl > 0

    # It must differ from what the old (buggy) naive max_risk_pct-only calc
    # gives (no slippage/geometry/margin/notional awareness).
    buggy_vol = suggest_vol(
        EQUITY, 10.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    assert body["vol"] != buggy_vol
    assert body["vol"] < buggy_vol


def test_sizing_suggest_caps_requested_risk_at_max(monkeypatch):
    """risk_pct=50 sent, max_risk_pct=10 -> capped at 10%, never exceeds it."""
    r = _post(monkeypatch, {"risk_pct": 50.0}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()

    expected_vol = _expected_vol(10.0, max_risk_pct=10.0)
    assert body["vol"] == expected_vol
    assert body["risk_pct"] == 10.0

    risk_at_sl = body["vol"] * CONTRACT_SIZE * abs(ENTRY - STOP)
    # Must never risk more than max_risk_pct of equity.
    assert risk_at_sl <= EQUITY * 0.10 + 1e-6
    assert risk_at_sl > 0


def test_sizing_suggest_defaults_to_max_risk_pct_when_missing(monkeypatch):
    """No risk_pct in the request -> sensible fallback (max_risk_pct)."""
    r = _post(monkeypatch, {}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()

    expected_vol = _expected_vol(10.0, max_risk_pct=10.0)
    assert body["vol"] == expected_vol
    assert body["risk_pct"] == 10.0


def test_sizing_suggest_treats_non_positive_risk_pct_as_missing(monkeypatch):
    """risk_pct=0 (or negative) is invalid input -> falls back to max_risk_pct."""
    r = _post(monkeypatch, {"risk_pct": 0.0}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()
    assert body["risk_pct"] == 10.0


def test_sizing_suggest_clamped_by_available_margin(monkeypatch):
    """F-09 regression: the naive risk-%-only formula ignores available
    margin entirely, so it can suggest a size the account cannot actually
    afford — Confirm's gate would then reject it (or worse, a lower-leverage
    fallback would silently understate the true risk). The fixed suggestion
    must clamp to what the real gate (available_usdt >= notional/leverage)
    would accept.
    """
    tiny_available = 100.0  # USDT
    mock = _mock_client(available=tiny_available)
    r = _post(monkeypatch, {"risk_pct": 2.0}, max_risk_pct=10.0, mock=mock)
    assert r.status_code == 200
    body = r.json()

    # The naive/old calc *would* suggest a size requiring far more margin
    # than the account has (at the ticket's default 5x leverage).
    naive_vol = suggest_vol(
        EQUITY, 2.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    naive_required_margin = (naive_vol * CONTRACT_SIZE * ENTRY) / 5
    assert naive_required_margin > tiny_available

    expected_vol = _expected_vol(2.0, max_risk_pct=10.0, available=tiny_available)
    assert body["vol"] == expected_vol
    assert 0 < body["vol"] < naive_vol

    # The clamped suggestion's estimated initial margin must fit the
    # account's real available balance (same check gates.py's "Available
    # margin" block performs at Preview/Confirm).
    est_margin = body["notional_usdt"] / 5
    assert est_margin <= tiny_available + 1e-6


def test_sizing_suggest_clamped_by_existing_same_side_risk(monkeypatch):
    """F-09 regression: aggregate MAX_RISK_PCT must include already-open
    same-side risk, or the suggestion can push total risk above the cap.
    """
    existing_position = {
        # Already-mapped shape (what MexcClient.account_snapshot() actually
        # returns — see app.mexc.client.map_position).
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 2,
        "entry_price": ENTRY,
        "liquidate_price": ENTRY * 0.5,
    }
    mock = _mock_client(positions=[existing_position])
    r = _post(monkeypatch, {"risk_pct": 2.0}, max_risk_pct=10.0, mock=mock)
    assert r.status_code == 200
    body = r.json()

    existing_risk_usdt = body["existing_same_side_risk_usdt"]
    assert existing_risk_usdt > 0

    expected_vol = _expected_vol(
        2.0, max_risk_pct=10.0, existing_risk=existing_risk_usdt
    )
    assert body["vol"] == expected_vol

    naive_vol = suggest_vol(
        EQUITY, 2.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    assert body["vol"] < naive_vol

    new_risk = body["vol"] * CONTRACT_SIZE * abs(ENTRY - STOP)
    assert new_risk + existing_risk_usdt <= EQUITY * 0.02 + 1e-6
