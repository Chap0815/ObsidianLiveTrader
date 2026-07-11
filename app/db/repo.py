"""Async SQLite repository for proposals, previews, and orders (Task 7/8)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.db.schema import SCHEMA_SQL


def _resolve_path(db_path: str) -> Path:
    path = Path(db_path)
    if not path.is_absolute():
        root = Path(__file__).resolve().parent.parent.parent
        path = root / path
    return path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, separators=(",", ":"), default=str)


def _loads(raw: str | None) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


class Database:
    def __init__(self, db_path: str):
        self.path = _resolve_path(db_path)

    def _connect(self):
        # timeout is sqlite3's busy handler window (seconds): wait for a
        # concurrent writer instead of raising "database is locked" at once.
        return aiosqlite.connect(str(self.path), timeout=30.0)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=30000;")
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()

    async def insert_proposal(
        self,
        *,
        symbol: str,
        proposal_json: Any,
        annotations_json: Any | None = None,
        context_hash: str | None = None,
    ) -> int:
        async with self._connect() as conn:
            cur = await conn.execute(
                """
                INSERT INTO proposals
                  (created_at, symbol, context_hash, proposal_json, annotations_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    symbol,
                    context_hash,
                    _dumps(proposal_json) or "{}",
                    _dumps(annotations_json),
                ),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def insert_preview(
        self,
        *,
        token_hash: str,
        payload_json: dict[str, Any] | str,
        expires_at: str,
    ) -> None:
        payload = _dumps(payload_json) or "{}"
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO order_previews
                  (token_hash, payload_json, expires_at, used_at)
                VALUES (?, ?, ?, NULL)
                """,
                (token_hash, payload, expires_at),
            )
            await conn.commit()

    async def mark_preview_used(self, token_hash: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                """
                UPDATE order_previews
                SET used_at = ?
                WHERE token_hash = ?
                """,
                (_utc_now_iso(), token_hash),
            )
            await conn.commit()

    async def insert_order(
        self,
        *,
        symbol: str,
        side: str | None,
        request_json: Any,
        response_json: Any,
        status: str,
        error: str | None,
    ) -> int:
        async with self._connect() as conn:
            cur = await conn.execute(
                """
                INSERT INTO orders
                  (created_at, symbol, side, request_json, response_json, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    symbol,
                    side,
                    _dumps(request_json),
                    _dumps(response_json),
                    status,
                    error,
                ),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def recent_proposals(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, created_at, symbol, context_hash, proposal_json, annotations_json
                FROM proposals
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["proposal"] = _loads(d.pop("proposal_json"))
                d["annotations"] = _loads(d.pop("annotations_json"))
                out.append(d)
            return out

    async def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, created_at, symbol, side, request_json, response_json, status, error
                FROM orders
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["request"] = _loads(d.pop("request_json"))
                d["response"] = _loads(d.pop("response_json"))
                out.append(d)
            return out

    async def history(self, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
        """Last N proposals + last N orders for the history panel."""
        return {
            "proposals": await self.recent_proposals(limit),
            "orders": await self.recent_orders(limit),
        }

    async def clear_history(self) -> dict[str, int]:
        """Delete all rows from the audit history tables (proposals + orders).

        Does NOT touch order_previews (short-lived one-time confirm tokens,
        not audit history) or any other table.
        """
        async with self._connect() as conn:
            cur = await conn.execute("DELETE FROM proposals")
            proposals_deleted = cur.rowcount if cur.rowcount is not None else 0
            cur = await conn.execute("DELETE FROM orders")
            orders_deleted = cur.rowcount if cur.rowcount is not None else 0
            await conn.commit()
            return {
                "proposals": max(0, proposals_deleted),
                "orders": max(0, orders_deleted),
            }
