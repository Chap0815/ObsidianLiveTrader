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
  -- Lern-Loop fix: 'market'|'limit'|NULL. Drives the resolver's shadow-fill
  -- model (market = fills at t0/index 0; limit = fills only when a candle
  -- straddles entry_price). Added here for fresh DBs; on EXISTING DBs it is
  -- backfilled by the idempotent ALTER in Database.init(). NULL (legacy rows /
  -- omitted) keeps the conservative LIMIT modeling -- no retroactive bias.
  order_type    TEXT,
  -- shadow-outcome resolver fields --
  status        TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING|WIN|LOSS|EXPIRED|SKIPPED|NO_FILL
  resolved_at   TEXT,                      -- UTC ISO when status left PENDING
  resolved_price REAL,                     -- tp1 or sl level that triggered (for WIN/LOSS)
  realized_r    REAL,                      -- GROSS +reward/risk on WIN, -1.0 on LOSS, NULL otherwise
  realized_r_net REAL,                     -- realized_r minus round-trip costs (F2-07); NULL when gross is
  ambiguous     INTEGER NOT NULL DEFAULT 0,-- 1 = tp1 & sl inside the same candle
  last_checked_at TEXT,                    -- UTC ISO of last resolver pass (debug/backoff)
  proposal_id   INTEGER,                   -- FK-ish link to proposals.id (best-effort, nullable)
  -- Task 20 attribution/versioning (F2-04/K2-04, F2-08, F2-12). Added here for
  -- fresh DBs; on EXISTING DBs these are backfilled by the idempotent ALTER
  -- migration in Database.init() (CREATE TABLE IF NOT EXISTS never adds columns).
  setup_type    TEXT,                      -- chart_pattern[/time_horizon] attribution key
  context_hash  TEXT,                      -- stable hash of analysis context (dedupe key)
  prompt_version TEXT,                     -- hash of build_system_prompt() (regime attribution)
  -- Block 2/TP2 Task P1: compact regime tag (btc trend bucket x vol bucket),
  -- e.g. "btcUp/volNormal", or "unknown" when a signal was missing. Added here
  -- for fresh DBs; on EXISTING DBs it is backfilled by the idempotent ALTER
  -- migration in Database.init() (CREATE TABLE IF NOT EXISTS never adds
  -- columns). Advisory-only label, never used in a gate/sizing/decision.
  regime        TEXT
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

-- Trade-Management-Layer (Task 4, 2026-07-19 design spec §5): durable baseline
-- per open position (entry/initial SL/1R/opened_at/thesis-invalidation) plus
-- arming + debounce state. A dedicated table (NOT proposals/journal_entries)
-- because it must survive /api/history/clear and /api/journal/clear -- those
-- only DELETE FROM proposals/orders/journal_entries (see Database.clear_history
-- / clear_journal), never touching this table.
CREATE TABLE IF NOT EXISTS position_management (
  id                 INTEGER PRIMARY KEY,
  symbol             TEXT    NOT NULL,
  side               TEXT    NOT NULL,          -- 'long'|'short'
  entry_snap         REAL    NOT NULL,
  initial_sl_snap    REAL    NOT NULL,
  r1                 REAL    NOT NULL,           -- |entry_snap - initial_sl_snap|, fixed at arm/first-sight
  opened_at          INTEGER,                    -- ms epoch, best-effort
  invalidation_price REAL,                       -- from latest proposal for symbol, nullable
  armed_rules        TEXT    NOT NULL DEFAULT '{}',  -- JSON: {"auto_be": true, ...}
  be_done            INTEGER NOT NULL DEFAULT 0,
  last_alert_state   TEXT    NOT NULL DEFAULT '{}',  -- JSON: debounce state per alert kind
  status             TEXT    NOT NULL DEFAULT 'OPEN', -- OPEN | CLOSED
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL,
  high_water         REAL,                           -- TML v2 (Task V3): monotonic
                                                       -- Chandelier high-water (long:
                                                       -- running max mark; short: running
                                                       -- min mark). Moves ONLY via
                                                       -- update_high_water(), never via
                                                       -- the upsert FROZEN path.
  open_sig           INTEGER,                        -- F2: STABLE reopen-signature
                                                       -- (HL: newest Flat->Open fill time;
                                                       -- MEXC: snapshot positionId). A
                                                       -- change on a same-entry re-sighting
                                                       -- means a close+reopen → hard
                                                       -- baseline reset. NULL = inconclusive
                                                       -- → never resets.
  user_override_hw   REAL                            -- F4: high-water parked at a manual
                                                       -- SL move; the trail is held until
                                                       -- high_water surpasses it.
);

-- Partial unique index: at most ONE OPEN row per (symbol, side). CLOSED rows
-- are kept as history and are exempt (a symbol/side can have many CLOSED rows
-- over time, but the invariant only needs to hold for the live OPEN record).
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_mgmt_open_unique
  ON position_management(symbol, side)
  WHERE status = 'OPEN';

CREATE INDEX IF NOT EXISTS idx_position_mgmt_status ON position_management(status);
"""


async def init_db(db_path: str) -> None:
    """Create parent dir + tables if missing."""
    from app.db.repo import Database

    db = Database(db_path)
    await db.init()
