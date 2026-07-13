"""Task 2: /api/analyze logs a journal entry (soft-fail, STAY_OUT->SKIPPED)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.models import TradeProposal


def _buy_proposal() -> TradeProposal:
    return TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        setup_confidence="high",
        entry_price=100_000.0,
        stop_loss=99_000.0,
        tp1=102_000.0,
        rrr=2.0,
        rationale="buy",
    )


def _stay_out_proposal() -> TradeProposal:
    return TradeProposal(
        htf_trend="ranging",
        ltf_trend="ranging",
        action="STAY_OUT",
        setup_confidence="low",
        rationale="no setup",
    )


_MOCK_SNAP = {
    "symbol": "BTC_USDT",
    "last_price": 100_500.0,
    "funding": {},
    "contract": {"apiAllowed": True, "contractSize": 0.0001},
    "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
    "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
}


def _client_ctx():
    return (
        patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock())),
        patch("app.main.snapshot_to_api_dict", return_value=_MOCK_SNAP),
        patch("app.main.build_llm_context", return_value={"symbol": "BTC_USDT"}),
    )


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "j.db"))
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    get_settings.cache_clear()


def test_analyze_logs_pending_buy_row(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        client = MagicMock()
        client.account_snapshot = AsyncMock(
            return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
        )
        tc.app.state.mexc = client
        b, snap_p, ctx = _client_ctx()
        with b, snap_p, ctx, patch(
            "app.main.analyze_with_llm", new=AsyncMock(return_value=_buy_proposal())
        ):
            r = tc.post("/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H",
                                              "scanner_verdict": {"bias": "long", "setup": "breakout", "score": 0.8}})
        assert r.status_code == 200, r.text

        import asyncio
        rows = asyncio.run(tc.app.state.db.recent_journal())
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "BUY"
        assert row["direction"] == "long"
        assert row["status"] == "PENDING"
        assert row["setup_confidence"] == "high"
        assert row["entry_price"] == 100_000.0
        assert row["stop_loss"] == 99_000.0
        assert row["tp1"] == 102_000.0
        assert row["provider"] == "claude"
        assert row["model"] == "claude-sonnet-5"
        assert row["scanner_summary"] == "long/breakout/0.8"
        assert row["last_price_t0"] == 100_500.0
    get_settings.cache_clear()


def test_analyze_stay_out_logs_skipped(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        client = MagicMock()
        client.account_snapshot = AsyncMock(
            return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
        )
        tc.app.state.mexc = client
        b, snap_p, ctx = _client_ctx()
        with b, snap_p, ctx, patch(
            "app.main.analyze_with_llm", new=AsyncMock(return_value=_stay_out_proposal())
        ):
            r = tc.post("/api/analyze", json={"symbol": "BTC_USDT"})
        assert r.status_code == 200, r.text

        import asyncio
        rows = asyncio.run(tc.app.state.db.recent_journal())
        assert len(rows) == 1
        assert rows[0]["action"] == "STAY_OUT"
        assert rows[0]["status"] == "SKIPPED"
        assert rows[0]["direction"] is None
    get_settings.cache_clear()


def test_analyze_still_200_when_journal_write_raises(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        client = MagicMock()
        client.account_snapshot = AsyncMock(
            return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
        )
        tc.app.state.mexc = client
        # Make the journal write blow up; analyze must still return 200.
        tc.app.state.db.insert_journal_entry = AsyncMock(side_effect=RuntimeError("boom"))
        b, snap_p, ctx = _client_ctx()
        with b, snap_p, ctx, patch(
            "app.main.analyze_with_llm", new=AsyncMock(return_value=_buy_proposal())
        ):
            r = tc.post("/api/analyze", json={"symbol": "BTC_USDT"})
        assert r.status_code == 200, r.text
        assert r.json()["proposal"]["action"] == "BUY"
    get_settings.cache_clear()
