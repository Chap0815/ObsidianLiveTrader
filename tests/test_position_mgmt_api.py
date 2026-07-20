"""Task 6: arm / alerts / killswitch API (TestClient, spec §3/§4/§6).

The arm endpoint is the money-path CONTROL (it enables autonomous stop moves)
but must itself only write the mgmt DB record — never place an order. These
tests drive the real FastAPI app via TestClient with a mocked exchange client
(account_snapshot + open_stop_orders) and the app's real SQLite DB, and assert:

- arm sets armed_rules {"auto_be": true} AND freezes a baseline (r1 from the
  live SL) — without ever calling an order-placing client method;
- arm with an unknown rule name → 400;
- arm on a non-live position → 404;
- alerts returns the set alert state;
- killswitch empties every armed_rules and returns the count;
- all three endpoints require the local token when one is configured.

Seeding/verification against the DB goes through a SEPARATE ``Database`` instance
on the same file (the repo's lazy per-call connection fallback) so it never
touches the app's shared aiosqlite connection bound to the TestClient loop.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.repo import Database
from app.main import app


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mock_client(positions, *, sl_price=98.0):
    """Exchange client stub. Only the read paths arm/ensure_baseline touch are
    implemented; any order-write attribute is deliberately absent so a stray
    write would AttributeError and fail the test."""
    client = MagicMock()
    client.account_snapshot = AsyncMock(return_value={"positions": positions})
    client.open_stop_orders = AsyncMock(
        return_value=[{"triggerPrice": sl_price, "orderType": "Stop"}]
    )
    return client


def _pos(symbol="BTC_USDT", side="long", entry=100.0):
    return {"symbol": symbol, "side": side, "entry_price": entry, "hold_vol": 1.0}


async def _seed_open(db_path, symbol, *, armed=False, alert_state=None):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(
        symbol,
        "long",
        entry_snap=100.0,
        initial_sl_snap=98.0,
        r1=2.0,
        opened_at=1_700_000_000_000,
        invalidation_price=None,
    )
    if armed:
        await db.set_armed_rules(symbol, "long", {"auto_be": True})
    if alert_state is not None:
        await db.set_alert_state(symbol, "long", alert_state)


def test_arm_sets_rules_and_freezes_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_ok.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        client = _mock_client([_pos()], sl_price=98.0)
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["armed_rules"] == {"auto_be": True}
        # entry 100, live SL 98 → r1 frozen at 2.0 (spec §4).
        assert body["r1"] == pytest.approx(2.0)
        assert body["initial_sl_snap"] == pytest.approx(98.0)
        assert body["be_done"] == 0
        # It must NOT have placed/modified any order — only read paths allowed.
        called = {c[0] for c in client.method_calls}
        assert called <= {"account_snapshot", "open_stop_orders", "user_fills"}
    get_settings.cache_clear()


def test_arm_unknown_rule_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_bad_rule.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        tc.app.state.mexc = _mock_client([_pos()])
        tc.app.state.exchange = tc.app.state.mexc
        r = tc.post(
            "/api/positions/arm",
            json={
                "symbol": "BTC_USDT",
                "side": "long",
                "rules": {"auto_trail": True},
            },
        )
        assert r.status_code == 400, r.text
        assert "auto_trail" in r.text
    get_settings.cache_clear()


def test_arm_non_live_position_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_nolive.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        # No open positions → arming a phantom position must 404.
        tc.app.state.mexc = _mock_client([])
        tc.app.state.exchange = tc.app.state.mexc
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 404, r.text
    get_settings.cache_clear()


def test_alerts_returns_set_alert_state(tmp_path, monkeypatch):
    db_path = str(tmp_path / "alerts.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    _run(
        _seed_open(
            db_path,
            "BTC_USDT",
            armed=True,
            alert_state={"thesis": {"active": True, "message": "These verletzt"}},
        )
    )

    with TestClient(app) as tc:
        r = tc.get("/api/positions/alerts")
        assert r.status_code == 200, r.text
        alerts = r.json()["alerts"]
        assert len(alerts) == 1
        row = alerts[0]
        assert row["symbol"] == "BTC_USDT"
        assert row["side"] == "long"
        assert row["armed_rules"] == {"auto_be": True}
        assert row["alerts"]["thesis"]["active"] is True
    get_settings.cache_clear()


def test_killswitch_empties_all_armed_rules(tmp_path, monkeypatch):
    db_path = str(tmp_path / "killswitch.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    _run(_seed_open(db_path, "BTC_USDT", armed=True))
    _run(_seed_open(db_path, "ETH_USDT", armed=True))

    with TestClient(app) as tc:
        r = tc.post("/api/positions/killswitch")
        assert r.status_code == 200, r.text
        assert r.json()["disarmed"] == 2

    # Every OPEN row is now disarmed (read via a fresh connection).
    rows = _run(Database(db_path).list_open_position_mgmt())
    assert len(rows) == 2
    assert all(row["armed_rules"] == {} for row in rows)
    get_settings.cache_clear()


def test_endpoints_require_token_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "secret")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        tc.app.state.mexc = _mock_client([_pos()])
        tc.app.state.exchange = tc.app.state.mexc

        # No token → 401 on all three.
        assert (
            tc.post(
                "/api/positions/arm",
                json={
                    "symbol": "BTC_USDT",
                    "side": "long",
                    "rules": {"auto_be": True},
                },
            ).status_code
            == 401
        )
        assert tc.get("/api/positions/alerts").status_code == 401
        assert tc.post("/api/positions/killswitch").status_code == 401

        # With the token → authorized (killswitch is the side-effect-free check).
        ok = tc.post(
            "/api/positions/killswitch", headers={"X-Local-Token": "secret"}
        )
        assert ok.status_code == 200, ok.text
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()


def test_arm_wrong_side_rejected(tmp_path, monkeypatch):
    """Arming a side that isn't the live side must 404 (never arm a phantom)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_wrongside.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    with TestClient(app) as tc:
        client = _mock_client([_pos(side="short")])  # only a SHORT is open
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 404, r.text
    get_settings.cache_clear()


