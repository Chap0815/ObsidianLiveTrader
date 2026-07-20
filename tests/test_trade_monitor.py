"""Task 5: server-side trade monitor — one-cycle behaviour (money-executing).

Drives `_run_one_cycle` directly (never the infinite loop) with a fake exchange
client + an in-memory DB. The auto-BE write path (`svc.modify_stop_loss`) is a
spy injected by monkeypatching `_make_order_service`, so no real order code runs.

Covered: armed HL at >=+1R executes auto-BE (+ be_done + feed) ; unarmed only
alerts ; disarmed never auto-acts ; a failing position doesn't abort the cycle ;
a non-HL position never auto-BEs.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repo import Database
from app.orders import monitor

NOW_MS = 1_700_000_000_000


class FakeClient:
    """Minimal exchange client. `is_hl` toggles the place_stop_order attribute
    that both the monitor and modify_stop_loss use as the HL-only gate."""

    def __init__(self, positions, mark=102.5, stops=None, is_hl=True):
        self._positions = positions
        self._mark = mark
        self._stops = stops if stops is not None else [
            {"triggerPrice": 98.0, "orderType": "Stop"}
        ]
        if is_hl:
            # Presence is all that matters (the real write goes via the spy).
            self.place_stop_order = lambda *a, **k: None

    async def account_snapshot(self):
        return {"positions": self._positions}

    async def ticker(self, symbol):
        return SimpleNamespace(last_price=self._mark)

    async def open_stop_orders(self, symbol):
        return list(self._stops)


def _pos(symbol="BTC_USDT", side="long", entry=100.0, hold=1.0):
    return {"symbol": symbol, "side": side, "entry_price": entry, "hold_vol": hold}


def _make_app(db, client):
    app = SimpleNamespace(state=SimpleNamespace())
    app.state.db = db
    app.state.mexc = client
    app.state.exchange = client
    app.state.preview_store = None
    app.state.trade_lock = None
    return app


def _install_spy(monkeypatch):
    """Replace the service seam with a spy exposing an AsyncMock modify_stop_loss."""
    svc = SimpleNamespace(modify_stop_loss=AsyncMock(return_value={"status": "ok"}))
    monkeypatch.setattr(monitor, "_make_order_service", lambda app, client, settings: svc)
    return svc


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "monitor.db")


async def _seed_open(db, *, armed):
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100.0,
        initial_sl_snap=98.0,
        r1=2.0,
        opened_at=NOW_MS,
        invalidation_price=None,
    )
    if armed:
        await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})


@pytest.mark.asyncio
async def test_armed_hl_at_1r_moves_sl_to_be(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(monkeypatch)
    # entry 100, SL 98 -> r1=2 ; mark 102.5 -> +1.25R, above the +1R trigger.
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()
    kwargs = svc.modify_stop_loss.await_args.kwargs
    assert kwargs["symbol"] == "BTC_USDT"
    assert kwargs["side"] == "long"
    # BE = entry * (1 + fee_rt=0.0006) = 100.06.
    assert kwargs["new_sl"] == pytest.approx(100.06)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1
    assert "auto_be" in row["last_alert_state"]


@pytest.mark.asyncio
async def test_unarmed_only_alerts_no_modify(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=False)
    # Thesis invalidation at 99 from the latest proposal; long mark 98.5 crosses.
    await db.insert_proposal(symbol="BTC_USDT", proposal_json={"invalidation_price": 99.0})
    svc = _install_spy(monkeypatch)
    app = _make_app(db, FakeClient([_pos()], mark=98.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert "thesis" in row["last_alert_state"]
    assert "auto_be" not in row["last_alert_state"]


@pytest.mark.asyncio
async def test_disarmed_position_no_auto_action(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=False)  # would be +1R but armed_rules empty
    svc = _install_spy(monkeypatch)
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0
    assert "auto_be" not in row["last_alert_state"]


@pytest.mark.asyncio
async def test_one_bad_position_does_not_abort_cycle(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(monkeypatch)
    # A malformed (non-dict) position raises inside _process_position; the armed
    # HL position that follows must still be processed.
    app = _make_app(db, FakeClient(["not-a-dict", _pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1


@pytest.mark.asyncio
async def test_non_hl_position_never_auto_bes(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(monkeypatch)
    # No place_stop_order attribute -> the HL-only gate blocks the write.
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=False))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0
    assert "auto_be_unavailable" in row["last_alert_state"]


@pytest.mark.asyncio
async def test_persistent_modify_failure_is_bounded(monkeypatch, db_path):
    """I-1: a persistently failing auto-BE must stop hammering after
    _BE_MAX_ATTEMPTS and go into a sticky halted state, not retry forever."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = SimpleNamespace(modify_stop_loss=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(monitor, "_make_order_service", lambda app, client, settings: svc)
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    # Run more cycles than the cap; every cycle the position is still live/armed/+1R.
    for _ in range(monitor._BE_MAX_ATTEMPTS + 3):
        await monitor._run_one_cycle(app, NOW_MS)

    # Exactly _BE_MAX_ATTEMPTS real modify attempts, then it stops calling.
    assert svc.modify_stop_loss.await_count == monitor._BE_MAX_ATTEMPTS
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0  # never succeeded → stays un-done
    assert row["last_alert_state"]["auto_be_error"]["halted"] is True


@pytest.mark.asyncio
async def test_transient_absence_does_not_disarm_before_grace(monkeypatch, db_path):
    """I-2: a single empty/partial snapshot must NOT close the mgmt record and
    wipe arming; only _CLOSE_GRACE_CYCLES consecutive absences close it."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=102.5, is_hl=True)
    app = _make_app(db, client)

    # Empty snapshot cycles (transient) — reuse the same app so absence counts persist.
    client._positions = []
    for i in range(monitor._CLOSE_GRACE_CYCLES - 1):  # one short of the grace
        await monitor._run_one_cycle(app, NOW_MS)
        row = await db.get_open_position_mgmt("BTC_USDT", "long")
        assert row is not None, "record closed too early on a transient glitch"
        assert row["armed_rules"] == {"auto_be": True}  # arming preserved

    # Reaching the grace threshold closes it.
    await monitor._run_one_cycle(app, NOW_MS)
    assert await db.get_open_position_mgmt("BTC_USDT", "long") is None


@pytest.mark.asyncio
async def test_non_hl_unavailable_alert_is_debounced(monkeypatch, db_path):
    """A non-HL armed position surfaces 'auto_be_unavailable' ONCE; the ts must
    NOT be rewritten every cycle (that would re-toast the client each poll)."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=102.5, is_hl=False)  # no place_stop_order
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)
    row1 = await db.get_open_position_mgmt("BTC_USDT", "long")
    ts1 = row1["last_alert_state"]["auto_be_unavailable"]["ts"]
    assert ts1 == NOW_MS

    # Second cycle at a LATER time — the alert must stay at the original ts.
    await monitor._run_one_cycle(app, NOW_MS + 60_000)
    row2 = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row2["last_alert_state"]["auto_be_unavailable"]["ts"] == ts1
