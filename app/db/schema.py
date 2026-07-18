"""SQLite schema for order previews + orders audit (Task 7 early / Task 8)."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  symbol TEXT NOT NULL,
  context_hash TEXT,
  proposal_json TEXT NOT NULL,
  annotations_json TEXT
);

CREATE TABLE IF NOT EXISTS order_previews (
  id INTEGER PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT,
  request_json TEXT,
  response_json TEXT,
  status TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_previews_expires ON order_previews(expires_at);

-- Journal + feedback-loop: the KI's SHADOW book. Advisory/measurement only —
-- it never touches the order/gate/confirm path. A dedicated table (NOT an
-- extension of proposals) because proposals is wiped by /api/history/clear
-- while the journal is the measurement dataset and must survive it, and stats
-- need typed/indexed columns to aggregate on (not a JSON blob).
CREATE TABLE IF NOT EXISTS journal_entries (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT    NOT NULL,          -- UTC ISO, proposal timestamp (t0)
  symbol        TEXT    NOT NULL,
  tf            TEXT    NOT NULL,
  htf           TEXT    NOT NULL,
  action        TEXT    NOT NULL,          -- STRONG_BUY|BUY|STAY_OUT|SELL|STRONG_SHORT
  direction     TEXT,                      -- 'long'|'short'|NULL (NULL for STAY_OUT)
  setup_confidence TEXT NOT NULL,          -- low|medium|high
  entry_price   REAL,                      -- NULL for STAY_OUT
  stop_loss     REAL,
  tp1           REAL,
  rrr           REAL,                      -- planned rrr from the proposal
  provider      TEXT,                      -- s.llm_provider at analyze time
  model         TEXT,                      -- resolved model string
  scanner_summary TEXT,                    -- compact "bias/setup/score" string or NULL
  last_price_t0 REAL,                      -- market last_price when logged (context)
  -- shadow-outcome resolver fields --
  status        TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING|WIN|LOSS|EXPIRED|SKIPPED|NO_FILL
  resolved_at   TEXT,                      -- UTC ISO when status left PENDING
  resolved_price REAL,                     -- tp1 or sl level that triggered (for WIN/LOSS)
  realized_r    REAL,                      -- GROSS +reward/risk on WIN, -1.0 on LOSS, NULL otherwise
  realized_r_net REAL,                     -- realized_r minus round-trip costs (F2-07); NULL when gross is
  ambiguous     INTEGER NOT NULL DEFAULT 0,-- 1 = tp1 & sl inside the same candle
  last_checked_at TEXT,                    -- UTC ISO of last resolver pass (debug/backoff)
  proposal_id   INTEGER                    -- FK-ish link to proposals.id (best-effort, nullable)
);

CREATE INDEX IF NOT EXISTS idx_journal_created ON journal_entries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_journal_status  ON journal_entries(status);

-- Additive indexes for the journal_stats() GROUP BY columns (action,
-- setup_confidence, provider) so the stats endpoint's per-group aggregation
-- (see Database.journal_stats._groups) stays cheap as journal_entries grows
-- (no cap/retention on that table -- see the comment at insert_journal_entry).
CREATE INDEX IF NOT EXISTS idx_journal_action     ON journal_entries(action);
CREATE INDEX IF NOT EXISTS idx_journal_confidence  ON journal_entries(setup_confidence);
CREATE INDEX IF NOT EXISTS idx_journal_provider    ON journal_entries(provider);
"""


async def init_db(db_path: str) -> None:
    """Create parent dir + tables if missing."""
    from app.db.repo import Database

    db = Database(db_path)
    await db.init()
