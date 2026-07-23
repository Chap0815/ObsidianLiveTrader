"""position_management repo: upsert/reset, arming, alert-state, close, kill-switch."""

from __future__ import annotations

import pytest

from app.db.repo import Database


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "position_mgmt.db")


def _base_kwargs(**over):
    kw = dict(
        symbol="BTC_USDT",
        side="long",
        entry_snap=100.0,
        initial_sl_snap=98.0,
        r1=2.0,
        opened_at=1_700_000_000_000,
        invalidation_price=97.0,
    )
    kw.update(over)
    return kw


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_same_open_row(db_path):
    db = Database(db_path)
    await db.init()

    await db.upsert_position_mgmt(**_base_kwargs())
    rows = await db.list_open_position_mgmt()
    assert len(rows) == 1
    first = rows[0]
    assert first["symbol"] == "BTC_USDT"
    assert first["side"] == "long"
    assert first["entry_snap"] == 100.0
    assert first["r1"] == 2.0
    assert first["status"] == "OPEN"
    assert first["be_done"] == 0

    # Same entry (within tolerance) -> update the SAME OPEN record, not a new one.
    # The BASELINE stays FROZEN (spec §4): initial_sl_snap/r1 must NOT drift to
    # the re-sighted stop, so "+1R" keeps measuring from the original risk.
    await db.upsert_position_mgmt(**_base_kwargs(entry_snap=100.0005, initial_sl_snap=98.5))
    rows = await db.list_open_position_mgmt()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["initial_sl_snap"] == 98.0  # frozen, NOT the re-sighted 98.5
    assert rows[0]["r1"] == 2.0  # frozen


@pytest.mark.asyncio
async def test_high_water_initialized_on_insert_and_present_on_get(db_path):
    db = Database(db_path)
    await db.init()

    await db.upsert_position_mgmt(**_base_kwargs())
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    # Task V3: initialized to entry_snap on fresh INSERT (documented choice --
    # see upsert_position_mgmt docstring).
    assert row["high_water"] == 100.0


