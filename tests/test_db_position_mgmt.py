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
