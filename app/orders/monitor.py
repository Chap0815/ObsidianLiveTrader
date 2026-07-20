"""Server-side Trade-Management monitor loop (spec §2/§3/§7).

This is the MONEY-EXECUTING component of the Trade-Management-Layer: a single
background task, started in lifespan and cancelled+awaited on shutdown (mirror
of ``run_resolver_loop``), that autonomously moves REAL stop-losses to
break-even on armed positions. Every safety-critical decision is delegated:

- The *decision* to move a stop is made by the pure ``evaluate_rules`` (no I/O).
- The *execution* runs EXCLUSIVELY through ``OrderService.modify_stop_loss`` —
  which owns the never-unprotected place→verify→cancel dance, the shared
  ``trade_lock`` and the audit row. The monitor NEVER writes orders on the
  exchange client directly (spec §3.2).
- Auto-BE is HL-only (same gate as ``modify_stop_loss``), fires at most once per
  position (``be_done``), and only when armed (spec §3.1/§3.4/§3.6).

Fail-safe everywhere (spec §3.7): the whole cycle is wrapped, and EACH position
is wrapped independently so one bad position never aborts the sweep or crashes
the loop. Single-process (Q-02): reuses the app-global ``trade_lock`` via the
service; no second writer process.

Factoring for tests: the loop body is ``_run_one_cycle(app, now_ms)`` so unit
tests can drive exactly ONE cycle with a fake client + in-memory DB and never
touch the infinite ``while True`` loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.orders.protection import classify_protection
from app.orders.trade_manager import Alert, MgmtBaseline, MoveSlToBe, evaluate_rules

log = logging.getLogger("app.orders.monitor")


def _client(app: Any) -> Any | None:
    """Active exchange client — resolved exactly like requests/resolver do."""
    return getattr(app.state, "mexc", None) or getattr(app.state, "exchange", None)


def _make_order_service(app: Any, client: Any, settings: Any) -> Any:
    """Build the SHARED-lock OrderService the monitor executes auto-BE through.

    Kept a module-level function (not inlined) so the money-write path is a
    single seam the tests monkeypatch to inject a ``modify_stop_loss`` spy —
    production always constructs a real OrderService bound to the app-global
    ``trade_lock`` (single-writer, spec §3.8).
    """
    from app.orders.service import OrderService

    store = getattr(app.state, "preview_store", None)
    db = getattr(app.state, "db", None)
    lock = getattr(app.state, "trade_lock", None)
    return OrderService(client, settings, store, db=db, trade_lock=lock)


def _iso_to_ms(value: Any) -> int | None:
    """Best-effort ISO-8601 string → epoch ms; None on anything unparseable."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


async def _best_effort_opened_at(
    client: Any, db: Any, symbol: str, side: str, now_ms: int
) -> int:
    """Position open time in ms — best-effort, only feeds the time-stop alarm.

    Priority: HL ``user_fills`` opening fill (earliest matching Open) → newest
    journal entry for (symbol, direction) → ``now_ms``. Never raises.
    """
    want = "long" if side == "long" else "short"
    # 1) HL opening fill (exchange truth for the open time).
    try:
        if hasattr(client, "user_fills"):
            fills = await client.user_fills(symbol)
            times = []
            for f in fills or []:
                d = str(f.get("dir") or "").lower()
                t = int(f.get("time") or 0)
                if "open" in d and want in d and t > 0:
                    times.append(t)
            if times:
                return min(times)
    except Exception:
        pass
    # 2) Journal approximation (MEXC has no user_fills; also HL fallback).
    try:
        if db is not None:
            rows = await db.recent_journal()
            for r in rows or []:
                if r.get("symbol") == symbol and str(
                    r.get("direction") or ""
                ).lower() == side:
                    ms = _iso_to_ms(r.get("created_at"))
                    if ms:
                        return ms
    except Exception:
        pass
    # 3) Last resort: now (time-stop simply won't fire yet — fail-safe).
    return now_ms


