"""GET /api/account — balance + positions mapping."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.mexc.client import MexcClient, map_account_snapshot, map_position, usdt_balances
from app.mexc.errors import MexcError


SAMPLE_ASSETS = [
    {
        "currency": "USDT",
        "positionMargin": 10.0,
        "availableBalance": 90.5,
        "cashBalance": 100.0,
        "frozenBalance": 0,
        "equity": 100.25,
        "unrealized": 0.25,
    },
    {
        "currency": "BTC",
        "availableBalance": 0.01,
        "equity": 0.01,
    },
]

SAMPLE_POSITIONS = [
    {
        "positionId": 1109973831,
        "symbol": "BTC_USDT",
        "positionType": 1,
        "openType": 1,
        "state": 1,
        "holdVol": 5,
        "holdAvgPrice": 109777.5,
        "openAvgPrice": 109777.5,
        "liquidatePrice": 55020.5,
        "im": 27.444375,
        "realised": 0,
        "leverage": 2,
        "marginRatio": 0.0027,
        "unRealizedPnl": -0.0039,
    }
]


def test_usdt_balances_prefers_usdt_row():
    equity, available = usdt_balances(SAMPLE_ASSETS)
    assert equity == 100.25
    assert available == 90.5


def test_usdt_balances_missing_usdt():
    equity, available = usdt_balances([{"currency": "BTC", "equity": 1, "availableBalance": 1}])
    assert equity == 0.0
    assert available == 0.0


def test_map_position_long_isolated():
    p = map_position(SAMPLE_POSITIONS[0])
    assert p["symbol"] == "BTC_USDT"
    assert p["side"] == "long"
    assert p["open_type"] == "isolated"
    assert p["hold_vol"] == 5.0
    assert p["entry_price"] == 109777.5
    assert p["leverage"] == 2
    assert p["unrealized_pnl"] == -0.0039
    assert p["position_id"] == 1109973831


def test_map_position_short_cross():
    p = map_position(
        {
            "positionId": 1,
            "symbol": "ETH_USDT",
            "positionType": 2,
            "openType": 2,
            "holdVol": "3",
            "openAvgPrice": 3000,
            "leverage": 5,
            "unRealizedPnl": 1.2,
        }
    )
    assert p["side"] == "short"
    assert p["open_type"] == "cross"
    assert p["entry_price"] == 3000.0
    assert p["hold_vol"] == 3.0


def test_map_position_default_contract_size_is_one():
    """No contract_size passed in -> safe default of 1.0 (F-10)."""
    p = map_position(SAMPLE_POSITIONS[0])
    assert p["contract_size"] == 1.0


def test_map_position_carries_own_contract_size():
    """Each position exposes its OWN contract_size, not the active chart symbol's (F-10)."""
    p = map_position(SAMPLE_POSITIONS[0], contract_size=0.0001)
    assert p["contract_size"] == 0.0001

    # A different position with a different contract size must not be affected.
    other = map_position(
        {
            "positionId": 2,
            "symbol": "SHIB_USDT",
            "positionType": 1,
            "openType": 1,
            "holdVol": 100,
            "holdAvgPrice": 0.00002,
            "leverage": 10,
            "unRealizedPnl": 0.0,
        },
        contract_size=10000.0,
    )
    assert other["contract_size"] == 10000.0
    assert p["contract_size"] == 0.0001


def test_map_account_snapshot():
    snap = map_account_snapshot(SAMPLE_ASSETS, SAMPLE_POSITIONS)
    assert snap["equity_usdt"] == 100.25
    assert snap["available_usdt"] == 90.5
    assert snap["error"] is None
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["symbol"] == "BTC_USDT"
    # No contract_sizes lookup supplied -> safe default, never the other
    # position's or the active chart symbol's contract size (F-10).
    assert snap["positions"][0]["contract_size"] == 1.0


def test_map_account_snapshot_per_symbol_contract_size():
    """Two open positions on different symbols each keep their OWN contract
    size — this is the actual F-10 fix (previously the frontend applied the
    active chart symbol's contractSize to every position)."""
    positions = SAMPLE_POSITIONS + [
        {
            "positionId": 2,
            "symbol": "SHIB_USDT",
            "positionType": 1,
            "openType": 1,
            "holdVol": 100,
            "holdAvgPrice": 0.00002,
            "leverage": 10,
            "unRealizedPnl": 0.0,
        }
    ]
    snap = map_account_snapshot(
        SAMPLE_ASSETS,
        positions,
        contract_sizes={"BTC_USDT": 0.0001, "SHIB_USDT": 10000.0},
    )
    by_symbol = {p["symbol"]: p["contract_size"] for p in snap["positions"]}
    assert by_symbol == {"BTC_USDT": 0.0001, "SHIB_USDT": 10000.0}


