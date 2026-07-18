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
    """R2-01: the endpoint (and the real gate, M-B) use the RAW last price as
    the market-order risk basis — NO market_entry_slippage_pct shift. The shift
    used to be applied only in sizing, which under-sized the suggestion vs the
    gate; removing it restores exact parity. The remaining adverse-fill buffer
    is RISK_SLIPPAGE_PCT inside suggest_vol(), identical to the gate.
    """
    return ENTRY


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


def test_sizing_matches_gate_no_slippage_shift(monkeypatch):
    """R2-01: the market-order sizing suggestion uses the RAW last price as its
    risk/entry basis — EXACTLY what the real gate (validate_order, M-B) uses —
    with NO market_entry_slippage_pct shift. Proves parity two ways:
      1. body["entry"] == raw last (no shift), and body["vol"] == a raw-entry
         suggest_vol() (the shifted result would be strictly smaller).
      2. Feeding the SUGGESTED vol back through validate_order at the same raw
         last price passes G3 and reports risk_pct == max_risk_pct (the gate
         agrees the suggestion sizes to the full, unchanged budget).
    """
    from app.models import OrderTicket
    from app.risk.gates import validate_order

    max_risk = 2.0
    settings = _settings(max_risk)
    mock = _mock_client()
    r = _post(monkeypatch, {}, max_risk_pct=max_risk, mock=mock)
    assert r.status_code == 200
    body = r.json()

    # (1) No slippage shift: the entry basis IS the raw last price.
    assert body["entry"] == ENTRY

    # The size equals a suggest_vol() computed on the RAW entry (the fix) …
    expected_raw = suggest_vol(
        EQUITY, max_risk, CONTRACT_SIZE, ENTRY, STOP, VOL_UNIT, MIN_VOL,
        side="long", slippage_pct=settings.risk_slippage_pct,
        available_usdt=AVAILABLE, leverage=5,
        max_notional_pct_of_equity=settings.max_notional_pct_of_equity,
    )
    assert body["vol"] == expected_raw

    # … and it is STRICTLY LARGER than the OLD, buggy slip-shifted suggestion
    # (proving the under-sizing is gone, not merely renamed).
    shifted_entry = ENTRY * (1.0 + settings.market_entry_slippage_pct / 100.0)
    shifted_vol = suggest_vol(
        EQUITY, max_risk, CONTRACT_SIZE, shifted_entry, STOP, VOL_UNIT, MIN_VOL,
        side="long", slippage_pct=settings.risk_slippage_pct,
        available_usdt=AVAILABLE, leverage=5,
        max_notional_pct_of_equity=settings.max_notional_pct_of_equity,
    )
    assert body["vol"] > shifted_vol

    # (2) The gate, given the SUGGESTED vol at the SAME raw last, sizes to the
    # full budget and does NOT reject it — exact math parity.
    ticket = OrderTicket(
        symbol="BTC_USDT", side="long", order_type="market",
        vol=body["vol"], stop_loss=STOP, leverage=5,
    )
    gate = validate_order(
        ticket, _contract_meta(), EQUITY, settings, last_price=ENTRY,
        available_usdt=AVAILABLE,
    )
    assert gate.entry_for_risk == ENTRY  # gate uses raw last too
    # Suggestion sizes to (approximately) the full max_risk_pct budget; never
    # over it. Floor-to-vol_unit rounding only ever leaves it at/just under.
    assert gate.risk_pct <= max_risk + 1e-9
    assert gate.risk_pct >= max_risk - 0.05
    assert not any("MAX_RISK_PCT" in e for e in gate.errors)


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


def test_sizing_suggest_missing_liq_non_strict_no_400(monkeypatch):
    """R-01: a same-side position without liquidate_price must NOT hard-block
    sizing when strict_aggregate_risk is off (the default). A conservative
    fallback risk + a warning is used instead of a 400.
    """
    existing_position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 2,
        "entry_price": ENTRY,
        "liquidate_price": None,  # missing → used to be a hard 400
    }
    mock = _mock_client(positions=[existing_position])
    r = _post(monkeypatch, {"risk_pct": 2.0}, max_risk_pct=10.0, mock=mock)
    assert r.status_code == 200
    body = r.json()
    # Fallback exposure is applied (not silently 0) …
    assert body["existing_same_side_risk_usdt"] > 0
    # … and the user is warned about the fallback.
    assert any("liquidate_price" in w for w in body.get("warnings", []))


def test_sizing_suggest_missing_liq_strict_still_400(monkeypatch):
    """R-01: with strict_aggregate_risk=True the fail-closed behaviour is kept
    — a missing liquidate_price still yields a 400.
    """
    existing_position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 2,
        "entry_price": ENTRY,
        "liquidate_price": None,
    }
    mock = _mock_client(positions=[existing_position])
    r = _post(
        monkeypatch,
        {"risk_pct": 2.0},
        max_risk_pct=10.0,
        mock=mock,
        settings_kwargs={"strict_aggregate_risk": True},
    )
    assert r.status_code == 400
