"""Task 20: journal attribution/dedupe/versioning + per-group CI/ambiguous + by_setup.

Covers the idempotent migration on a pre-populated old DB (missing the new
columns), context_hash dedupe within a window, and the stats extensions
(win_rate_ci95 + ambiguous + clean_win_rate per group, plus a by_setup group).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from app.db.repo import Database
from app.journal.stats import build_stats_response


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
        scanner_summary=None,
        last_price_t0=100.5,
    )
    kw.update(over)
    return kw


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# ── Migration on a pre-populated old DB ─────────────────────────────────

# Old journal_entries schema WITHOUT realized_r_net and WITHOUT the Task-20
# attribution columns (setup_type/context_hash/prompt_version). init() must add
# them idempotently via ALTER TABLE ADD COLUMN without touching the old row.
_OLD_SCHEMA = """
CREATE TABLE journal_entries (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL, symbol TEXT NOT NULL, tf TEXT NOT NULL, htf TEXT NOT NULL,
  action TEXT NOT NULL, direction TEXT, setup_confidence TEXT NOT NULL,
  entry_price REAL, stop_loss REAL, tp1 REAL, rrr REAL, provider TEXT, model TEXT,
  scanner_summary TEXT, last_price_t0 REAL,
  status TEXT NOT NULL DEFAULT 'PENDING', resolved_at TEXT, resolved_price REAL,
  realized_r REAL, ambiguous INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT, proposal_id INTEGER
);
"""


@pytest.mark.asyncio
async def test_migration_adds_columns_to_existing_db(db_path):
    # 1) pre-populate an OLD DB missing the new columns
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_OLD_SCHEMA)
        await conn.execute(
            "INSERT INTO journal_entries (created_at, symbol, tf, htf, action, "
            "direction, setup_confidence, status) VALUES "
            "(?, 'BTC_USDT', '15m', '1H', 'BUY', 'long', 'high', 'PENDING')",
            (_iso(datetime.now(timezone.utc)),),
        )
        await conn.commit()

    # 2) init() must add the columns idempotently (no drop/rewrite)
    db = Database(db_path)
    await db.init()
    await db.init()  # second pass must be a no-op, not an error

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("PRAGMA table_info(journal_entries)")
        cols = {r[1] for r in await cur.fetchall()}
    for c in ("realized_r_net", "setup_type", "context_hash", "prompt_version"):
        assert c in cols, f"migration missing column {c}"

    # 3) old row survived and has NULL for the new columns
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT setup_type, context_hash, prompt_version FROM journal_entries"
        )
        row = await cur.fetchone()
    assert row == (None, None, None)

    # 4) a fresh insert that populates the new columns works
    jid = await db.insert_journal_entry(
        **_base_kwargs(setup_type="breakout/swing", context_hash="h1", prompt_version="v1")
    )
    assert jid >= 1
    rows = await db.recent_journal()
    assert len(rows) == 2  # old NULL row + new row, nothing wiped


# ── F2-08 dedupe by context_hash within a window ─────────────────────────

@pytest.mark.asyncio
async def test_journal_insert_dedupes_by_context_hash(db_path):
    db = Database(db_path)
    await db.init()

    id1 = await db.insert_journal_entry(**_base_kwargs(context_hash="ctx-A", rrr=2.0))
    # same hash within the window → UPDATE in place, same id, no new row
    id2 = await db.insert_journal_entry(**_base_kwargs(context_hash="ctx-A", rrr=3.0))
    assert id2 == id1
    rows = await db.recent_journal()
    assert len(rows) == 1
    assert rows[0]["rrr"] == 3.0  # latest analysis overwrote the row

    # a different hash → genuinely new row
    id3 = await db.insert_journal_entry(**_base_kwargs(context_hash="ctx-B"))
    assert id3 != id1
    assert len(await db.recent_journal()) == 2

    # same hash but OUTSIDE the window (old created_at) → new row, not a dedupe
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    await db.insert_journal_entry(**_base_kwargs(context_hash="ctx-C", created_at=old))
    id5 = await db.insert_journal_entry(**_base_kwargs(context_hash="ctx-C"))
    rows = await db.recent_journal()
    ctxc = [r for r in rows if r["id"] in (id5,)]
    assert len(ctxc) == 1
    assert len([r for r in await db.recent_journal()]) == 4  # A, B, C-old, C-new


# ── F2-09/F2-10 per-group CI + ambiguous + clean win rate ────────────────

@pytest.mark.asyncio
async def test_stats_groups_have_ci_and_ambiguous(db_path):
    db = Database(db_path)
    await db.init()
    w = await db.insert_journal_entry(**_base_kwargs(setup_confidence="high", context_hash="a"))
    l_clean = await db.insert_journal_entry(
        **_base_kwargs(setup_confidence="high", action="SELL", direction="short", context_hash="b")
    )
    l_amb = await db.insert_journal_entry(
        **_base_kwargs(setup_confidence="high", action="SELL", direction="short", context_hash="c")
    )
    await db.update_journal_outcome(w, status="WIN", realized_r=2.0)
    await db.update_journal_outcome(l_clean, status="LOSS", realized_r=-1.0, ambiguous=0)
    await db.update_journal_outcome(l_amb, status="LOSS", realized_r=-1.0, ambiguous=1)

    raw = await db.journal_stats()
    r = build_stats_response(raw, min_sample=20)
    blk = r["by_confidence"]["high"]
    assert blk["wins"] == 1 and blk["losses"] == 2
    # F2-09: Wilson CI present as a [lo, hi] list
    assert isinstance(blk["win_rate_ci95"], list) and len(blk["win_rate_ci95"]) == 2
    # F2-10: one ambiguous resolved row in the group
    assert blk["ambiguous"] == 1
    # clean win rate excludes the ambiguous loss: 1 win / (1 win + 1 clean loss)
    assert blk["clean_win_rate"] == 0.5


# ── Block 2/TP2 Task P1: regime column persists + by_regime breakdown ───

@pytest.mark.asyncio
async def test_regime_column_persists(db_path):
    db = Database(db_path)
    await db.init()
    jid = await db.insert_journal_entry(
        **_base_kwargs(regime="btcUp/volNormal", context_hash="reg-a")
    )
    assert jid >= 1
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT regime FROM journal_entries WHERE id = ?", (jid,)
        )
        row = await cur.fetchone()
    assert row == ("btcUp/volNormal",)

    # a row with no regime (older caller / fail-safe path) stays NULL, not "".
    jid2 = await db.insert_journal_entry(**_base_kwargs(context_hash="reg-b"))
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT regime FROM journal_entries WHERE id = ?", (jid2,)
        )
        row2 = await cur.fetchone()
    assert row2 == (None,)


@pytest.mark.asyncio
async def test_by_regime_breakdown(db_path):
    db = Database(db_path)
    await db.init()
    w = await db.insert_journal_entry(
        **_base_kwargs(regime="btcUp/volNormal", context_hash="a")
    )
    l = await db.insert_journal_entry(
        **_base_kwargs(
            regime="btcDown/volHigh", action="SELL", direction="short", context_hash="b"
        )
    )
    # a row with NULL regime must not crash the group / appear as a key
    await db.insert_journal_entry(**_base_kwargs(regime=None, context_hash="c"))
    await db.update_journal_outcome(w, status="WIN", realized_r=2.0)
    await db.update_journal_outcome(l, status="LOSS", realized_r=-1.0)

    raw = await db.journal_stats()
    r = build_stats_response(raw, min_sample=20)
    by_regime = r["by_regime"]
    assert by_regime["btcUp/volNormal"]["wins"] == 1
    assert by_regime["btcDown/volHigh"]["losses"] == 1
    assert None not in by_regime and "None" not in by_regime


# ── by_setup breakdown ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_by_setup_breakdown(db_path):
    db = Database(db_path)
    await db.init()
    w = await db.insert_journal_entry(**_base_kwargs(setup_type="breakout", context_hash="a"))
    l = await db.insert_journal_entry(
        **_base_kwargs(setup_type="range", action="SELL", direction="short", context_hash="b")
    )
    # a row with NULL setup_type must not crash the group / appear as a key
    await db.insert_journal_entry(**_base_kwargs(setup_type=None, context_hash="c"))
    await db.update_journal_outcome(w, status="WIN", realized_r=2.0)
    await db.update_journal_outcome(l, status="LOSS", realized_r=-1.0)

    raw = await db.journal_stats()
    r = build_stats_response(raw, min_sample=20)
    by_setup = r["by_setup"]
    assert by_setup["breakout"]["wins"] == 1
    assert by_setup["range"]["losses"] == 1
    assert None not in by_setup and "None" not in by_setup
