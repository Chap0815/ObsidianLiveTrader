"""TML state-machine hardening — regression tests for five verified findings.

Each test first pins the BUG (red) before the corresponding fix lands:

- F1: frozen r1==0 (position opened without a bracket SL) is never healed when a
      real SL later appears → auto-BE/trail/time-stop R-gates never fire.
- F2: a close+reopen of the same (symbol, side) at ~the same entry inside one
      cycle inherits be_done + a stale high_water because the reset only hangs on
      the absence counter; a changed exchange open-signature must hard-reset.
- F3: the trail emits on every micro-improvement (no min step) → modify churn.
- F4: a deliberate manual SL loosening is immediately overridden by the trail.
- F5: the opened_at journal fallback picks the NEWEST matching row → every new
      proposal "rejuvenates" the position and the time-stop never triggers.
"""
from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repo import Database
from app.orders import monitor
from app.orders.trade_manager import MgmtBaseline, MoveSlToBe, evaluate_rules

NOW_MS = 1_700_000_000_000


# ── shared harness (mirrors tests/test_trade_monitor.py) ─────────────────────
class FakeClient:
    def __init__(self, positions, mark=102.5, stops=None, is_hl=True, fills=None):
        self._positions = positions
        self._mark = mark
        self._stops = stops if stops is not None else [
            {"triggerPrice": 98.0, "orderType": "Stop"}
        ]
        self._fills = fills  # None => no user_fills attr at all (MEXC-like)
        if is_hl:
            self.place_stop_order = lambda *a, **k: None
        if fills is not None:
            self.user_fills = self._user_fills

    async def _user_fills(self, symbol=None, limit=100):
        return list(self._fills)

    async def account_snapshot(self, *, fresh=False):
        return {"positions": self._positions}

    async def ticker(self, symbol):
        return SimpleNamespace(last_price=self._mark)

    async def open_stop_orders(self, symbol):
        return list(self._stops)


def _pos(symbol="BTC_USDT", side="long", entry=100.0, hold=1.0, position_id=None):
    p = {"symbol": symbol, "side": side, "entry_price": entry, "hold_vol": hold}
    if position_id is not None:
        p["position_id"] = position_id
    return p


def _open_fill(time, start_position=0.0, side="long"):
    """A normalized userFills row (HL shape). start_position==0 => Flat->Open."""
    return {
        "dir": "Open Long" if side == "long" else "Open Short",
        "time": time,
        "start_position": start_position,
    }


def _make_app(db, client):
    app = SimpleNamespace(state=SimpleNamespace())
    app.state.db = db
    app.state.mexc = client
    app.state.exchange = client
    app.state.preview_store = None
    app.state.trade_lock = None
    return app


def _install_spy(monkeypatch, result=None):
    if result is None:
        result = {"status": "modify_sl_ok", "verified": True}
    svc = SimpleNamespace(modify_stop_loss=AsyncMock(return_value=result))
    monkeypatch.setattr(
        monitor, "_make_order_service", lambda app, client, settings: svc
    )
    return svc


# Pure-function helpers (mirror tests/test_trade_manager.py).
def _settings(**over):
    base = dict(
        tm_be_trigger_r=1.0,
        tm_be_fee_rt=0.0006,
        tm_time_stop_hours=4.0,
        tm_time_stop_min_r=0.5,
        tm_trail_activation_r=1.0,
        tm_trail_atr_mult=2.0,
        tm_trail_min_step_atr=0.25,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _mgmt(**over):
    base = dict(
        entry=100.0,
        initial_sl=90.0,
        r1=10.0,
        opened_at_ms=0,
        invalidation_price=None,
        armed_rules={},
        be_done=False,
        last_alert_state={},
        high_water=None,
        user_override_hw=None,
    )
    base.update(over)
    return MgmtBaseline(**base)


def _trails(actions):
    return [a for a in actions if isinstance(a, MoveSlToBe) and a.reason == "auto-trail"]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "tml_hardening.db")


