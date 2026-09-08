"""Task 5: /api/journal + /api/journal/stats + Wilson CI + stats math + auth."""

from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.db.repo import Database
from app.journal.stats import build_stats_response, wilson_ci


# ── Pure math ──────────────────────────────────────────────────────────

def test_wilson_ci_empty_is_none():
    assert wilson_ci(0, 0) is None


def test_wilson_ci_bounds_within_unit_interval():
    lo, hi = wilson_ci(36, 24)
    assert 0.0 <= lo < hi <= 1.0
    # centre near 0.6 for 36/60
    assert lo == pytest.approx(0.474, abs=0.01)
    assert hi == pytest.approx(0.712, abs=0.01)


def test_wilson_ci_clamped_at_extremes():
    lo, hi = wilson_ci(5, 0)  # 100% wins
    assert lo >= 0.0 and hi <= 1.0


def test_build_stats_empty_db_returns_zeros():
    r = build_stats_response({}, min_sample=20)
    assert r["totals"]["proposals"] == 0
    assert r["overall"]["sample"] == 0
    assert r["overall"]["win_rate"] is None
    assert r["overall"]["win_rate_ci95"] is None
    assert r["totals"]["stay_out_rate"] is None
    # standing shadow caveat always present
    caveat = next(c for c in r["caveats"] if "Shadow eval" in c)
    assert "limits only after price touch" in caveat
    assert "net R uses a flat fee/slippage model" in caveat


def test_build_stats_math_and_flags():
    raw = {
        "total": 10, "stay_out": 4, "pending": 1, "expired": 1, "skipped": 0,
        "wins": 3, "losses": 1, "overall_sum_r": 3.0,  # e.g. 2+2+1 win R, -1 loss? sum given
        "by_confidence": {
            "high": {"wins": 3, "losses": 0, "sum_r": 6.0},
            "low": {"wins": 0, "losses": 1, "sum_r": -1.0},
        },
        "by_action": {"BUY": {"wins": 3, "losses": 1, "sum_r": 3.0}},
        "by_provider": {"claude": {"wins": 3, "losses": 1, "sum_r": 3.0}},
    }
    r = build_stats_response(raw, min_sample=20)
    assert r["totals"]["proposals"] == 10
    assert r["totals"]["stay_out"] == 4
    assert r["totals"]["stay_out_rate"] == 0.4
    assert r["totals"]["resolved"] == 4
    assert r["overall"]["sample"] == 4
    assert r["overall"]["win_rate"] == 0.75
    assert r["overall"]["avg_realized_rrr"] == 0.75
    assert r["overall"]["low_sample"] is True  # 4 < 20
    assert r["by_confidence"]["high"]["win_rate"] == 1.0
    assert r["by_confidence"]["low"]["win_rate"] == 0.0
    # low-sample groups named in a caveat
    assert any("Sample < 20" in c for c in r["caveats"])


# ── Endpoint integration ───────────────────────────────────────────────

def _seed(db: Database):
    async def go():
        base = dict(
            symbol="BTC_USDT", tf="15m", htf="1H", setup_confidence="high",
            provider="claude", model="m", scanner_summary=None, last_price_t0=100.0,
        )
        w = await db.insert_journal_entry(action="BUY", direction="long",
            entry_price=100.0, stop_loss=99.0, tp1=102.0, rrr=2.0, **base)
        l = await db.insert_journal_entry(action="SELL", direction="short",
            entry_price=100.0, stop_loss=101.0, tp1=98.0, rrr=2.0,
            **{**base, "setup_confidence": "low"})
        await db.insert_journal_entry(action="STAY_OUT", direction=None,
            entry_price=None, stop_loss=None, tp1=None, rrr=None, status="SKIPPED", **base)
        await db.update_journal_outcome(w, status="WIN", realized_r=2.0, resolved_price=102.0)
        await db.update_journal_outcome(l, status="LOSS", realized_r=-1.0, resolved_price=101.0)
    asyncio.run(go())


def test_journal_endpoints_seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        _seed(tc.app.state.db)

        r = tc.get("/api/journal?limit=50")
        assert r.status_code == 200, r.text
        entries = r.json()["entries"]
        assert len(entries) == 3
        assert {e["status"] for e in entries} == {"WIN", "LOSS", "SKIPPED"}

        rs = tc.get("/api/journal/stats")
        assert rs.status_code == 200, rs.text
        st = rs.json()
        assert st["totals"]["proposals"] == 3
        assert st["totals"]["stay_out"] == 1
        assert st["overall"]["wins"] == 1
        assert st["overall"]["losses"] == 1
        assert st["overall"]["win_rate"] == 0.5
        assert st["overall"]["avg_realized_rrr"] == 0.5
        assert st["by_action"]["BUY"]["wins"] == 1
        assert st["by_action"]["SELL"]["losses"] == 1

    get_settings.cache_clear()


def test_journal_stats_empty_db_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je_empty.db"))
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        r = tc.get("/api/journal/stats")
        assert r.status_code == 200
        assert r.json()["overall"]["sample"] == 0
        r2 = tc.get("/api/journal")
        assert r2.status_code == 200
        assert r2.json()["entries"] == []
    get_settings.cache_clear()


# ── Task 7: POST /api/journal/clear ────────────────────────────────────


def test_journal_clear_deletes_only_journal_entries(tmp_path, monkeypatch):
    """Clearing the journal must not touch proposals/orders (separate reset
    from /api/history/clear — the journal deliberately survives that one)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je_clear.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        db: Database = tc.app.state.db
        _seed(db)

        async def seed_history():
            await db.insert_proposal(
                symbol="BTC_USDT",
                proposal_json={"action": "BUY"},
                annotations_json=None,
                context_hash="hclear",
            )

        asyncio.run(seed_history())

        r_before = tc.get("/api/journal?limit=50")
        assert len(r_before.json()["entries"]) == 3

        r_clear = tc.post("/api/journal/clear")
        assert r_clear.status_code == 200, r_clear.text
        body = r_clear.json()
        assert body["ok"] is True
        assert body["deleted"] == 3

        r_after = tc.get("/api/journal?limit=50")
        assert r_after.json()["entries"] == []

        rs = tc.get("/api/journal/stats")
        assert rs.json()["totals"]["proposals"] == 0

        # proposals/orders (history) untouched by the journal clear
        hist = tc.get("/api/history")
        assert len(hist.json()["proposals"]) == 1

    get_settings.cache_clear()


def test_journal_clear_empty_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je_clear_empty.db"))
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        r = tc.post("/api/journal/clear")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "deleted": 0}
    get_settings.cache_clear()


def test_journal_clear_requires_token_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je_clear_auth.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "secret2")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        r = tc.post("/api/journal/clear")
        assert r.status_code == 401
        ok = tc.post("/api/journal/clear", headers={"X-Local-Token": "secret2"})
        assert ok.status_code == 200
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()


def test_journal_clear_without_db_returns_503(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je_clear_nodb.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        tc.app.state.db = None
        r = tc.post("/api/journal/clear")
        assert r.status_code == 503
    get_settings.cache_clear()


def test_journal_requires_token_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "je_auth.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "secret")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        r = tc.get("/api/journal")
        assert r.status_code == 401
        rs = tc.get("/api/journal/stats")
        assert rs.status_code == 401
        ok = tc.get("/api/journal", headers={"X-Local-Token": "secret"})
        assert ok.status_code == 200
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