def test_arm_non_bool_rule_value_rejected(tmp_path, monkeypatch):
    """A truthy string like "false" must NOT arm — non-bool rule values → 400."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_nonbool.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    with TestClient(app) as tc:
        client = _mock_client([_pos()])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": "false"}},
        )
        assert r.status_code == 400, r.text
    get_settings.cache_clear()


def test_arm_auto_be_rejected_on_non_hl(tmp_path, monkeypatch):
    """Auto-BE is HL-only — arming it on a non-HL client must 400 at the source
    (prevents the monitor from emitting a repeating 'unavailable' alert)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_nonhl.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    with TestClient(app) as tc:
        client = _mock_client([_pos()])
        del client.place_stop_order  # non-HL: no HL stop-order method
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 400, r.text
    get_settings.cache_clear()


def test_rearm_clears_auto_be_halt(tmp_path, monkeypatch):
    """F2: re-arming must clear the sticky auto-BE halt (in-memory attempt
    counter + stale auto_be_error) so the 'neu scharfschalten' recovery works."""
    import asyncio

    db_file = str(tmp_path / "rearm.db")
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    # Seed an OPEN record carrying a stale halted auto_be_error.
    asyncio.get_event_loop_policy().new_event_loop()
    asyncio.run(
        _seed_open(
            db_file,
            "BTC_USDT",
            armed=False,
            alert_state={"auto_be_error": {"active": True, "halted": True, "ts": 1}},
        )
    )
    with TestClient(app) as tc:
        client = _mock_client([_pos()])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        tc.app.state.tm_be_attempts = {("BTC_USDT", "long"): 3}  # halted
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 200, r.text
        assert "auto_be_error" not in r.json().get("last_alert_state", {})
        assert ("BTC_USDT", "long") not in tc.app.state.tm_be_attempts
    get_settings.cache_clear()
