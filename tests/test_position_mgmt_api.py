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
    client.place_stop_order = AsyncMock()
    client.account_snapshot = AsyncMock(return_value={"positions": positions})
    client.open_stop_orders = AsyncMock(
        return_value=[
            {"symbol": "BTC", "triggerPrice": sl_price, "orderType": "Stop"}
        ]
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


async def _set_raw_management_controls(db_path, *, armed_rules, be_done):
    db = Database(db_path)
    async with db._acquire() as conn:
        await conn.execute(
            """
            UPDATE position_management
            SET armed_rules = ?, be_done = ?
            WHERE symbol = 'BTC_USDT' AND side = 'long' AND status = 'OPEN'
            """,
            (armed_rules, be_done),
        )
        await conn.commit()


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
        # Arming enables autonomous stop mutations and freezes the live
        # baseline, so Hyperliquid's bounded-stale display cache must not be
        # accepted here.
        client.account_snapshot.assert_awaited_once_with(fresh=True)
        # It must NOT have placed/modified any order — only read paths allowed.
        called = {c[0] for c in client.method_calls}
        assert called <= {"account_snapshot", "open_stop_orders", "user_fills"}
    get_settings.cache_clear()


def test_arm_rule_patch_preserves_concurrent_other_rule(tmp_path, monkeypatch):
    """A stale tab changing Auto-BE must not overwrite the current Trail rule."""
    db_path = str(tmp_path / "arm_patch.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    _run(_seed_open(db_path, "BTC_USDT"))
    _run(
        Database(db_path).set_armed_rules(
            "BTC_USDT", "long", {"auto_trail": True}
        )
    )

    with TestClient(app) as tc:
        client = _mock_client([_pos()])
        tc.app.state.mexc = client
        tc.app.state.exchange = client

        enable = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert enable.status_code == 200, enable.text
        assert enable.json()["armed_rules"] == {
            "auto_be": True,
            "auto_trail": True,
        }

        client.account_snapshot.reset_mock()
        client.open_stop_orders.reset_mock()
        disable = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": False}},
        )
        assert disable.status_code == 200, disable.text
        assert disable.json()["armed_rules"] == {"auto_trail": True}
        client.account_snapshot.assert_not_awaited()
        client.open_stop_orders.assert_not_awaited()

    row = _run(Database(db_path).get_open_position_mgmt("BTC_USDT", "long"))
    assert row["armed_rules"] == {"auto_trail": True}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_arm_resolves_replacement_client_after_trade_lock(tmp_path):
    """A queued arm must validate the client installed while it was waiting."""
    from types import SimpleNamespace

    import app.main as main
    from app.models import ArmRequest

    db = Database(str(tmp_path / "arm_client_swap.db"))
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100.0,
        initial_sl_snap=98.0,
        r1=2.0,
        opened_at=1_700_000_000_000,
        invalidation_price=None,
    )
    old_client = _mock_client([_pos()])
    new_client = _mock_client([_pos()])
    entered = asyncio.Event()
    release = asyncio.Event()

    class GateLock:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    state = SimpleNamespace(
        db=db,
        mexc=old_client,
        exchange=old_client,
        trade_lock=GateLock(),
        tm_be_attempts={},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    task = asyncio.create_task(
        main.positions_arm(
            request,
            ArmRequest(
                symbol="BTC_USDT", side="long", rules={"auto_be": True}
            ),
            None,
        )
    )
    await entered.wait()

    state.mexc = new_client
    state.exchange = new_client
    release.set()
    result = await task

    assert result["armed_rules"] == {"auto_be": True}
    old_client.account_snapshot.assert_not_awaited()
    new_client.account_snapshot.assert_awaited_once_with(fresh=True)


def test_disarm_all_rules_succeeds_without_exchange_read(tmp_path, monkeypatch):
    """Turning automation off is local control and must survive an exchange outage."""
    db_path = str(tmp_path / "disarm_offline.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    _run(_seed_open(db_path, "BTC_USDT", armed=True))

    with TestClient(app) as tc:
        client = _mock_client([])
        client.account_snapshot = AsyncMock(side_effect=RuntimeError("exchange down"))
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        response = tc.post(
            "/api/positions/arm",
            json={
                "symbol": "BTC_USDT",
                "side": "long",
                "rules": {"auto_be": False, "auto_trail": False},
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["armed_rules"] == {}
        client.account_snapshot.assert_not_awaited()
        client.open_stop_orders.assert_not_awaited()

    row = _run(Database(db_path).get_open_position_mgmt("BTC_USDT", "long"))
    assert row["armed_rules"] == {}
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
                "rules": {"auto_moon": True},
            },
        )
        assert r.status_code == 400, r.text
        assert "auto_moon" in r.text
    get_settings.cache_clear()


def test_arm_non_live_position_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_nolive.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()


def test_arm_account_error_does_not_reflect_diagnostics(tmp_path, monkeypatch):
    from app.mexc.errors import MexcError

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_account_error.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    marker = "SYNTHETIC_PRIVATE_ARM_ACCOUNT_ERROR"

    with TestClient(app) as tc:
        client = _mock_client([])
        client.account_snapshot = AsyncMock(side_effect=MexcError(marker))
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        response = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Exchange account data unavailable"
    assert marker not in response.text
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


def test_arm_duplicate_live_position_rejected_before_baseline_write(tmp_path, monkeypatch):
    """Ambiguous exchange identity must not freeze an arbitrary auto-rule baseline."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_duplicate.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        client = _mock_client([_pos(entry=100.0), _pos(entry=110.0)])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 409, r.text
        assert "multiple live" in r.text.lower()
        client.open_stop_orders.assert_not_awaited()
    get_settings.cache_clear()


@pytest.mark.parametrize("bad_entry", [10**400, True, "100.0"])
def test_arm_invalid_entry_rejected_before_baseline_read(
    tmp_path, monkeypatch, bad_entry
):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_overflowed_entry.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        client = _mock_client([_pos(entry=bad_entry)])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 400, r.text
        assert "usable entry price" in r.text
        client.open_stop_orders.assert_not_awaited()
    get_settings.cache_clear()


@pytest.mark.parametrize("bad_hold", [None, 0, -1, True, "bad", "1.0", 10**400])
def test_arm_rejects_invalid_live_position_size_before_baseline_read(
    tmp_path, monkeypatch, bad_hold
):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_invalid_hold.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        client = _mock_client([_pos() | {"hold_vol": bad_hold}])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 502, r.text
        assert "account position data" in r.text.lower()
        client.open_stop_orders.assert_not_awaited()
    get_settings.cache_clear()


def test_arm_missing_position_collection_is_upstream_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_missing_positions.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        client = _mock_client([])
        client.account_snapshot = AsyncMock(return_value={})
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 502, r.text
        assert "account position data" in r.text.lower()
        client.open_stop_orders.assert_not_awaited()
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "bad_position",
    [{}, _pos(symbol="BTC_USDT#SYNTHETIC_PRIVATE_POSITION")],
)
def test_arm_invalid_position_row_blocks_valid_match(
    tmp_path, monkeypatch, bad_position
):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_invalid_position_row.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        client = _mock_client([_pos(), bad_position])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 502, r.text
        assert "account position data" in r.text.lower()
        assert "SYNTHETIC_PRIVATE_POSITION" not in r.text
        client.open_stop_orders.assert_not_awaited()
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
            alert_state={
                "thesis": {
                    "active": True,
                    "message": "These verletzt",
                    "ts": 123,
                }
            },
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
        assert row["alerts"]["thesis"]["message"] == (
            "The thesis invalidation level was crossed. Check the position."
        )
    get_settings.cache_clear()


def test_alerts_allowlists_persisted_feed_without_reflecting_text(tmp_path, monkeypatch):
    db_path = str(tmp_path / "alerts_public_contract.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    marker = "SYNTHETIC_PRIVATE_LEGACY_PROVIDER_ERROR"
    _run(
        _seed_open(
            db_path,
            "BTC_USDT",
            alert_state={
                "auto_be_error": {
                    "active": True,
                    "halted": True,
                    "message": marker,
                    "ts": 456,
                    "provider_response": marker,
                },
                "unknown_kind": {
                    "active": True,
                    "message": marker,
                    "ts": 456,
                },
                "time_stop": {"active": "true", "message": marker, "ts": 456},
            },
        )
    )

    with TestClient(app) as tc:
        r = tc.get("/api/positions/alerts")

    assert r.status_code == 200, r.text
    alerts = r.json()["alerts"][0]["alerts"]
    assert alerts == {
        "auto_be_error": {
            "active": True,
            "halted": True,
            "message": (
                "Auto-management is stopped. Check the live stop state and arm it "
                "again."
            ),
            "ts": 456,
        }
    }
    assert marker not in r.text
    get_settings.cache_clear()


def test_alerts_fail_closed_for_malformed_persisted_controls(tmp_path, monkeypatch):
    db_path = str(tmp_path / "alerts_malformed_controls.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    _run(_seed_open(db_path, "BTC_USDT"))
    _run(
        _set_raw_management_controls(
            db_path,
            armed_rules='{"auto_be":"false","auto_trail":1,"unknown":true}',
            be_done="",
        )
    )

    with TestClient(app) as tc:
        r = tc.get("/api/positions/alerts")
        assert r.status_code == 200, r.text
        row = r.json()["alerts"][0]

    assert row["armed_rules"] == {"auto_be": False, "auto_trail": False}
    assert row["be_done"] is None
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        ["SYNTHETIC_PRIVATE_NON_OBJECT_ROW"],
        [
            {
                "symbol": "SYNTHETIC_PRIVATE_WRONG_SYMBOL",
                "side": "long",
                "armed_rules": {},
                "be_done": 0,
                "last_alert_state": {},
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "SYNTHETIC_PRIVATE_WRONG_SIDE",
                "armed_rules": {},
                "be_done": 0,
                "last_alert_state": {},
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "armed_rules": {},
                "be_done": 0,
                "last_alert_state": {},
            },
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "armed_rules": {"auto_be": True},
                "be_done": 1,
                "last_alert_state": {},
            },
        ],
    ],
)
async def test_alerts_reject_invalid_or_duplicate_persisted_identity(rows, caplog):
    from types import SimpleNamespace

    import app.main as main

    db = MagicMock()
    db.list_open_position_mgmt = AsyncMock(return_value=rows)
    state = SimpleNamespace(
        db=db,
        mexc=SimpleNamespace(exchange_id="mexc"),
        exchange=SimpleNamespace(exchange_id="mexc"),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    with pytest.raises(main.HTTPException) as exc:
        await main.positions_alerts(request, None)

    assert exc.value.status_code == 503
    assert exc.value.detail == "position management alerts unavailable"
    assert "SYNTHETIC_PRIVATE" not in str(exc.value.detail)
    assert "SYNTHETIC_PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_alerts_accept_canonical_hyperliquid_identity():
    from types import SimpleNamespace

    import app.main as main

    db = MagicMock()
    db.list_open_position_mgmt = AsyncMock(
        return_value=[
            {
                "symbol": "BTC",
                "side": "short",
                "armed_rules": {"auto_trail": True},
                "be_done": 0,
                "last_alert_state": {},
            }
        ]
    )
    client = SimpleNamespace(exchange_id="hyperliquid")
    state = SimpleNamespace(db=db, mexc=client, exchange=client)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    result = await main.positions_alerts(request, None)

    assert result["alerts"][0]["symbol"] == "BTC"
    assert result["alerts"][0]["side"] == "short"
    assert result["alerts"][0]["armed_rules"] == {"auto_trail": True}


def test_alerts_db_failure_is_unavailable_not_empty(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "alerts_error.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        original_db = tc.app.state.db
        failing_db = MagicMock()
        marker = "SYNTHETIC_PRIVATE_ALERT_DB_ERROR"
        failing_db.list_open_position_mgmt = AsyncMock(side_effect=RuntimeError(marker))
        tc.app.state.db = failing_db
        try:
            r = tc.get("/api/positions/alerts")
            tc.app.state.db = None
            missing = tc.get("/api/positions/alerts")
        finally:
            tc.app.state.db = original_db

        assert r.status_code == 503, r.text
        assert marker not in r.text
        assert missing.status_code == 503, missing.text
        assert marker not in caplog.text
        assert "type=RuntimeError" in caplog.text
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
        client.exchange_id = "mexc"
        # A method name alone must not grant Hyperliquid-only capabilities.
        client.place_stop_order = AsyncMock()
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_be": True}},
        )
        assert r.status_code == 400, r.text
    get_settings.cache_clear()


def test_arm_auto_trail_rejected_on_non_hl(tmp_path, monkeypatch):
    """Auto-Trail is HL-only (drives modify_stop_loss) — arming it on a non-HL
    client must 400 at the source, exactly like auto_be (V4 whitelist + reject)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "arm_trail_nonhl.db"))
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    with TestClient(app) as tc:
        client = _mock_client([_pos()])
        client.exchange_id = "mexc"
        client.place_stop_order = AsyncMock()
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post(
            "/api/positions/arm",
            json={"symbol": "BTC_USDT", "side": "long", "rules": {"auto_trail": True}},
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


def test_rearm_auto_trail_clears_shared_auto_management_halt(tmp_path, monkeypatch):
    """Re-arming Trail resets the retry state shared by both automatic rules."""
    db_file = str(tmp_path / "rearm_trail.db")
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    get_settings.cache_clear()
    _run(
        _seed_open(
            db_file,
            "BTC_USDT",
            alert_state={"auto_be_error": {"active": True, "halted": True, "ts": 1}},
        )
    )

    with TestClient(app) as tc:
        client = _mock_client([_pos()])
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        tc.app.state.tm_be_attempts = {("BTC_USDT", "long"): 3}

        response = tc.post(
            "/api/positions/arm",
            json={
                "symbol": "BTC_USDT",
                "side": "long",
                "rules": {"auto_trail": True},
            },
        )

        assert response.status_code == 200, response.text
        assert "auto_be_error" not in response.json().get("last_alert_state", {})
        assert ("BTC_USDT", "long") not in tc.app.state.tm_be_attempts
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_rearm_alert_cleanup_failure_does_not_secretly_enable_rule(monkeypatch):
    """A failed re-arm response must not leave autonomous trading enabled."""
    from types import SimpleNamespace

    import app.main as main
    import app.orders.monitor as monitor
    from app.models import ArmRequest

    db = MagicMock()
    db.get_open_position_mgmt = AsyncMock(
        return_value={
            "symbol": "BTC_USDT",
            "side": "long",
            "armed_rules": {},
            "last_alert_state": {
                "auto_be_error": {"active": True, "halted": True, "ts": 1}
            },
        }
    )
    db.set_armed_rules = AsyncMock()
    db.set_alert_state = AsyncMock(side_effect=RuntimeError("synthetic DB failure"))
    client = _mock_client([_pos()])
    state = SimpleNamespace(
        db=db,
        mexc=client,
        exchange=client,
        trade_lock=asyncio.Lock(),
        tm_be_attempts={("BTC_USDT", "long"): 3},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    monkeypatch.setattr(monitor, "ensure_baseline", AsyncMock())

    with pytest.raises(RuntimeError, match="synthetic DB failure"):
        await main.positions_arm(
            request,
            ArmRequest(
                symbol="BTC_USDT", side="long", rules={"auto_trail": True}
            ),
            None,
        )

    db.set_armed_rules.assert_not_awaited()
    assert state.tm_be_attempts[("BTC_USDT", "long")] == 3


@pytest.mark.asyncio
async def test_disarm_succeeds_when_stale_alert_cleanup_fails(monkeypatch, caplog):
    """Local disarming remains authoritative when cosmetic DB cleanup fails."""
    from types import SimpleNamespace

    import app.main as main
    from app.models import ArmRequest

    stale_alert = {"auto_be_error": {"active": True, "halted": True, "ts": 1}}
    db = MagicMock()
    db.get_open_position_mgmt = AsyncMock(
        return_value={
            "symbol": "BTC_USDT",
            "side": "long",
            "armed_rules": {"auto_trail": True},
            "last_alert_state": stale_alert,
        }
    )
    db.set_armed_rules = AsyncMock()
    marker = "SYNTHETIC_PRIVATE_DISARM_DB_DETAIL"
    db.set_alert_state = AsyncMock(side_effect=RuntimeError(marker))
    attempts = {("BTC_USDT", "long"): 3}
    state = SimpleNamespace(
        db=db,
        trade_lock=asyncio.Lock(),
        tm_be_attempts=attempts,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    result = await main.positions_arm(
        request,
        ArmRequest(
            symbol="BTC_USDT", side="long", rules={"auto_trail": False}
        ),
        None,
    )

    db.set_armed_rules.assert_awaited_once_with("BTC_USDT", "long", {})
    db.set_alert_state.assert_awaited_once_with("BTC_USDT", "long", {})
    assert result["armed_rules"] == {}
    assert result["last_alert_state"] == stale_alert
    assert ("BTC_USDT", "long") not in attempts
    assert marker not in caplog.text
    assert "RuntimeError" in caplog.text