# ── FINDING 1: heal a frozen r1==0 once a real SL exists ─────────────────────
@pytest.mark.asyncio
async def test_f1_frozen_zero_r1_is_healed_and_unblocks_auto_be(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    # Position opened WITHOUT a bracket SL → r1 frozen at 0, initial_sl == entry.
    await db.upsert_position_mgmt(
        "BTC_USDT", "long",
        entry_snap=100.0, initial_sl_snap=100.0, r1=0.0,
        opened_at=NOW_MS, invalidation_price=None,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})
    svc = _install_spy(monkeypatch)
    # A real SL now exists (98) and mark is +1.25R off a healed r1=2.
    app = _make_app(db, FakeClient([_pos()], mark=102.5, stops=[{"triggerPrice": 98.0}]))

    await monitor._run_one_cycle(app, NOW_MS)

    # Healed r1 (0 -> |100-98|=2) makes +1.25R >= +1R → auto-BE executes.
    svc.modify_stop_loss.assert_awaited_once()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["r1"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_f1_heal_never_overwrites_a_real_r1(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT", "long",
        entry_snap=100.0, initial_sl_snap=95.0, r1=5.0,
        opened_at=NOW_MS, invalidation_price=None,
    )
    # A different live SL must NOT re-write an already-real r1 (freeze intact).
    await db.heal_r1("BTC_USDT", "long", 98.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["r1"] == pytest.approx(5.0)


# ── FINDING 2: reset ONLY on a STABLE identity change, never on a rolling min ─
async def _seed_f2(db, *, opened_at=NOW_MS):
    await db.upsert_position_mgmt(
        "BTC_USDT", "long",
        entry_snap=100.0, initial_sl_snap=98.0, r1=2.0,
        opened_at=opened_at, invalidation_price=None,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True, "auto_trail": True})
    await db.mark_be_done("BTC_USDT", "long")
    await db.update_high_water("BTC_USDT", "long", 200.0)  # stale extreme


@pytest.mark.asyncio
async def test_f2_hl_reopen_new_epoch_fill_resets(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_f2(db, opened_at=5000)
    _install_spy(monkeypatch)
    # Cycle A: epoch = newest Flat->Open fill @5000. mark 99 < entry so a reset
    # leaves high_water at entry (monotonic MAX can't re-inflate) → unambiguous.
    client = FakeClient([_pos()], mark=99.0, fills=[_open_fill(5000)])
    app = _make_app(db, client)
    await monitor._run_one_cycle(app, NOW_MS)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))["be_done"] == 1

    # Cycle B: genuine reopen → a NEW Flat->Open fill @9000 (old one may linger).
    client._fills = [_open_fill(9000), _open_fill(5000)]
    await monitor._run_one_cycle(app, NOW_MS)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0, "new trade-epoch fill must reset the BE latch"
    assert row["high_water"] == pytest.approx(100.0)
    assert row["armed_rules"] == {"auto_be": True, "auto_trail": True}
    assert row["opened_at"] == 9000, "new trade epoch must reset the time-stop age"


@pytest.mark.asyncio
async def test_f2_hl_epoch_rolled_out_of_window_does_not_reset(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_f2(db)
    _install_spy(monkeypatch)
    # Cycle A: learn epoch @5000.
    client = FakeClient([_pos()], mark=99.0, fills=[_open_fill(5000)])
    app = _make_app(db, client)
    await monitor._run_one_cycle(app, NOW_MS)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))["be_done"] == 1

    # Cycle B: fill-intensive, still-open position — the Flat->Open fill scrolled
    # out; only add-on/partial fills (start_position != 0) remain → signature is
    # None (INCONCLUSIVE) → the latch must NOT be reset mid-trade.
    client._fills = [
        _open_fill(9000, start_position=1.0),
        _open_fill(9100, start_position=2.0),
    ]
    await monitor._run_one_cycle(app, NOW_MS)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1, "an out-of-window epoch is inconclusive → no reset"


@pytest.mark.asyncio
async def test_f2_hl_add_on_fill_does_not_reset(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_f2(db)
    _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=99.0, fills=[_open_fill(5000)])
    app = _make_app(db, client)
    await monitor._run_one_cycle(app, NOW_MS)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))["be_done"] == 1

    # Cycle B: an ADD-ON (start_position != 0) plus the ORIGINAL epoch @5000 still
    # in the window → epoch anchor unchanged → no reset.
    client._fills = [_open_fill(9000, start_position=1.0), _open_fill(5000)]
    await monitor._run_one_cycle(app, NOW_MS)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1, "an add-on must not reset (epoch anchor is stable)"


@pytest.mark.asyncio
async def test_f2_mexc_position_id_change_resets(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_f2(db, opened_at=5000)
    _install_spy(monkeypatch)
    # Non-HL client (no place_stop_order) → signature = snapshot positionId.
    client = FakeClient([_pos(position_id=111)], mark=99.0, is_hl=False)
    app = _make_app(db, client)
    await monitor._run_one_cycle(app, NOW_MS)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))["be_done"] == 1

    # Cycle B: reopen → a NEW positionId (222) → signature change → reset.
    client._positions = [_pos(position_id=222)]
    await monitor._run_one_cycle(app, NOW_MS)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0, "a new MEXC positionId must reset the BE latch"
    assert row["high_water"] == pytest.approx(100.0)
    assert row["opened_at"] == NOW_MS


