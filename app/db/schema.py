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
"""


async def init_db(db_path: str) -> None:
    """Create parent dir + tables if missing."""
    from app.db.repo import Database

    db = Database(db_path)
    await db.init()