async def _best_effort_invalidation(db: Any, symbol: str) -> float | None:
    """Thesis-invalidation price from the latest proposal for the symbol.

    Heuristic (spec §10): newest proposal for the symbol. Fail-safe — any miss
    or decode error yields None (no alarm) rather than a wrong alarm/crash.
    """
    try:
        if db is None:
            return None
        prop = await db.latest_proposal_for_symbol(symbol)
        if not prop:
            return None
        p = prop.get("proposal")
        if isinstance(p, dict):
            v = p.get("invalidation_price")
            if v not in (None, ""):
                return float(v)
    except Exception:
        pass
    return None


async def _current_sl(client: Any, symbol: str, side: str, entry: float) -> float | None:
    """Current protective stop for one position via open trigger orders.

    Fail-safe: a lookup failure yields None (treated as "no known SL" — with
    r1=0 that means no auto-BE, never a fabricated stop).
    """
    try:
        stops = await client.open_stop_orders(symbol)
        sl, _tp = classify_protection(stops, side=side, entry=entry)
        return sl
    except Exception:
        return None


async def _process_position(
    app: Any,
    db: Any,
    client: Any,
    settings: Any,
    service: Any,
    pos: dict[str, Any],
    now_ms: int,
) -> None:
    """Evaluate + act on ONE open position. Raises only on unexpected bugs; the
    caller wraps this per-position so a failure can't abort the cycle."""
    symbol = pos.get("symbol")
    side = pos.get("side")
    entry = pos.get("entry_price")
    if not symbol or side not in ("long", "short"):
        return
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return
    if entry <= 0:
        return

    current_sl = await _current_sl(client, symbol, side, entry)

    # Mark price — required to evaluate any rule geometry. No mark → skip (the
    # next cycle retries); never guess.
    try:
        ticker = await client.ticker(symbol)
        mark = float(ticker.last_price) if ticker.last_price else None
    except Exception:
        mark = None
    if mark is None or mark <= 0:
        return

    # r1 is fixed at baseline time. With no known SL it can't be computed → 0.0,
    # which evaluate_rules reads as "no R signal" (no auto-BE / no time-stop-R
    # gate) — safe until a real stop exists.
    r1 = abs(entry - current_sl) if current_sl is not None else 0.0
    opened_at = await _best_effort_opened_at(client, db, symbol, side, now_ms)
    invalidation = await _best_effort_invalidation(db, symbol)

    # Durable baseline. First sighting inserts; a later sighting refreshes in
    # place (arming/be_done/alert-state preserved), unless entry deviated beyond
    # tolerance → treated as a NEW position by the repo (reset). §4/§5.
    await db.upsert_position_mgmt(
        symbol,
        side,
        entry_snap=entry,
        initial_sl_snap=current_sl if current_sl is not None else entry,
        r1=r1,
        opened_at=opened_at,
        invalidation_price=invalidation,
    )
    mgmt_row = await db.get_open_position_mgmt(symbol, side)
    if mgmt_row is None:
        return

    baseline = MgmtBaseline(
        entry=float(mgmt_row.get("entry_snap") or entry),
        initial_sl=mgmt_row.get("initial_sl_snap"),
        r1=float(mgmt_row.get("r1") or 0.0),
        opened_at_ms=int(mgmt_row.get("opened_at") or now_ms),
        invalidation_price=mgmt_row.get("invalidation_price"),
        armed_rules=mgmt_row.get("armed_rules") or {},
        be_done=bool(mgmt_row.get("be_done")),
        last_alert_state=mgmt_row.get("last_alert_state") or {},
    )

    actions = evaluate_rules(
        side=side,
        entry=entry,
        current_sl=current_sl,
        mark=mark,
        mgmt=baseline,
        now_ms=now_ms,
        settings=settings,
    )

    # Working copy of the debounce/feed state; persisted once at the end.
    alert_state = dict(baseline.last_alert_state)
    state_dirty = False

    for action in actions:
        if isinstance(action, MoveSlToBe):
            # HL-only gate — identical predicate to modify_stop_loss (§3.6). A
            # non-HL client is surfaced as an informational feed entry, never a
            # direct order write.
            if not hasattr(client, "place_stop_order"):
                alert_state["auto_be_unavailable"] = {
                    "active": True,
                    "message": "Auto-BE ist nur auf Hyperliquid verfuegbar.",
                    "ts": now_ms,
                }
                state_dirty = True
                continue
            try:
                await service.modify_stop_loss(
                    symbol=symbol, side=side, new_sl=action.new_sl
                )
            except Exception as e:  # noqa: BLE001 — must not abort the cycle
                log.warning(
                    "auto-BE modify_stop_loss failed for %s %s: %s",
                    symbol,
                    side,
                    e,
                )
                alert_state["auto_be_error"] = {
                    "active": True,
                    "message": f"Auto-BE fehlgeschlagen: {e}",
                    "ts": now_ms,
                }
                state_dirty = True
                # Do NOT mark be_done — leave it armed to retry next cycle.
                continue
            # Success: single-shot disarm + visible auto-action feed entry.
            await db.mark_be_done(symbol, side)
            alert_state["auto_be"] = {
                "active": True,
                "reason": action.reason,
                "new_sl": action.new_sl,
                "message": f"App hat SL auf BE gezogen ({action.reason}).",
                "ts": now_ms,
            }
            state_dirty = True
        elif isinstance(action, Alert):
            alert_state[action.kind] = {
                "active": True,
                "message": action.message,
                "ts": now_ms,
            }
            state_dirty = True

    if state_dirty:
        await db.set_alert_state(symbol, side, alert_state)