@pytest.mark.asyncio
async def test_f2_mexc_stable_position_id_does_not_reset(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_f2(db, opened_at=5000)
    _install_spy(monkeypatch)
    client = FakeClient([_pos(position_id=111)], mark=99.0, is_hl=False)
    app = _make_app(db, client)
    await monitor._run_one_cycle(app, NOW_MS)
    # Same positionId across cycles → never a reset.
    await monitor._run_one_cycle(app, NOW_MS)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1
    assert row["opened_at"] == 5000


# ── FINDING 3: minimum trail step (ATR fraction) suppresses micro-churn ───────
def test_f3_trail_below_min_step_is_suppressed():
    # hw 125, atr 5, mult 2 → trail 115. current_sl 114.5 → improvement 0.5.
    # min_step = 0.25*5 = 1.25 → 0.5 < 1.25 → suppressed.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=114.5, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_f3_trail_at_or_above_min_step_still_fires():
    # current_sl 113.0 → improvement 2.0 >= min_step 1.25 → fires.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=113.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    trails = _trails(actions)
    assert len(trails) == 1
    assert math.isclose(trails[0].new_sl, 115.0, rel_tol=1e-12)


def test_f3_min_step_zero_is_old_any_improvement_behavior():
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=114.5, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(tm_trail_min_step_atr=0.0), atr=5.0,
    )
    assert len(_trails(actions)) == 1


def test_f3_min_step_short_side():
    # short: hw 75, atr 5, mult 2 → trail 85. current_sl 85.5 → improvement 0.5
    # < min_step 1.25 → suppressed.
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 armed_rules={"auto_trail": True}, high_water=75.0)
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=85.5, mark=75.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


# ── FINDING 4: honor a manual SL loosening (user_override_hw) ─────────────────
def test_f4_trail_held_after_manual_loosening_until_new_high():
    # User loosened the stop to 100. Without the override the trail (115) would
    # immediately restore. With user_override_hw == current high_water (125) the
    # trail stays quiet until the high-water climbs ABOVE 125.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0,
                 user_override_hw=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=100.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_f4_trail_resumes_once_high_water_exceeds_override():
    # New high 130 > override 125 → trail resumes: 130 - 2*5 = 120 > 100.
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=130.0,
                 user_override_hw=125.0)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=100.0, mark=126.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    trails = _trails(actions)
    assert len(trails) == 1
    assert math.isclose(trails[0].new_sl, 120.0, rel_tol=1e-12)


def test_f4_short_override_holds_until_new_low():
    # short: override low 75; high_water still 75 → held.
    mgmt = _mgmt(entry=100.0, initial_sl=110.0, r1=10.0,
                 armed_rules={"auto_trail": True}, high_water=75.0,
                 user_override_hw=75.0)
    actions = evaluate_rules(
        side="short", entry=100.0, current_sl=100.0, mark=80.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert not _trails(actions)


def test_f4_no_override_leaves_trail_unchanged():
    mgmt = _mgmt(armed_rules={"auto_trail": True}, high_water=125.0,
                 user_override_hw=None)
    actions = evaluate_rules(
        side="long", entry=100.0, current_sl=100.0, mark=120.0,
        mgmt=mgmt, now_ms=0, settings=_settings(), atr=5.0,
    )
    assert len(_trails(actions)) == 1


@pytest.mark.asyncio
async def test_f4_manual_modify_records_user_override_hw(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT", "long",
        entry_snap=100.0, initial_sl_snap=98.0, r1=2.0,
        opened_at=NOW_MS, invalidation_price=None,
    )
    await db.update_high_water("BTC_USDT", "long", 130.0)
    await db.set_user_override_hw("BTC_USDT", "long")
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["user_override_hw"] == pytest.approx(130.0)


# ── FINDING 5: opened_at journal fallback picks the OLDEST matching row ───────
@pytest.mark.asyncio
async def test_f5_opened_at_journal_fallback_uses_oldest_match():
    # No user_fills (MEXC-like) → journal fallback. recent_journal is newest-first.
    class _DB:
        async def recent_journal(self, limit=50):
            return [
                {"symbol": "BTC_USDT", "direction": "long",
                 "created_at": "2026-07-20T12:00:00+00:00"},   # newest match
                {"symbol": "ETH_USDT", "direction": "long",
                 "created_at": "2026-07-20T11:00:00+00:00"},   # non-match
                {"symbol": "BTC_USDT", "direction": "long",
                 "created_at": "2026-07-20T09:00:00+00:00"},   # OLDEST match
            ]

    client = SimpleNamespace()  # no user_fills attr → HL branch skipped
    got = await monitor._best_effort_opened_at(client, _DB(), "BTC_USDT", "long", NOW_MS)
    expected = monitor._iso_to_ms("2026-07-20T09:00:00+00:00")
    assert got == expected


@pytest.mark.asyncio
async def test_opened_at_skips_platform_invalid_journal_date_and_keeps_searching():
    class _DB:
        async def recent_journal(self, limit=50):
            return [
                {
                    "symbol": "BTC_USDT",
                    "direction": "long",
                    # A naive boundary year reaches Windows' local-time
                    # conversion and raises OSError unless _iso_to_ms contains it.
                    "created_at": "0001-01-01T00:00:00",
                },
                {
                    "symbol": "BTC_USDT",
                    "direction": "long",
                    "created_at": "2026-07-20T09:00:00+00:00",
                },
            ]

    got = await monitor._best_effort_opened_at(
        SimpleNamespace(), _DB(), "BTC_USDT", "long", NOW_MS
    )

    assert got == monitor._iso_to_ms("2026-07-20T09:00:00+00:00")
