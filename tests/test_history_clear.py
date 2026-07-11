"""Tests for DB Database.clear_history() and POST /api/history/clear."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.db.repo import Database


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "clear_history.db")


@pytest.mark.asyncio
async def test_repo_clear_history_deletes_proposals_and_orders(db_path):
    db = Database(db_path)
    await db.init()

    await db.insert_proposal(
        symbol="BTC_USDT",
        proposal_json={"action": "BUY"},
        annotations_json=None,
        context_hash="h1",
    )
    await db.insert_proposal(
        symbol="ETH_USDT",
        proposal_json={"action": "SELL"},
        annotations_json=None,
        context_hash="h2",
    )
    await db.insert_order(
        symbol="BTC_USDT",
        side="long",
        request_json={"vol": 1},
        response_json={"orderId": 1},
        status="placed",
        error=None,
    )

    hist_before = await db.history(limit=20)
    assert len(hist_before["proposals"]) == 2
    assert len(hist_before["orders"]) == 1

    deleted = await db.clear_history()
    assert deleted == {"proposals": 2, "orders": 1}

    hist_after = await db.history(limit=20)
    assert hist_after["proposals"] == []
    assert hist_after["orders"] == []


@pytest.mark.asyncio
async def test_repo_clear_history_does_not_touch_order_previews(db_path):
    """clear_history must only wipe audit tables, not the preview-token table."""
    db = Database(db_path)
    await db.init()

    await db.insert_preview(
        token_hash="tok1",
        payload_json={"symbol": "BTC_USDT"},
        expires_at="2099-01-01T00:00:00+00:00",
    )
    await db.insert_proposal(
        symbol="BTC_USDT",
        proposal_json={"action": "BUY"},
        annotations_json=None,
        context_hash="h1",
    )

    await db.clear_history()

    async with db._connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM order_previews")
        row = await cur.fetchone()
        assert row[0] == 1  # untouched


@pytest.mark.asyncio
async def test_repo_clear_history_empty_is_noop(db_path):
    db = Database(db_path)
    await db.init()
    deleted = await db.clear_history()
    assert deleted == {"proposals": 0, "orders": 0}


def test_api_history_clear_requires_token(tmp_path, monkeypatch):
    """When LOCAL_API_TOKEN is set, POST /api/history/clear needs a matching header."""
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hist.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        r = tc.post("/api/history/clear")
        assert r.status_code == 401

        r_ok = tc.post(
            "/api/history/clear", headers={"X-Local-Token": "test-token"}
        )
        assert r_ok.status_code == 200, r_ok.text
        body = r_ok.json()
        assert body["ok"] is True
        assert body["deleted"] == {"proposals": 0, "orders": 0}

    get_settings.cache_clear()


def test_api_history_clear_deletes_seeded_rows_and_history_still_works(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "seeded_clear.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()

    import asyncio

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        db: Database = tc.app.state.db

        async def seed():
            await db.insert_proposal(
                symbol="BTC_USDT",
                proposal_json={"action": "BUY"},
                annotations_json=None,
                context_hash="h1",
            )
            await db.insert_order(
                symbol="BTC_USDT",
                side="long",
                request_json={"vol": 1},
                response_json={"ok": True},
                status="placed",
                error=None,
            )

        asyncio.run(seed())

        r = tc.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert len(body["proposals"]) == 1
        assert len(body["orders"]) == 1

        r_clear = tc.post("/api/history/clear")
        assert r_clear.status_code == 200, r_clear.text
        deleted = r_clear.json()["deleted"]
        assert deleted == {"proposals": 1, "orders": 1}

        # GET /api/history still works and is now empty (existing endpoint unbroken).
        r2 = tc.get("/api/history")
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["proposals"] == []
        assert body2["orders"] == []

    get_settings.cache_clear()


def test_api_history_clear_without_db_returns_503(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "unused.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        tc.app.state.db = None
        r = tc.post("/api/history/clear")
        assert r.status_code == 503

    get_settings.cache_clear()
