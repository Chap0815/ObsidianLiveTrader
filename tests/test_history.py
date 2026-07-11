"""SQLite history + GET /api/history + analyze persistence."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.db.repo import Database
from app.models import TradeProposal


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "history.db")


@pytest.mark.asyncio
async def test_repo_insert_and_recent_proposals_orders(db_path):
    db = Database(db_path)
    await db.init()

    pid = await db.insert_proposal(
        symbol="BTC_USDT",
        proposal_json={
            "action": "BUY",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "rrr": 2.5,
        },
        annotations_json={"advisory_only": True},
        context_hash="abc123",
    )
    assert pid >= 1

    oid = await db.insert_order(
        symbol="BTC_USDT",
        side="long",
        request_json={"symbol": "BTC_USDT", "vol": 1},
        response_json={"orderId": 1},
        status="placed",
        error=None,
    )
    assert oid >= 1

    hist = await db.history(limit=20)
    assert len(hist["proposals"]) == 1
    assert hist["proposals"][0]["symbol"] == "BTC_USDT"
    assert hist["proposals"][0]["proposal"]["action"] == "BUY"
    assert hist["proposals"][0]["annotations"]["advisory_only"] is True
    assert hist["proposals"][0]["context_hash"] == "abc123"

    assert len(hist["orders"]) == 1
    assert hist["orders"][0]["status"] == "placed"
    assert hist["orders"][0]["request"]["vol"] == 1
    assert hist["orders"][0]["response"]["orderId"] == 1


@pytest.mark.asyncio
async def test_history_limit(db_path):
    db = Database(db_path)
    await db.init()
    for i in range(5):
        await db.insert_proposal(
            symbol="ETH_USDT",
            proposal_json={"action": "STAY_OUT", "n": i},
            annotations_json=None,
            context_hash=str(i),
        )
    rows = await db.recent_proposals(limit=3)
    assert len(rows) == 3
    # Newest first
    assert rows[0]["proposal"]["n"] == 4
    assert rows[2]["proposal"]["n"] == 2


def test_api_history_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api_hist.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        r = tc.get("/api/history")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["proposals"] == []
        assert body["orders"] == []
        assert body.get("limit") == 20

    get_settings.cache_clear()


def test_api_history_after_seed(tmp_path, monkeypatch):
    path = str(tmp_path / "seeded.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    get_settings.cache_clear()

    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        # lifespan inits DB; seed via app.state.db
        db: Database = tc.app.state.db

        async def seed():
            await db.insert_proposal(
                symbol="BTC_USDT",
                proposal_json={"action": "SELL", "rrr": 3.0},
                annotations_json={"tf": "15m"},
                context_hash="h1",
            )
            await db.insert_order(
                symbol="BTC_USDT",
                side="short",
                request_json={"side": 3},
                response_json={"ok": True},
                status="placed",
                error=None,
            )

        asyncio.run(seed())

        r = tc.get("/api/history?limit=10")
        assert r.status_code == 200
        body = r.json()
        assert len(body["proposals"]) == 1
        assert body["proposals"][0]["proposal"]["action"] == "SELL"
        assert len(body["orders"]) == 1
        assert body["orders"][0]["side"] == "short"

    get_settings.cache_clear()


def _minimal_proposal() -> TradeProposal:
    return TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100_000.0,
        stop_loss=99_000.0,
        tp1=102_000.0,
        tp2=103_000.0,
        rrr=2.0,
        recommended_leverage="5x",
        trigger_entry_zone="near support",
        key_levels={
            "immediate_support": 99_500.0,
            "immediate_resistance": 101_000.0,
            "major_liquidity_pools": [],
        },
        funding_alert="neutral",
        management={"move_sl_to_be": "after TP1", "early_invalidation": "below SL"},
        rationale="test proposal for history",
    )


def test_analyze_persists_proposal(tmp_path, monkeypatch):
    """Successful /api/analyze writes a proposals row (Grok mocked)."""
    path = str(tmp_path / "analyze_hist.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    mock_snap = {
        "symbol": "BTC_USDT",
        "last_price": 100_000.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 0.0001},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }

    with TestClient(app) as tc:
        client = MagicMock()
        client.account_snapshot = AsyncMock(
            return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
        )
        tc.app.state.mexc = client

        with (
            patch(
                "app.main.build_market_snapshot",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("app.main.snapshot_to_api_dict", return_value=mock_snap),
            patch(
                "app.main.analyze_with_llm",
                new=AsyncMock(return_value=_minimal_proposal()),
            ),
            patch("app.main.build_llm_context", return_value={"symbol": "BTC_USDT"}),
        ):
            r = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["proposal"]["action"] == "BUY"

        hr = tc.get("/api/history")
        assert hr.status_code == 200
        hist = hr.json()
        assert len(hist["proposals"]) >= 1
        assert hist["proposals"][0]["symbol"] == "BTC_USDT"
        assert hist["proposals"][0]["proposal"]["action"] == "BUY"
        assert hist["proposals"][0]["annotations"]["advisory_only"] is True

    get_settings.cache_clear()
