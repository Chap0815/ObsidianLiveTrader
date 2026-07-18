"""Async SQLite repository for proposals, previews, and orders (Task 7/8)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
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
    # ── Connection lifecycle (Q-03) ──────────────────────────────────────
    # ONE long-lived aiosqlite connection is held on the instance for the
    # lifetime of the process (opened in lifespan after init(), closed on
    # shutdown). The single-worker invariant (see main.lifespan / F-16) means
    # exactly one process + one event loop touches this DB, so a single shared
    # connection is safe: aiosqlite serializes every command onto that
    # connection's own background thread, so the request handlers AND the
    # background journal resolver funnel through one writer — WAL single-writer
    # semantics are preserved, not weakened.
    #
    # LAZY FALLBACK (important): the shared connection is OPTIONAL. Every method
    # goes through `_acquire()`, which yields the shared connection when one is
    # open and otherwise opens a throwaway per-call connection (the old
    # behaviour). This keeps the dozens of tests that build a Database and call
    # methods directly — WITHOUT open()/lifespan — working unchanged, and keeps
    # init() (which runs before open()) self-contained.

    def __init__(self, db_path: str):
        self.path = _resolve_path(db_path)
        self._shared: aiosqlite.Connection | None = None

    def _connect(self):
        # timeout is sqlite3's busy handler window (seconds): wait for a
        # concurrent writer instead of raising "database is locked" at once.
        return aiosqlite.connect(str(self.path), timeout=30.0)

    async def open(self) -> None:
        """Open the long-lived shared connection. Called once from lifespan
        AFTER init(). Idempotent; safe to call when already open."""
        if self._shared is not None:
            return
        conn = await self._connect()
        # Match init()'s per-connection pragma (WAL itself is a persistent DB
        # property, but busy_timeout is per-connection).
        await conn.execute("PRAGMA busy_timeout=30000;")
        self._shared = conn

    async def close(self) -> None:
        """Close the shared connection (called from lifespan on shutdown).
        After this, methods fall back to per-call connections again."""
        conn, self._shared = self._shared, None
        if conn is not None:
            await conn.close()

    @asynccontextmanager
    async def _acquire(self):
        """Yield the shared connection if open, else a throwaway per-call one.

        The shared connection is NEVER closed here (its lifecycle is open/close);
        the fallback path opens and closes a fresh connection exactly like the
        original per-call implementation. row_factory is set per query by the
        callers that need aiosqlite.Row — writers rely only on cursor.lastrowid
        / .rowcount, which are row_factory-independent, so a factory left over
        on the shared connection from a prior read is harmless.
        """
        shared = self._shared
        if shared is not None:
            yield shared
        else:
            async with self._connect() as conn:
                yield conn

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA busy_timeout=30000;")
            await conn.executescript(SCHEMA_SQL)
            # F2-07: additive migration for pre-existing DBs. CREATE TABLE IF
            # NOT EXISTS won't add a column to an already-created table, so add
            # realized_r_net explicitly when missing. Idempotent + safe: an
            # empty column defaults to NULL, which stats/SUM already ignore.
            cur = await conn.execute("PRAGMA table_info(journal_entries)")
            cols = {row[1] for row in await cur.fetchall()}
            if "realized_r_net" not in cols:
                await conn.execute(
                    "ALTER TABLE journal_entries ADD COLUMN realized_r_net REAL"
                )
            await conn.commit()

    async def insert_proposal(
        self,
        *,
        symbol: str,
        proposal_json: Any,
        annotations_json: Any | None = None,
        context_hash: str | None = None,
    ) -> int:
        async with self._acquire() as conn:
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
        async with self._acquire() as conn:
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
        async with self._acquire() as conn:
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
        async with self._acquire() as conn:
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
        async with self._acquire() as conn:
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
        async with self._acquire() as conn:
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

    # ── Journal + feedback-loop (KI shadow book) ────────────────────────
    # Advisory/measurement only: NEVER touches the order/gate/confirm path.
    # Every method here is defensive so a journal failure can be swallowed by
    # the caller (soft-fail) without breaking analyze / the resolver / startup.
    #
    # journal_entries has NO cap and NO retention/pruning: every /api/analyze
    # call writes one row and rows are never auto-deleted (only an explicit
    # POST /api/journal/clear removes them). This is a deliberate choice for
    # a single-user tool where the full measurement history has value -- the
    # table may grow unbounded over time. Revisit (retention window / archive
    # to a separate table) if this ever becomes multi-user or the table size
    # becomes a real problem; the created_at/status indexes below keep reads
    # cheap in the meantime.

    async def insert_journal_entry(
        self,
        *,
        symbol: str,
        tf: str,
        htf: str,
        action: str,
        direction: str | None,
        setup_confidence: str,
        entry_price: float | None,
        stop_loss: float | None,
        tp1: float | None,
        rrr: float | None,
        provider: str | None,
        model: str | None,
        scanner_summary: str | None,
        last_price_t0: float | None,
        status: str = "PENDING",
        proposal_id: int | None = None,
        created_at: str | None = None,
    ) -> int:
        async with self._acquire() as conn:
            cur = await conn.execute(
                """
                INSERT INTO journal_entries
                  (created_at, symbol, tf, htf, action, direction,
                   setup_confidence, entry_price, stop_loss, tp1, rrr,
                   provider, model, scanner_summary, last_price_t0,
                   status, proposal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at or _utc_now_iso(),
                    symbol,
                    tf,
                    htf,
                    action,
                    direction,
                    setup_confidence,
                    entry_price,
                    stop_loss,
                    tp1,
                    rrr,
                    provider,
                    model,
                    scanner_summary,
                    last_price_t0,
                    status,
                    proposal_id,
                ),
            )
            await conn.commit()
            return int(cur.lastrowid or 0)

    async def pending_journal_entries(self) -> list[dict[str, Any]]:
        """All rows still PENDING (the resolver's work queue), oldest first."""
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, created_at, symbol, tf, htf, action, direction,
                       entry_price, stop_loss, tp1, rrr, status
                FROM journal_entries
                WHERE status = 'PENDING'
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def update_journal_outcome(
        self,
        entry_id: int,
        *,
        status: str,
        resolved_at: str | None = None,
        resolved_price: float | None = None,
        realized_r: float | None = None,
        realized_r_net: float | None = None,
        ambiguous: int = 0,
    ) -> None:
        """Transition a PENDING row to a terminal state (WIN|LOSS|EXPIRED|SKIPPED|NO_FILL).

        Guarded by `status='PENDING'` in the WHERE clause so the resolver is
        idempotent: a row that already resolved is never revisited/overwritten.
        """
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE journal_entries
                SET status = ?, resolved_at = ?, resolved_price = ?,
                    realized_r = ?, realized_r_net = ?, ambiguous = ?,
                    last_checked_at = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (
                    status,
                    resolved_at or _utc_now_iso(),
                    resolved_price,
                    realized_r,
                    realized_r_net,
                    1 if ambiguous else 0,
                    _utc_now_iso(),
                    entry_id,
                ),
            )
            await conn.commit()

    async def touch_journal_checked(self, entry_id: int) -> None:
        """Record a resolver pass that left the row PENDING (debug/backoff)."""
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE journal_entries SET last_checked_at = ? WHERE id = ?",
                (_utc_now_iso(), entry_id),
            )
            await conn.commit()

    async def recent_journal(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent journal entries, newest first (read-only UI/endpoint feed)."""
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, created_at, symbol, tf, htf, action, direction,
                       setup_confidence, entry_price, stop_loss, tp1, rrr,
                       provider, model, scanner_summary, last_price_t0,
                       status, resolved_at, resolved_price, realized_r,
                       realized_r_net, ambiguous
                FROM journal_entries
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def journal_stats(self) -> dict[str, Any]:
        """Aggregate counts + per-group win/loss/rrr for the stats endpoint.

        Returns raw counts and per-group {wins, losses, sample, sum_r} tuples;
        the Wilson CI / rate math lives in the endpoint helper (pure Python,
        unit-tested) so this stays a thin SQL layer.
        """
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row

            async def _scalar(sql: str, params: tuple = ()) -> int:
                cur = await conn.execute(sql, params)
                row = await cur.fetchone()
                return int((row[0] if row and row[0] is not None else 0))

            # Q-04: one GROUP BY status instead of seven separate COUNT(*)
            # scans. `total` is the sum of the (mutually-exclusive) status
            # buckets — identical to the old standalone COUNT(*). Statuses
            # absent from the table simply have no row, so .get(..., 0) yields
            # the same 0 the old per-status COUNT returned. `stay_out` stays a
            # separate scan because it keys off `action`, which is orthogonal
            # to `status` (a STAY_OUT row is typically also SKIPPED — the two
            # counts intentionally overlap, exactly as before).
            cur = await conn.execute(
                "SELECT status, COUNT(*) AS n FROM journal_entries GROUP BY status"
            )
            status_counts: dict[str, int] = {}
            for r in await cur.fetchall():
                status_counts[str(r["status"])] = int(r["n"] or 0)

            pending = status_counts.get("PENDING", 0)
            expired = status_counts.get("EXPIRED", 0)
            skipped = status_counts.get("SKIPPED", 0)
            # F2-02: NO_FILL rows are NOT WIN/LOSS (never filled) -> excluded
            # from win-rate/sum_r everywhere below, surfaced as its own count.
            no_fill = status_counts.get("NO_FILL", 0)
            wins = status_counts.get("WIN", 0)
            losses = status_counts.get("LOSS", 0)
            total = sum(status_counts.values())
            stay_out = await _scalar(
                "SELECT COUNT(*) FROM journal_entries WHERE action = 'STAY_OUT'"
            )

            async def _groups(column: str) -> dict[str, dict[str, float]]:
                # `column` is always one of the hardcoded literals passed at
                # the call sites below ("setup_confidence"/"action"/"provider")
                # -- NEVER request-derived -- so interpolating it into the SQL
                # f-string is safe. Guard against a future caller ever passing
                # a user/request-controlled value here (that would be a SQL
                # injection foot-gun).
                cur = await conn.execute(
                    f"""
                    SELECT {column} AS grp,
                           SUM(CASE WHEN status='WIN'  THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END) AS losses,
                           SUM(CASE WHEN status IN ('WIN','LOSS')
                                    THEN realized_r ELSE 0 END) AS sum_r
                    FROM journal_entries
                    WHERE status IN ('WIN','LOSS') AND {column} IS NOT NULL
                    GROUP BY {column}
                    """
                )
                out: dict[str, dict[str, float]] = {}
                for r in await cur.fetchall():
                    grp = r["grp"]
                    if grp is None:
                        continue
                    out[str(grp)] = {
                        "wins": int(r["wins"] or 0),
                        "losses": int(r["losses"] or 0),
                        "sum_r": float(r["sum_r"] or 0.0),
                    }
                return out

            # Overall sum of realized_r over resolved (WIN|LOSS) rows.
            sum_r = await conn.execute(
                """
                SELECT SUM(realized_r) FROM journal_entries
                WHERE status IN ('WIN','LOSS')
                """
            )
            sr = await sum_r.fetchone()
            overall_sum_r = float(sr[0]) if sr and sr[0] is not None else 0.0

            # F2-07: NET sum over resolved rows (Task 21's feedback uses net,
            # not gross). Pre-migration WIN/LOSS rows have NULL net -> SUM skips
            # them, so this stays 0.0 until the resolver repopulates.
            sum_r_net = await conn.execute(
                """
                SELECT SUM(realized_r_net) FROM journal_entries
                WHERE status IN ('WIN','LOSS')
                """
            )
            srn = await sum_r_net.fetchone()
            overall_sum_r_net = float(srn[0]) if srn and srn[0] is not None else 0.0

            return {
                "total": total,
                "stay_out": stay_out,
                "pending": pending,
                "expired": expired,
                "skipped": skipped,
                "no_fill": no_fill,
                "wins": wins,
                "losses": losses,
                "overall_sum_r": overall_sum_r,
                "overall_sum_r_net": overall_sum_r_net,
                "by_confidence": await _groups("setup_confidence"),
                "by_action": await _groups("action"),
                "by_provider": await _groups("provider"),
            }

    async def clear_journal(self) -> int:
        """Delete all journal_entries. Separate from clear_history on purpose
        (the journal is the measurement dataset and survives history-clear)."""
        async with self._acquire() as conn:
            cur = await conn.execute("DELETE FROM journal_entries")
            deleted = cur.rowcount if cur.rowcount is not None else 0
            await conn.commit()
            return max(0, deleted)

    async def clear_history(self) -> dict[str, int]:
        """Delete all rows from the audit history tables (proposals + orders).

        Does NOT touch order_previews (short-lived one-time confirm tokens,
        not audit history) or any other table.
        """
        async with self._acquire() as conn:
            cur = await conn.execute("DELETE FROM proposals")
            proposals_deleted = cur.rowcount if cur.rowcount is not None else 0
            cur = await conn.execute("DELETE FROM orders")
            orders_deleted = cur.rowcount if cur.rowcount is not None else 0
            await conn.commit()
            return {
                "proposals": max(0, proposals_deleted),
                "orders": max(0, orders_deleted),
            }
