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


def _mock_client() -> MagicMock:
    mock = MagicMock()
    mock.contract_meta = AsyncMock(return_value=_contract_meta())
    mock.ticker = AsyncMock(
        return_value=Ticker(symbol="BTC_USDT", last_price=ENTRY)
    )
    mock.account_snapshot = AsyncMock(return_value={"equity_usdt": EQUITY})
    return mock


def _settings(max_risk_pct: float) -> Settings:
    return Settings(
        exchange="mexc",
        mexc_api_key="k",
        mexc_api_secret="s",
        max_risk_pct=max_risk_pct,
    )


def _post(monkeypatch, payload: dict, max_risk_pct: float = 10.0):
    monkeypatch.setattr("app.main.get_settings", lambda: _settings(max_risk_pct))
    mock = _mock_client()
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


def test_sizing_suggest_uses_requested_risk_pct_not_max(monkeypatch):
    """risk_pct=2.0 sent, max_risk_pct=10 -> sizes for 2%, not 10%."""
    r = _post(monkeypatch, {"risk_pct": 2.0}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()

    expected_vol = suggest_vol(
        EQUITY, 2.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    assert body["vol"] == expected_vol
    assert body["risk_pct"] == 2.0

    # Sanity: the returned size must actually risk ~2% of equity at the SL,
    # not 10% (the old bug).
    risk_at_sl = body["vol"] * CONTRACT_SIZE * abs(ENTRY - STOP)
    assert risk_at_sl == pytest.approx(EQUITY * 0.02, rel=1e-6)

    # It must differ from what the old (buggy) max_risk_pct-only calc gives.
    buggy_vol = suggest_vol(
        EQUITY, 10.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    assert body["vol"] != buggy_vol


def test_sizing_suggest_caps_requested_risk_at_max(monkeypatch):
    """risk_pct=50 sent, max_risk_pct=10 -> capped at 10%, never exceeds it."""
    r = _post(monkeypatch, {"risk_pct": 50.0}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()

    expected_vol = suggest_vol(
        EQUITY, 10.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    assert body["vol"] == expected_vol
    assert body["risk_pct"] == 10.0

    risk_at_sl = body["vol"] * CONTRACT_SIZE * abs(ENTRY - STOP)
    assert risk_at_sl == pytest.approx(EQUITY * 0.10, rel=1e-6)
    # Must never risk more than max_risk_pct of equity.
    assert risk_at_sl <= EQUITY * 0.10 + 1e-6


def test_sizing_suggest_defaults_to_max_risk_pct_when_missing(monkeypatch):
    """No risk_pct in the request -> sensible fallback (max_risk_pct)."""
    r = _post(monkeypatch, {}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()

    expected_vol = suggest_vol(
        EQUITY, 10.0, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL
    )
    assert body["vol"] == expected_vol
    assert body["risk_pct"] == 10.0


def test_sizing_suggest_treats_non_positive_risk_pct_as_missing(monkeypatch):
    """risk_pct=0 (or negative) is invalid input -> falls back to max_risk_pct."""
    r = _post(monkeypatch, {"risk_pct": 0.0}, max_risk_pct=10.0)
    assert r.status_code == 200
    body = r.json()
    assert body["risk_pct"] == 10.0
