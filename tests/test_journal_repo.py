"""Journal repo: schema roundtrip, PENDING→terminal transitions, stats, idempotency."""

from __future__ import annotations

import pytest

from app.db.repo import Database


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "journal.db")


def _base_kwargs(**over):
    kw = dict(
        symbol="BTC_USDT",
        tf="15m",
        htf="1H",
        action="BUY",
        direction="long",
        setup_confidence="high",
        entry_price=100.0,
        stop_loss=99.0,
        tp1=102.0,
        rrr=2.0,
        provider="claude",
        model="claude-x",
        scanner_summary="long/breakout/0.8",
        last_price_t0=100.5,
    )
    kw.update(over)
    return kw


@pytest.mark.asyncio
async def test_insert_and_readback_pending(db_path):
    db = Database(db_path)
    await db.init()
    jid = await db.insert_journal_entry(**_base_kwargs())
    assert jid >= 1

    rows = await db.recent_journal(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "BTC_USDT"
    assert r["action"] == "BUY"
    assert r["direction"] == "long"
    assert r["status"] == "PENDING"
    assert r["entry_price"] == 100.0
    assert r["tp1"] == 102.0
    assert r["ambiguous"] == 0
    assert r["scanner_summary"] == "long/breakout/0.8"


@pytest.mark.asyncio
async def test_stay_out_logged_skipped(db_path):
    db = Database(db_path)
    await db.init()
    await db.insert_journal_entry(
        **_base_kwargs(
            action="STAY_OUT",
            direction=None,
            entry_price=None,
            stop_loss=None,
            tp1=None,
            rrr=None,
            status="SKIPPED",
        )
    )
    stats = await db.journal_stats()
    assert stats["total"] == 1
    assert stats["stay_out"] == 1
    assert stats["skipped"] == 1
    assert stats["pending"] == 0
    # SKIPPED never appears in the PENDING work queue
    assert await db.pending_journal_entries() == []


@pytest.mark.asyncio
async def test_missing_levels_skipped(db_path):
    db = Database(db_path)
    await db.init()
    await db.insert_journal_entry(
        **_base_kwargs(entry_price=None, stop_loss=None, tp1=None, status="SKIPPED")
    )
    stats = await db.journal_stats()
    assert stats["skipped"] == 1
    assert stats["stay_out"] == 0


@pytest.mark.asyncio
async def test_transition_pending_to_win_and_idempotent(db_path):
    db = Database(db_path)
    await db.init()
    jid = await db.insert_journal_entry(**_base_kwargs())
    pend = await db.pending_journal_entries()
    assert len(pend) == 1 and pend[0]["id"] == jid

    await db.update_journal_outcome(
        jid, status="WIN", resolved_price=102.0, realized_r=2.0, ambiguous=0
    )
    rows = await db.recent_journal()
    assert rows[0]["status"] == "WIN"
    assert rows[0]["realized_r"] == 2.0
    assert rows[0]["resolved_price"] == 102.0
    assert rows[0]["resolved_at"] is not None
    # no longer pending
    assert await db.pending_journal_entries() == []

    # Idempotency: a second update must NOT overwrite a terminal row
    await db.update_journal_outcome(jid, status="LOSS", realized_r=-1.0)
    rows = await db.recent_journal()
    assert rows[0]["status"] == "WIN"
    assert rows[0]["realized_r"] == 2.0


@pytest.mark.asyncio
async def test_touch_journal_checked_keeps_pending(db_path):
    db = Database(db_path)
    await db.init()
    jid = await db.insert_journal_entry(**_base_kwargs())
    await db.touch_journal_checked(jid)
    pend = await db.pending_journal_entries()
    assert len(pend) == 1
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_stats_counts_and_groups(db_path):
    db = Database(db_path)
    await db.init()
    # 2 wins (high), 1 loss (low), 1 stay_out, 1 pending
    w1 = await db.insert_journal_entry(**_base_kwargs(setup_confidence="high", action="BUY"))
    w2 = await db.insert_journal_entry(**_base_kwargs(setup_confidence="high", action="STRONG_BUY"))
    l1 = await db.insert_journal_entry(**_base_kwargs(setup_confidence="low", action="SELL", direction="short"))
    await db.insert_journal_entry(**_base_kwargs())  # stays pending
    await db.insert_journal_entry(
        **_base_kwargs(action="STAY_OUT", direction=None, entry_price=None,
                       stop_loss=None, tp1=None, rrr=None, status="SKIPPED")
    )
    await db.update_journal_outcome(w1, status="WIN", realized_r=2.0)
    await db.update_journal_outcome(w2, status="WIN", realized_r=1.5)
    await db.update_journal_outcome(l1, status="LOSS", realized_r=-1.0)

    stats = await db.journal_stats()
    assert stats["total"] == 5
    assert stats["stay_out"] == 1
    assert stats["skipped"] == 1
    assert stats["pending"] == 1
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["overall_sum_r"] == pytest.approx(2.0 + 1.5 - 1.0)

    bc = stats["by_confidence"]
    assert bc["high"]["wins"] == 2 and bc["high"]["losses"] == 0
    assert bc["low"]["losses"] == 1
    ba = stats["by_action"]
    assert ba["BUY"]["wins"] == 1
    assert ba["STRONG_BUY"]["wins"] == 1
    assert ba["SELL"]["losses"] == 1


@pytest.mark.asyncio
async def test_journal_stats_single_status_groupby(db_path):
    """Q-04: the collapsed GROUP BY status must be bit-identical to the old
    seven-COUNT implementation across a mix of every status + STAY_OUT."""
    db = Database(db_path)
    await db.init()
    w = await db.insert_journal_entry(**_base_kwargs(setup_confidence="high"))
    l = await db.insert_journal_entry(
        **_base_kwargs(setup_confidence="low", direction="short", action="SELL")
    )
    e = await db.insert_journal_entry(**_base_kwargs(setup_confidence="mid"))
    await db.insert_journal_entry(**_base_kwargs())  # stays PENDING
    await db.insert_journal_entry(
        **_base_kwargs(action="STAY_OUT", direction=None, entry_price=None,
                       stop_loss=None, tp1=None, rrr=None, status="SKIPPED")
    )
    await db.update_journal_outcome(w, status="WIN", realized_r=2.0)
    await db.update_journal_outcome(l, status="LOSS", realized_r=-1.0)
    await db.update_journal_outcome(e, status="EXPIRED")

    stats = await db.journal_stats()
    # one row in each of the five statuses; STAY_OUT overlaps the SKIPPED row
    assert stats["total"] == 5
    assert stats["pending"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["expired"] == 1
    assert stats["skipped"] == 1
    assert stats["stay_out"] == 1  # via action, independent of status buckets
    # total is exactly the sum of the mutually-exclusive status groups
    assert stats["total"] == (
        stats["pending"] + stats["wins"] + stats["losses"]
        + stats["expired"] + stats["skipped"]
    )
    assert stats["overall_sum_r"] == pytest.approx(2.0 - 1.0)
    assert stats["by_confidence"]["high"]["wins"] == 1
    assert stats["by_confidence"]["low"]["losses"] == 1


@pytest.mark.asyncio
async def test_db_shared_connection_reused(db_path):
    """Q-03: after open() the same aiosqlite connection is reused for every
    operation (no per-call reconnect), and close() restores lazy fallback."""
    db = Database(db_path)
    await db.init()
    await db.open()

    calls = {"n": 0}
    orig_connect = db._connect

    def counting_connect():
        calls["n"] += 1
        return orig_connect()

    db._connect = counting_connect
    shared = db._shared
    assert shared is not None

    await db.insert_journal_entry(**_base_kwargs())
    await db.recent_journal()
    await db.journal_stats()
    await db.pending_journal_entries()
    # No fresh connection opened while the shared one is live.
    assert calls["n"] == 0
    assert db._shared is shared

    await db.close()
    assert db._shared is None
    # Lazy fallback: methods still work by opening a per-call connection.
    rows = await db.recent_journal()
    assert len(rows) == 1
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_init_idempotent_and_indexes(db_path):
    db = Database(db_path)
    await db.init()
    await db.insert_journal_entry(**_base_kwargs())
    # re-init on an existing DB must not wipe or error
    await db.init()
    rows = await db.recent_journal()
    assert len(rows) == 1

    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_journal%'"
        )
        names = {r[0] for r in await cur.fetchall()}
    assert "idx_journal_created" in names
    assert "idx_journal_status" in names


@pytest.mark.asyncio
async def test_clear_journal_only(db_path):
    db = Database(db_path)
    await db.init()
    await db.insert_journal_entry(**_base_kwargs())
    await db.insert_proposal(symbol="BTC_USDT", proposal_json={"action": "BUY"})
    deleted = await db.clear_journal()
    assert deleted == 1
    assert await db.recent_journal() == []
    # proposals untouched
    props = await db.recent_proposals()
    assert len(props) == 1
