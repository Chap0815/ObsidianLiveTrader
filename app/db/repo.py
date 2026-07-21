"""Async SQLite repository for proposals, previews, and orders (Task 7/8)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# Task 4 (trade-management-layer spec §4/§5): a position is treated as the
# SAME open trade across upserts as long as entry_snap hasn't moved beyond a
# small tolerance (mark noise / re-fetch jitter). Anything larger means the
# position was closed and a new one opened at a different entry -> the r1
# baseline must reset (never silently inherit a stale 1R from a prior trade).
_ENTRY_TOLERANCE_REL = 0.001  # 0.1% relative
_ENTRY_TOLERANCE_ABS_FLOOR = 1e-6


def _entry_deviated(old_entry: float, new_entry: float) -> bool:
    tolerance = max(abs(old_entry) * _ENTRY_TOLERANCE_REL, _ENTRY_TOLERANCE_ABS_FLOOR)
    return abs(new_entry - old_entry) > tolerance


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


def _decode_position_mgmt_row(d: dict[str, Any]) -> dict[str, Any]:
    """Decode the JSON TEXT columns (armed_rules / last_alert_state) to dicts."""
    d["armed_rules"] = _loads(d.get("armed_rules")) or {}
    d["last_alert_state"] = _loads(d.get("last_alert_state")) or {}
    return d


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
            # Task 20 (F2-04/K2-04, F2-08, F2-12): additive attribution/versioning
            # columns. Same idempotent pattern — CREATE TABLE IF NOT EXISTS never
            # adds a column to an already-created table, so on a pre-existing DB
            # (real journal data present) these must be ADDed explicitly or every
            # INSERT would break with "no such column". Bestandszeilen = NULL.
            # Literals are hardcoded (never request-derived) -> f-string is safe.
            # Block 2/TP2 Task P1: regime is additive too -- same idempotent
            # pattern (fail-safe fallback value is "unknown", NULL on old rows).
            # Lern-Loop fix: `order_type` ('market'|'limit') so the shadow-fill
            # resolver can model market entries as index-0 fills instead of
            # treating every entry as a limit. Same idempotent TEXT-ALTER pattern;
            # NULL on legacy rows keeps the old conservative LIMIT behaviour.
            for _col in (
                "setup_type", "context_hash", "prompt_version", "regime", "order_type"
            ):
                if _col not in cols:
                    await conn.execute(
                        f"ALTER TABLE journal_entries ADD COLUMN {_col} TEXT"
                    )
            # Index on setup_type for the by_setup GROUP BY. Created HERE (not in
            # SCHEMA_SQL) so it runs only AFTER the column exists on both fresh
            # DBs (CREATE TABLE above) and migrated DBs (ALTER above) — a CREATE
            # INDEX inside SCHEMA_SQL would fail on an old DB whose table has no
            # setup_type column yet (executescript runs before this migration).
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_setup "
                "ON journal_entries(setup_type)"
            )
            # Same reasoning for regime's by_regime GROUP BY (Task P1).
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_regime "
                "ON journal_entries(regime)"
            )
            # TML v2 (Task V3): additive high_water column for pre-existing DBs.
            # Same idempotent pattern as above -- CREATE TABLE IF NOT EXISTS never
            # adds a column to an already-created table. Literal is hardcoded
            # (never request-derived) -> f-string/execute is safe.
            cur = await conn.execute("PRAGMA table_info(position_management)")
            pm_cols = {row[1] for row in await cur.fetchall()}
            if "high_water" not in pm_cols:
                await conn.execute(
                    "ALTER TABLE position_management ADD COLUMN high_water REAL"
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

    async def latest_proposal_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Task 21 (O2-06): the most recent stored proposal for a symbol — the
        ORIGINAL thesis the reevaluate path compares current structure against.
        Returns None (never raises for a missing row) when there is no proposal;
        the caller soft-fails so reevaluate is never broken by a lookup miss."""
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, created_at, symbol, proposal_json
                FROM proposals
                WHERE symbol = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol,),
            )
            r = await cur.fetchone()
            if r is None:
                return None
            d = dict(r)
            d["proposal"] = _loads(d.pop("proposal_json"))
            return d

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
        setup_type: str | None = None,
        context_hash: str | None = None,
        prompt_version: str | None = None,
        regime: str | None = None,
        order_type: str | None = None,
        dedupe_window_min: int = 30,
    ) -> int:
        now = created_at or _utc_now_iso()
        async with self._acquire() as conn:
            # F2-08 dedupe: repeatedly analysing the SAME context within a short
            # window would otherwise write N correlated rows and inflate the
            # sample (tightening the Wilson CI dishonestly). If a still-PENDING
            # row with the same context_hash exists inside the window, UPDATE it
            # in place with the latest analysis instead of inserting a new row.
            # Only PENDING rows are collapsed — a row that already resolved
            # (WIN/LOSS/…) is a real observation and must never be overwritten.
            if context_hash:
                try:
                    base_dt = datetime.fromisoformat(now)
                except ValueError:
                    base_dt = datetime.now(timezone.utc)
                threshold = (
                    base_dt - timedelta(minutes=dedupe_window_min)
                ).isoformat()
                cur = await conn.execute(
                    """
                    SELECT id FROM journal_entries
                    WHERE context_hash = ? AND status = 'PENDING'
                          AND created_at >= ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (context_hash, threshold),
                )
                dup = await cur.fetchone()
                if dup is not None:
                    existing_id = int(dup[0])
                    await conn.execute(
                        """
                        UPDATE journal_entries
                        SET created_at = ?, symbol = ?, tf = ?, htf = ?,
                            action = ?, direction = ?, setup_confidence = ?,
                            entry_price = ?, stop_loss = ?, tp1 = ?, rrr = ?,
                            provider = ?, model = ?, scanner_summary = ?,
                            last_price_t0 = ?, status = ?, proposal_id = ?,
                            setup_type = ?, prompt_version = ?, regime = ?,
                            order_type = ?
                        WHERE id = ?
                        """,
                        (
                            now, symbol, tf, htf, action, direction,
                            setup_confidence, entry_price, stop_loss, tp1, rrr,
                            provider, model, scanner_summary, last_price_t0,
                            status, proposal_id, setup_type, prompt_version,
                            regime, order_type, existing_id,
                        ),
                    )
                    await conn.commit()
                    return existing_id

            cur = await conn.execute(
                """
                INSERT INTO journal_entries
                  (created_at, symbol, tf, htf, action, direction,
                   setup_confidence, entry_price, stop_loss, tp1, rrr,
                   provider, model, scanner_summary, last_price_t0,
                   status, proposal_id, setup_type, context_hash, prompt_version,
                   regime, order_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
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
                    setup_type,
                    context_hash,
                    prompt_version,
                    regime,
                    order_type,
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
                       entry_price, stop_loss, tp1, rrr, status, order_type
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
                                    THEN realized_r ELSE 0 END) AS sum_r,
                           -- F2-10: ambiguous resolved rows + a CLEAN win/loss
                           -- split (ambiguous excluded) so the endpoint can show
                           -- a win rate that isn't inflated by intrabar ties.
                           SUM(CASE WHEN status IN ('WIN','LOSS') AND ambiguous=1
                                    THEN 1 ELSE 0 END) AS ambiguous,
                           SUM(CASE WHEN status='WIN'  AND ambiguous=0
                                    THEN 1 ELSE 0 END) AS clean_wins,
                           SUM(CASE WHEN status='LOSS' AND ambiguous=0
                                    THEN 1 ELSE 0 END) AS clean_losses
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
                        "ambiguous": int(r["ambiguous"] or 0),
                        "clean_wins": int(r["clean_wins"] or 0),
                        "clean_losses": int(r["clean_losses"] or 0),
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
            # them, so this stays 0.0 until the resolver repopulates. COUNT(col)
            # counts only non-NULL rows -> dedicated net denominator so the net
            # average is NOT diluted by un-backfilled pre-migration rows.
            sum_r_net = await conn.execute(
                """
                SELECT SUM(realized_r_net), COUNT(realized_r_net)
                FROM journal_entries
                WHERE status IN ('WIN','LOSS')
                """
            )
            srn = await sum_r_net.fetchone()
            overall_sum_r_net = float(srn[0]) if srn and srn[0] is not None else 0.0
            overall_net_sample = int(srn[1]) if srn and srn[1] is not None else 0

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
                "overall_net_sample": overall_net_sample,
                "by_confidence": await _groups("setup_confidence"),
                "by_action": await _groups("action"),
                "by_provider": await _groups("provider"),
                # F2-04/K2-04 attribution: win rate keyed by the setup type
                # (chart_pattern[/time_horizon]). NULL setup_type rows are
                # excluded by the _groups WHERE clause (pre-migration rows).
                "by_setup": await _groups("setup_type"),
                # Block 2/TP2 Task P1: win rate keyed by the persisted regime
                # tag (btc-trend x vol bucket). Only NULL rows (pre-migration,
                # never analyzed with this feature) are excluded by _groups'
                # WHERE clause; "unknown" is a real string value and stays IN
                # the group like any other segment.
                "by_regime": await _groups("regime"),
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

    # ── Trade-Management-Layer: position_management (Task 4) ───────────
    # Durable baseline (entry/initial SL/1R/opened_at/thesis-invalidation) +
    # arming + alert-debounce state per open position. A dedicated table that
    # survives clear_history()/clear_journal() (neither touches it -- see
    # above; both only DELETE FROM proposals/orders/journal_entries).
    #
    # Identity = (symbol, side); at most one OPEN row per identity is enforced
    # by the partial UNIQUE index in schema.py. upsert_position_mgmt() is the
    # single write path that creates/refreshes/resets that OPEN row so the
    # invariant never has to be re-checked by callers.

    async def upsert_position_mgmt(
        self,
        symbol: str,
        side: str,
        *,
        entry_snap: float,
        initial_sl_snap: float,
        r1: float,
        opened_at: int | None,
        invalidation_price: float | None,
    ) -> int:
        """Insert the OPEN record for (symbol, side) or refresh the existing one.

        If there is no OPEN record yet, OR the existing OPEN record's
        entry_snap has moved beyond `_entry_deviated`'s tolerance, this is
        treated as a NEW position: be_done, armed_rules and last_alert_state
        all reset to their defaults (fresh baseline; the user must re-arm --
        default is alarm-only, never silently inherit an old arming/1R).
        Otherwise the existing row is updated in place (arming/be_done/alert
        state preserved) and its id is returned unchanged.

        `high_water` (TML v2, Task V3): initialized to `entry_snap` on a fresh
        baseline (INSERT or entry-deviation reset) -- a conservative starting
        point (never claims a more favorable high/low than the entry itself)
        that also avoids NULL-handling downstream. It is NEVER written on the
        non-deviated FROZEN refresh path above -- it only advances via the
        dedicated `update_high_water()` UPDATE, monotonically.
        """
        now = _now_ms()
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, entry_snap FROM position_management
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (symbol, side),
            )
            existing = await cur.fetchone()

            if existing is not None and not _entry_deviated(
                float(existing["entry_snap"]), entry_snap
            ):
                # Non-deviated re-sighting: the BASELINE (entry_snap,
                # initial_sl_snap, r1, opened_at) is FROZEN at creation so "+1R"
                # is always measured from the ORIGINAL risk even after the stop
                # is later moved (spec §4 — r1 must stay stable when the SL
                # wanders). Only the volatile invalidation_price (a fresh proposal
                # may update it) and updated_at are refreshed.
                await conn.execute(
                    """
                    UPDATE position_management
                    SET invalidation_price = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (invalidation_price, now, existing["id"]),
                )
                await conn.commit()
                return int(existing["id"])

            if existing is not None:
                # Entry deviated beyond tolerance -> same (symbol, side) but a
                # NEW position: reset arming/be_done/alert-state, fresh
                # baseline, reuse the row (keeps the partial-unique invariant
                # trivially satisfied -- no delete+insert race).
                await conn.execute(
                    """
                    UPDATE position_management
                    SET entry_snap = ?, initial_sl_snap = ?, r1 = ?, opened_at = ?,
                        invalidation_price = ?, armed_rules = '{}', be_done = 0,
                        last_alert_state = '{}', status = 'OPEN', updated_at = ?,
                        high_water = ?
                    WHERE id = ?
                    """,
                    (
                        entry_snap, initial_sl_snap, r1, opened_at,
                        invalidation_price, now, entry_snap, existing["id"],
                    ),
                )
                await conn.commit()
                return int(existing["id"])

            # Atomic UPSERT (single statement) on the partial-unique index
            # (symbol, side) WHERE status='OPEN'. This replaces the former
            # INSERT-then-catch-IntegrityError-then-conn.rollback() recovery: on the
            # SHARED connection (Q-03) that connection-wide rollback could discard a
            # concurrent coroutine's (resolver / monitor / HTTP) still-uncommitted
            # write, silently losing it. One ON CONFLICT DO UPDATE has NO rollback,
            # NO savepoint and NO multi-statement window that interleaving or a
            # foreign commit could corrupt. On the (symbol, side) race the DO UPDATE
            # refreshes the winner's baseline + volatile fields in place — the same
            # outcome the old catch-path produced. Needs SQLite >= 3.35 (partial
            # ON CONFLICT target >= 3.24, RETURNING >= 3.35); shipped build is 3.49.
            cur = await conn.execute(
                """
                INSERT INTO position_management
                  (symbol, side, entry_snap, initial_sl_snap, r1, opened_at,
                   invalidation_price, armed_rules, be_done, last_alert_state,
                   status, created_at, updated_at, high_water)
                VALUES (?, ?, ?, ?, ?, ?, ?, '{}', 0, '{}', 'OPEN', ?, ?, ?)
                ON CONFLICT(symbol, side) WHERE status = 'OPEN' DO UPDATE SET
                  entry_snap = excluded.entry_snap,
                  initial_sl_snap = excluded.initial_sl_snap,
                  r1 = excluded.r1,
                  opened_at = excluded.opened_at,
                  invalidation_price = excluded.invalidation_price,
                  updated_at = excluded.updated_at,
                  high_water = excluded.high_water
                RETURNING id
                """,
                (
                    symbol, side, entry_snap, initial_sl_snap, r1, opened_at,
                    invalidation_price, now, now, entry_snap,
                ),
            )
            row = await cur.fetchone()
            await conn.commit()
            return int(row["id"]) if row else 0

    async def get_open_position_mgmt(
        self, symbol: str, side: str
    ) -> dict[str, Any] | None:
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT * FROM position_management
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (symbol, side),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return _decode_position_mgmt_row(dict(row))

    async def list_open_position_mgmt(self) -> list[dict[str, Any]]:
        async with self._acquire() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM position_management WHERE status = 'OPEN' ORDER BY id ASC"
            )
            rows = await cur.fetchall()
            return [_decode_position_mgmt_row(dict(r)) for r in rows]

    async def set_armed_rules(
        self, symbol: str, side: str, rules: dict[str, Any]
    ) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE position_management
                SET armed_rules = ?, updated_at = ?
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (_dumps(rules) or "{}", _now_ms(), symbol, side),
            )
            await conn.commit()

    async def mark_be_done(self, symbol: str, side: str) -> None:
        """Idempotent: setting be_done=1 again on an already-done row is a no-op."""
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE position_management
                SET be_done = 1, updated_at = ?
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (_now_ms(), symbol, side),
            )
            await conn.commit()

    async def update_high_water(self, symbol: str, side: str, mark: float) -> None:
        """Monotonic Chandelier high-water update (TML v2, Task V3).

        Its OWN UPDATE -- deliberately NOT part of upsert_position_mgmt's
        FROZEN non-deviated path -- so it moves every monitor cycle regardless
        of whether the entry/SL baseline itself is frozen.

        long:  high_water = MAX(COALESCE(high_water, mark), mark)  -- never decreases.
        short: high_water = MIN(COALESCE(high_water, mark), mark)  -- never increases
               (it tracks a running LOW for shorts, reusing the same column).

        SQLite's MAX(x, y) / MIN(x, y) with two+ arguments is the multi-arg
        SCALAR function (row-wise), not the single-arg aggregate -- exactly
        what's needed here. Only the OPEN record for (symbol, side) is
        touched; if none exists this is a no-op (no row created).
        """
        fn = "MAX" if side == "long" else "MIN"
        async with self._acquire() as conn:
            await conn.execute(
                f"""
                UPDATE position_management
                SET high_water = {fn}(COALESCE(high_water, ?), ?), updated_at = ?
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (mark, mark, _now_ms(), symbol, side),
            )
            await conn.commit()

    async def reset_position_mgmt_baseline(self, symbol: str, side: str) -> None:
        """Re-arm/reopen fresh baseline: zero ``be_done`` and re-seed
        ``high_water`` to ``entry_snap`` on the OPEN record for (symbol, side).

        Used when a position REAPPEARS after an absence (a potential same-price
        reopen the entry-deviation check in upsert_position_mgmt can't catch,
        because entry_snap barely moved) so a fresh position never inherits a
        stale BE latch or a stale trail high-water from the prior trade. Only the
        one-shot-BE latch + the Chandelier high-water are reset; the frozen risk
        baseline (entry_snap/initial_sl_snap/r1) and the user's armed_rules are
        deliberately preserved (a reopen at ~the same entry keeps the same risk
        geometry and the same arming intent). Idempotent no-op if no OPEN record
        exists. high_water=entry_snap mirrors the fresh-baseline seed in
        upsert_position_mgmt (never claims a more favorable extreme than entry).
        """
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE position_management
                SET be_done = 0, high_water = entry_snap, updated_at = ?
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (_now_ms(), symbol, side),
            )
            await conn.commit()

    async def set_alert_state(
        self, symbol: str, side: str, state: dict[str, Any]
    ) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE position_management
                SET last_alert_state = ?, updated_at = ?
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (_dumps(state) or "{}", _now_ms(), symbol, side),
            )
            await conn.commit()

    async def close_position_mgmt(self, symbol: str, side: str) -> None:
        async with self._acquire() as conn:
            await conn.execute(
                """
                UPDATE position_management
                SET status = 'CLOSED', updated_at = ?
                WHERE symbol = ? AND side = ? AND status = 'OPEN'
                """,
                (_now_ms(), symbol, side),
            )
            await conn.commit()

    async def disarm_all(self) -> int:
        """Kill-switch (spec §3.5): empty armed_rules on every OPEN row so no
        auto-action fires again until the user re-arms. Returns the count of
        rows affected (0 when nothing was armed/open)."""
        async with self._acquire() as conn:
            cur = await conn.execute(
                """
                UPDATE position_management
                SET armed_rules = '{}', updated_at = ?
                WHERE status = 'OPEN'
                """,
                (_now_ms(),),
            )
            affected = cur.rowcount if cur.rowcount is not None else 0
            await conn.commit()
            return max(0, affected)
