"""POST /api/reevaluate — advisory reevaluation of an ALREADY OPEN position.

Mirrors the /api/analyze test pattern in test_history.py: mocks the market
snapshot builder and the LLM call, then checks the endpoint wires the
position context (entry/side/pnl/current SL) into the LLM call and returns
the structured, advisory-only result untouched by risk gates/order logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.models import ReevaluateProposal


def _mock_snap():
    return {
        "symbol": "BTC_USDT",
        "last_price": 101_000.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 0.0001},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }


def _mock_position(symbol="BTC_USDT", side="long", hold_vol=1.0):
    return {
        "position_id": 1,
        "symbol": symbol,
        "side": side,
        "hold_vol": hold_vol,
        "entry_price": 100_000.0,
        "leverage": 10,
        "open_type": "isolated",
        "unrealized_pnl": 500.0,
        "realised": 0.0,
        "liquidate_price": 90_000.0,
        "im": 1000.0,
        "margin_ratio": 0.1,
        "state": 1,
    }


def _env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reevaluate_hold_result(monkeypatch):
    """Happy path: open position found, LLM (mocked) returns HOLD, response
    is the structured advisory result — and the position context (entry,
    side, pnl, current SL) actually reached the LLM call."""
    _env(monkeypatch)

    from fastapi.testclient import TestClient

    from app.main import app

    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={
            "equity_usdt": 5000.0,
            "available_usdt": 4000.0,
            "positions": [_mock_position()],
        }
    )
    client.open_stop_orders = AsyncMock(
        return_value=[{"symbol": "BTC_USDT", "stopLossPrice": 98_000.0}]
    )

    mocked_result = ReevaluateProposal(
        action="HOLD",
        confidence="medium",
        reason="Thesis intact, HTF uptrend, position in profit but no urgent action.",
        new_sl=None,
        new_tp=None,
        partial_close_pct=None,
        risk_notes="",
    )

    captured_context: dict = {}

    async def fake_reevaluate_with_llm(context, settings):
        captured_context.update(context)
        return mocked_result

    with TestClient(app) as tc:
        tc.app.state.mexc = client

        with (
            patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock())),
            patch("app.main.snapshot_to_api_dict", return_value=_mock_snap()),
            patch("app.main.reevaluate_with_llm", new=fake_reevaluate_with_llm),
        ):
            r = tc.post(
                "/api/reevaluate",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "BTC_USDT"
        assert body["reevaluation"]["action"] == "HOLD"
        assert body["reevaluation"]["confidence"] == "medium"
        assert body["annotations"]["advisory_only"] is True
        assert body["annotations"]["gates_not_bypassed"] is True

        # Position context surfaced in the response...
        assert body["position"]["entry_price"] == 100_000.0
        assert body["position"]["side"] == "long"
        assert body["position"]["unrealized_pnl"] == 500.0
        assert body["position"]["stop_loss"] == 98_000.0

        # ...and actually reached the LLM call (not just the HTTP response).
        pos_ctx = captured_context.get("position")
        assert pos_ctx is not None
        assert pos_ctx["entry_price"] == 100_000.0
        assert pos_ctx["side"] == "long"
        assert pos_ctx["unrealized_pnl"] == 500.0
        assert pos_ctx["stop_loss"] == 98_000.0
        assert pos_ctx["liquidate_price"] == 90_000.0
        # Market snapshot context also present (same shape as /api/analyze).
        assert "htf" in captured_context
        assert "ltf" in captured_context

    get_settings.cache_clear()


def test_reevaluate_no_open_position_404(monkeypatch):
    """No open position for the symbol → clear 404, not a generic 500/crash."""
    _env(monkeypatch)

    from fastapi.testclient import TestClient

    from app.main import app

    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 5000.0, "available_usdt": 5000.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        r = tc.post(
            "/api/reevaluate",
            json={"symbol": "ETH_USDT", "tf": "15m", "htf": "1H"},
        )

    assert r.status_code == 404, r.text
    assert "ETH_USDT" in r.json()["detail"]

    get_settings.cache_clear()


def test_reevaluate_llm_not_configured(monkeypatch):
    """No LLM key set → 400 with a helpful message, same as /api/analyze.

    The repo's local .env carries real keys for manual runs, so an empty
    string is set explicitly (env vars win over env_file in pydantic-
    settings) rather than deleting the var, which would fall through to the
    .env value.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CLAUDE_API_KEY", "")
    monkeypatch.setenv("XAI_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        r = tc.post(
            "/api/reevaluate",
            json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
        )

    assert r.status_code == 400
    assert "not configured" in r.json()["detail"].lower()

    get_settings.cache_clear()