@pytest.mark.asyncio
async def test_mexc_account_snapshot_resolves_per_symbol_contract_size():
    """MexcClient.account_snapshot() wires real contract metadata into each
    position, so opening BTC (small contract size) and SHIB (large contract
    size) at once no longer share one (wrong) contract size (F-10)."""
    c = MexcClient("https://contract.mexc.com", "k", "s")

    async def fake_request(method, path, *, params=None, private=False, **kw):
        if path == "/api/v1/private/account/assets":
            return SAMPLE_ASSETS
        if path == "/api/v1/private/position/open_positions":
            return SAMPLE_POSITIONS + [
                {
                    "positionId": 2,
                    "symbol": "SHIB_USDT",
                    "positionType": 1,
                    "openType": 1,
                    "holdVol": 100,
                    "holdAvgPrice": 0.00002,
                    "leverage": 10,
                    "unRealizedPnl": 0.0,
                }
            ]
        if path == "/api/v1/contract/detail":
            return [
                {"symbol": "BTC_USDT", "contractSize": 0.0001},
                {"symbol": "SHIB_USDT", "contractSize": 10000.0},
                {"symbol": "ETH_USDT", "contractSize": 0.01},
            ]
        raise AssertionError(f"unexpected path {path}")

    c._request = fake_request  # type: ignore[assignment]
    snap = await c.account_snapshot()
    by_symbol = {p["symbol"]: p["contract_size"] for p in snap["positions"]}
    assert by_symbol == {"BTC_USDT": 0.0001, "SHIB_USDT": 10000.0}


@pytest.mark.asyncio
async def test_mexc_account_snapshot_defaults_when_contract_detail_fails():
    """If the contract-metadata lookup errors, positions still come back with
    the safe 1.0 default instead of the whole /api/account call failing."""
    c = MexcClient("https://contract.mexc.com", "k", "s")

    async def fake_request(method, path, *, params=None, private=False, **kw):
        if path == "/api/v1/private/account/assets":
            return SAMPLE_ASSETS
        if path == "/api/v1/private/position/open_positions":
            return SAMPLE_POSITIONS
        if path == "/api/v1/contract/detail":
            raise MexcError("contract detail unavailable")
        raise AssertionError(f"unexpected path {path}")

    c._request = fake_request  # type: ignore[assignment]
    snap = await c.account_snapshot()
    assert snap["positions"][0]["contract_size"] == 1.0


def test_account_keys_not_configured(monkeypatch):
    from app.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(
            exchange="mexc", mexc_api_key="", mexc_api_secret="", hl_private_key=""
        ),
    )
    with TestClient(app) as client:
        r = client.get("/api/account")
    assert r.status_code == 200
    body = r.json()
    assert "not configured" in (body["error"] or "").lower()
    assert body["equity_usdt"] == 0.0
    assert body["available_usdt"] == 0.0
    assert body["positions"] == []


def test_account_success_with_mock_client(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(
            exchange="mexc", mexc_api_key="k", mexc_api_secret="s"
        ),
    )
    mock = MagicMock()
    mock.account_snapshot = AsyncMock(
        return_value=map_account_snapshot(SAMPLE_ASSETS, SAMPLE_POSITIONS)
    )
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get("/api/account")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None
    assert body["equity_usdt"] == 100.25
    assert body["available_usdt"] == 90.5
    assert body["positions"][0]["side"] == "long"
    mock.account_snapshot.assert_awaited_once()


def test_account_mexc_error_returns_200_with_error(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(
            exchange="mexc", mexc_api_key="k", mexc_api_secret="s"
        ),
    )
    mock = MagicMock()
    mock.account_snapshot = AsyncMock(side_effect=MexcError("signature invalid"))
    with TestClient(app) as client:
        client.app.state.mexc = mock
        client.app.state.exchange = mock
        r = client.get("/api/account")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] == "signature invalid"
    assert body["equity_usdt"] == 0.0
    assert body["positions"] == []