async def _run_one_cycle(app: Any, now_ms: int) -> None:
    """One monitor sweep. Fail-safe per position; never raises to the loop.

    Factored out of the infinite loop so tests can run exactly one cycle.
    """
    settings = get_settings()
    db = getattr(app.state, "db", None)
    client = _client(app)
    if db is None or client is None:
        return

    try:
        snap = await client.account_snapshot()
    except Exception:
        log.warning("trade monitor: account_snapshot failed", exc_info=True)
        return
    positions = (snap.get("positions") if isinstance(snap, dict) else None) or []

    # Build the shared-lock service ONCE per cycle (single seam for auto-BE
    # writes). Never fatal — if it can't be built, positions still evaluate for
    # alarms and auto-BE simply records a failure.
    try:
        service = _make_order_service(app, client, settings)
    except Exception:
        service = None

    live_keys: set[tuple[Any, Any]] = set()
    for pos in positions:
        symbol = pos.get("symbol") if isinstance(pos, dict) else None
        side = pos.get("side") if isinstance(pos, dict) else None
        if symbol and side in ("long", "short"):
            live_keys.add((symbol, side))
        try:
            await _process_position(app, db, client, settings, service, pos, now_ms)
        except Exception:
            # One bad position must never abort the sweep (spec §3.7).
            log.warning("trade monitor: position cycle failed", exc_info=True)
            continue

    # Positions no longer live → close their mgmt record (frees the OPEN slot so
    # a later re-open starts a fresh baseline).
    try:
        for row in await db.list_open_position_mgmt():
            key = (row.get("symbol"), row.get("side"))
            if key not in live_keys:
                await db.close_position_mgmt(row.get("symbol"), row.get("side"))
    except Exception:
        log.warning("trade monitor: close-vanished sweep failed", exc_info=True)


async def run_trade_monitor_loop(app: Any) -> None:
    """Background task: evaluate open positions on an interval and act.

    Started in lifespan() (only when ``tm_enabled``), cancelled+awaited on
    shutdown. NEVER crashes the app: the whole cycle body is wrapped and it
    sleeps regardless of outcome (mirror of ``run_resolver_loop``). Relies on the
    single-worker invariant so exactly one monitor runs.
    """
    while True:
        try:
            interval = int(getattr(get_settings(), "tm_monitor_interval_s", 20))
        except Exception:
            interval = 20
        await asyncio.sleep(max(1, interval))
        try:
            settings = get_settings()
            if not getattr(settings, "tm_enabled", True):
                continue
            now_ms = int(time.time() * 1000)
            await _run_one_cycle(app, now_ms)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("trade monitor cycle failed", exc_info=True)