@pytest.mark.asyncio
async def test_non_deviated_re_sighting_does_not_move_high_water(db_path):
    """Frozen-baseline path (TML v2 Task V3): high_water only moves via
    update_high_water, NEVER via the non-deviated upsert refresh path."""
    db = Database(db_path)
    await db.init()

    await db.upsert_position_mgmt(**_base_kwargs())
    await db.update_high_water("BTC_USDT", "long", 105.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["high_water"] == 105.0

    # Non-deviated re-sighting (same upsert path as the frozen-baseline test
    # above) must NOT touch high_water, even though initial_sl_snap is passed.
    await db.upsert_position_mgmt(**_base_kwargs(entry_snap=100.0005, initial_sl_snap=98.5))
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["high_water"] == 105.0


@pytest.mark.asyncio
async def test_update_high_water_monotonic_long_never_decreases(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs())  # entry_snap=100.0

    await db.update_high_water("BTC_USDT", "long", 105.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["high_water"] == 105.0

    # Lower mark -> high_water must NOT drop back down.
    await db.update_high_water("BTC_USDT", "long", 102.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["high_water"] == 105.0

    # New higher mark -> advances.
    await db.update_high_water("BTC_USDT", "long", 110.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["high_water"] == 110.0


@pytest.mark.asyncio
async def test_update_high_water_monotonic_short_never_increases(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs(side="short", entry_snap=100.0, initial_sl_snap=102.0))

    await db.update_high_water("BTC_USDT", "short", 95.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "short")
    assert row["high_water"] == 95.0

    # Higher mark -> high_water (a "low-water" mark for shorts) must NOT rise back up.
    await db.update_high_water("BTC_USDT", "short", 98.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "short")
    assert row["high_water"] == 95.0

    # New lower mark -> advances (drops further).
    await db.update_high_water("BTC_USDT", "short", 90.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "short")
    assert row["high_water"] == 90.0


@pytest.mark.asyncio
async def test_update_high_water_noop_when_no_open_record(db_path):
    db = Database(db_path)
    await db.init()
    # No OPEN row exists yet -> must not raise, must not create a row.
    await db.update_high_water("BTC_USDT", "long", 105.0)
    assert await db.get_open_position_mgmt("BTC_USDT", "long") is None


@pytest.mark.asyncio
async def test_entry_deviation_resets_position_and_be_done(db_path):
    db = Database(db_path)
    await db.init()

    await db.upsert_position_mgmt(**_base_kwargs())
    await db.mark_be_done("BTC_USDT", "long")
    await db.update_high_water("BTC_USDT", "long", 105.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1
    assert row["high_water"] == 105.0

    # Entry moved significantly -> treated as a NEW position: be_done resets.
    # high_water resets too (Task V3: fresh baseline -> fresh high_water).
    await db.upsert_position_mgmt(**_base_kwargs(entry_snap=110.0, initial_sl_snap=108.0))
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0
    assert row["entry_snap"] == 110.0
    assert row["initial_sl_snap"] == 108.0
    assert row["high_water"] == 110.0  # reset to the new entry, NOT the stale 105.0


@pytest.mark.asyncio
async def test_set_armed_rules_mark_be_done_set_alert_state_persist_idempotent(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs())

    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["armed_rules"] == {"auto_be": True}

    # Idempotent: calling again with the same payload is a no-op re-write.
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["armed_rules"] == {"auto_be": True}

    await db.mark_be_done("BTC_USDT", "long")
    await db.mark_be_done("BTC_USDT", "long")  # idempotent
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1

    await db.set_alert_state("BTC_USDT", "long", {"thesis": "fired"})
    await db.set_alert_state("BTC_USDT", "long", {"thesis": "fired"})  # idempotent
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["last_alert_state"] == {"thesis": "fired"}


@pytest.mark.asyncio
async def test_close_position_mgmt_sets_status_closed(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs())

    await db.close_position_mgmt("BTC_USDT", "long")
    assert await db.get_open_position_mgmt("BTC_USDT", "long") is None
    assert await db.list_open_position_mgmt() == []

    # Re-upsert after close starts a fresh OPEN baseline (new position).
    await db.upsert_position_mgmt(**_base_kwargs(entry_snap=101.0, initial_sl_snap=99.0))
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row is not None
    assert row["status"] == "OPEN"
    assert row["be_done"] == 0


@pytest.mark.asyncio
async def test_disarm_all_empties_armed_rules_and_returns_count(db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs(symbol="BTC_USDT", side="long"))
    await db.upsert_position_mgmt(**_base_kwargs(symbol="ETH_USDT", side="short"))
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})
    await db.set_armed_rules("ETH_USDT", "short", {"auto_be": True})

    count = await db.disarm_all()
    assert count == 2

    for sym, side in (("BTC_USDT", "long"), ("ETH_USDT", "short")):
        row = await db.get_open_position_mgmt(sym, side)
        assert row["armed_rules"] == {}


# ── 2026-07-20: shared-connection atomic-upsert fix (concurrent-write safety) ──


@pytest.mark.asyncio
async def test_partial_index_on_conflict_upsert_supported(tmp_path):
    """Pin the SQLite features the position_mgmt upsert now relies on: a partial-
    index ON CONFLICT target (>=3.24) + RETURNING (>=3.35). If a runtime ever ships
    an older sqlite, THIS fails loudly instead of the method raising in production.
    Also proves DO UPDATE refreshes in place (no duplicate, no IntegrityError)."""
    import aiosqlite

    conn = await aiosqlite.connect(str(tmp_path / "u.db"))
    try:
        await conn.execute("CREATE TABLE pm(id INTEGER PRIMARY KEY, sym TEXT, st TEXT, v INTEGER)")
        await conn.execute("CREATE UNIQUE INDEX u ON pm(sym) WHERE st='OPEN'")
        await conn.execute("INSERT INTO pm(sym, st, v) VALUES ('BTC','OPEN',1)")
        await conn.commit()
        cur = await conn.execute(
            "INSERT INTO pm(sym, st, v) VALUES ('BTC','OPEN',2) "
            "ON CONFLICT(sym) WHERE st='OPEN' DO UPDATE SET v=excluded.v RETURNING id, v"
        )
        row = await cur.fetchone()
        await conn.commit()
        cur2 = await conn.execute("SELECT COUNT(*), MAX(v) FROM pm")
        cnt, maxv = await cur2.fetchone()
    finally:
        await conn.close()
    assert row is not None and row[1] == 2  # RETURNING gave the updated row
    assert cnt == 1 and maxv == 2  # updated IN PLACE — no duplicate, no raise


@pytest.mark.asyncio
async def test_concurrent_upsert_same_position_converges_to_one_row(db_path):
    """End-to-end invariant: two connections issuing upserts for the same
    (symbol, side) OPEN position must converge to exactly ONE row / id — no
    duplicate, no crash — whether they serialize (second sees the row) or race
    the partial-unique index (ON CONFLICT DO UPDATE). Which internal path each
    call takes is timing-dependent and NOT asserted here (that's the honest
    scope); the atomic upsert makes the outcome identical either way."""
    import asyncio

    db1 = Database(db_path)
    await db1.init()
    db2 = Database(db_path)
    ids = await asyncio.gather(
        db1.upsert_position_mgmt(**_base_kwargs()),
        db2.upsert_position_mgmt(**_base_kwargs()),
    )
    rows = await db1.list_open_position_mgmt()
    assert len(rows) == 1  # exactly one OPEN row, no duplicate
    assert ids[0] == ids[1] == rows[0]["id"]  # both returned the one winning id


# ── 2026-07-23: reopen re-arms the advisory alarms (thesis/time_stop) ─────────
# A close+reopen of the SAME (symbol, side) at ~the same entry resets the
# volatile latches (be_done/high_water/user_override_hw) but historically left
# last_alert_state stale -> the one-shot thesis/time_stop gate in the monitor
# (not alert_state.get(...)) stayed tripped from the PRIOR trade and the NEW
# position's advisory alarm never fired (silent alarm suppression). Both reopen
# reset paths must clear last_alert_state to '{}' -- exactly like the existing
# entry-deviation reset -- while the ordinary re-sighting cycle must NOT (that
# would re-arm every tick and spam alerts).


@pytest.mark.asyncio
async def test_reset_baseline_clears_stale_alert_state(db_path):
    """reset_position_mgmt_baseline (same-price reopen the entry-deviation check
    can't catch) must re-arm the advisory alarms: last_alert_state -> {}."""
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs())
    await db.set_alert_state("BTC_USDT", "long", {"thesis": "fired", "time_stop": "fired"})
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["last_alert_state"] == {"thesis": "fired", "time_stop": "fired"}

    await db.reset_position_mgmt_baseline("BTC_USDT", "long")
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    # Re-armed: stale flags gone -> the thesis/time_stop gate can fire again.
    assert row["last_alert_state"] == {}


@pytest.mark.asyncio
async def test_sig_changed_reopen_clears_stale_alert_state(db_path):
    """sig_changed branch (same entry, NEW open_sig -> genuine reopen) must
    re-arm the advisory alarms: last_alert_state -> {}."""
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs(open_sig=1000))
    await db.set_alert_state("BTC_USDT", "long", {"thesis": "fired"})

    # Same entry (within tolerance) but a DIFFERENT stable reopen signature.
    await db.upsert_position_mgmt(**_base_kwargs(entry_snap=100.0005, open_sig=2000))
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["last_alert_state"] == {}  # re-armed
    assert row["be_done"] == 0  # sanity: this really took the reopen path


@pytest.mark.asyncio
async def test_non_reopen_cycle_preserves_alert_state(db_path):
    """ANTI-SPAM: an ordinary re-sighting (same entry, SAME open_sig, no
    deviation) is NOT a reopen -> last_alert_state must stay UNCHANGED, else the
    one-shot alarm would re-arm every monitor tick and spam alerts."""
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(**_base_kwargs(open_sig=1000))
    await db.set_alert_state("BTC_USDT", "long", {"thesis": "fired"})

    # Same entry (within tolerance), SAME signature -> plain re-sighting.
    await db.upsert_position_mgmt(**_base_kwargs(entry_snap=100.0005, open_sig=1000))
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["last_alert_state"] == {"thesis": "fired"}  # preserved, NOT re-armed
