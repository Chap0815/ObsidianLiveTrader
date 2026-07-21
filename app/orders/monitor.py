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
import math
import time
from datetime import datetime
from typing import Any

from app.analysis.indicators import compute_atr
from app.config import get_settings
from app.orders.protection import classify_protection
from app.orders.trade_manager import Alert, MgmtBaseline, MoveSlToBe, evaluate_rules

log = logging.getLogger("app.orders.monitor")

# Sentinel for "argument not supplied" so a caller can inject an already-read
# value that is legitimately None (no stop) without it being mistaken for
# "please read it yourself" (Finding 3).
_UNSET: Any = object()


async def _safe_user_fills(client: Any, symbol: str) -> list[dict[str, Any]] | None:
    """ONE best-effort user_fills read, reused by BOTH the opened_at estimate and
    the HL trade-epoch signature within a single position cycle (Finding 3:
    previously each fetched its own copy). Returns None when the client exposes
    no ``user_fills`` (MEXC) or the read RAISED — callers treat None exactly like
    an empty/failed fill window (journal fallback / inconclusive signature)."""
    if not hasattr(client, "user_fills"):
        return None
    try:
        return await client.user_fills(symbol)
    except Exception:
        return None


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
    client: Any, db: Any, symbol: str, side: str, now_ms: int, *, fills: Any = _UNSET
) -> int:
    """Position open time in ms — best-effort, only feeds the time-stop alarm.

    Priority: HL ``user_fills`` opening fill (earliest matching Open) → newest
    journal entry for (symbol, direction) → ``now_ms``. Never raises.

    ``fills`` may be an already-fetched fill window (Finding 3: shared with the
    epoch-signature read so the position is only queried once). ``_UNSET`` ⇒ read
    it here; ``None``/`[]` ⇒ no usable fills, fall straight through to journal.
    """
    want = "long" if side == "long" else "short"
    if fills is _UNSET:
        fills = await _safe_user_fills(client, symbol)
    # 1) HL opening fill (exchange truth for the open time).
    try:
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
    # F5: recent_journal is newest-first, so returning the FIRST match picked the
    # YOUNGEST row — every fresh proposal then "rejuvenated" opened_at and the
    # time-stop could never reach its threshold. Take the OLDEST matching row in
    # the recent window instead (best-effort: the window is capped, so this is the
    # oldest KNOWN sighting of the active trade, not necessarily its true open).
    try:
        if db is not None:
            rows = await db.recent_journal()
            oldest: int | None = None
            for r in rows or []:
                if r.get("symbol") == symbol and str(
                    r.get("direction") or ""
                ).lower() == side:
                    ms = _iso_to_ms(r.get("created_at"))
                    if ms and (oldest is None or ms < oldest):
                        oldest = ms
            if oldest is not None:
                return oldest
    except Exception:
        pass
    # 3) Last resort: now (time-stop simply won't fire yet — fail-safe).
    return now_ms


def _position_id_signature(pos: Any) -> int | None:
    """MEXC (and any client exposing a STABLE exchange position id): the
    ``positionId`` carried in the positions snapshot.

    A close+reopen gets a NEW positionId; add-ons keep it — so it is a stable
    trade-identity anchor, unlike anything derived from a rolling fill window.
    None when absent or non-integer → INCONCLUSIVE (never resets). NOTE: HL's
    snapshot ``position_id`` is just the coin (not a per-trade id), so this is
    used ONLY on the non-HL path; HL uses ``_hl_epoch_signature`` instead.
    """
    if not isinstance(pos, dict):
        return None
    pid = pos.get("position_id")
    if pid in (None, ""):
        return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


async def _hl_epoch_signature(
    client: Any, symbol: str, side: str, *, fills: Any = _UNSET
) -> int | None:
    """HL trade-epoch anchor: the time of the NEWEST Flat→Open fill
    (``start_position`` == 0) for this side.

    Why not ``min()``/``max()`` over the whole fill window: that is UNSTABLE.
    The earliest fill can roll out of the rolling userFills window mid-trade
    (spurious signature jump → false reset), and a lingering original fill can
    keep the signature unchanged across a real reopen (false negative). The
    Flat→Open fill is the true epoch: a reopen mints a NEW one (newer time) →
    the signature changes → reset fires; add-ons/partial fills carry a non-zero
    ``start_position`` and never move the anchor. If NO epoch fill is currently
    in the window, return None = INCONCLUSIVE — deliberately never spring a
    reset just because the anchor scrolled out of view. Never raises.
    """
    want = "long" if side == "long" else "short"
    if fills is _UNSET:
        try:
            fills = await client.user_fills(symbol)
        except Exception:
            return None
    epochs: list[int] = []
    for f in fills or []:
        d = str(f.get("dir") or "").lower()
        if "open" not in d or want not in d:
            continue
        sp = f.get("start_position")
        try:
            spf = float(sp)
        except (TypeError, ValueError):
            continue  # unknown start_position → can't confirm a flat→open epoch
        t = int(f.get("time") or 0)
        if abs(spf) < 1e-12 and t > 0:  # position was FLAT before this fill
            epochs.append(t)
    return max(epochs) if epochs else None


async def _open_signature(
    client: Any, pos: Any, symbol: str, side: str, *, fills: Any = _UNSET
) -> int | None:
    """A STABLE exchange reopen-signature, or None (INCONCLUSIVE).

    F2: detects a close+reopen of the same (symbol, side) at ~the same entry
    that the entry-deviation check can't catch. HL (identified by the
    ``place_stop_order`` gate the monitor uses everywhere) derives a trade-epoch
    from the newest Flat→Open fill; every other client (MEXC included — it DOES
    have ``user_fills``, but a per-trade ``positionId`` in the snapshot is the
    stabler anchor) uses that positionId. None on either side of a comparison is
    treated as inconclusive by the repo (never a reset). Never raises.
    """
    if hasattr(client, "place_stop_order"):
        return await _hl_epoch_signature(client, symbol, side, fills=fills)
    return _position_id_signature(pos)


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


async def _current_sl(
    client: Any, symbol: str, side: str, entry: float
) -> tuple[float | None, bool]:
    """Current protective stop + a read-OK flag.

    Returns ``(sl, True)`` on a successful read (``sl`` may be None = genuinely
    no stop), or ``(None, False)`` when the lookup RAISED. Callers MUST NOT treat
    a failed read as "no stop": with `_is_more_protective(current_sl=None)`
    returning True, a transient `open_stop_orders` hiccup would otherwise let the
    monitor move a well-trailed stop DOWN to break-even (F1). On a failed read
    the monitor skips the move entirely; the baseline just gets r1=0 (no auto-BE).
    """
    try:
        stops = await client.open_stop_orders(symbol)
        sl, _tp = classify_protection(stops, side=side, entry=entry)
        return sl, True
    except Exception:
        return None, False


async def ensure_baseline(
    db: Any,
    client: Any,
    symbol: str,
    side: str,
    entry: float,
    now_ms: int,
    *,
    pos: Any = None,
    current_sl: Any = _UNSET,
    fills: Any = _UNSET,
) -> None:
    """Create (first sighting / arm time) or refresh the durable baseline for
    ONE open position — the SINGLE baseline path shared by the monitor cycle and
    the ``POST /api/positions/arm`` endpoint (spec §4).

    Reads the current protective stop, computes ``r1 = |entry - initial_sl|``
    (0.0 when no SL is known → evaluate_rules treats it as "no R signal"), the
    best-effort opened_at and thesis-invalidation, then upserts. The repo FREEZES
    entry_snap/initial_sl_snap/r1 on a non-deviated re-sighting (so "+1R" stays
    measured from the ORIGINAL risk even after the stop later moves) and resets
    to a fresh baseline when the entry deviated beyond tolerance. NEVER places an
    order or moves a stop — it only writes the mgmt DB record.

    Finding 3: ``current_sl`` and ``fills`` may be passed in by the monitor cycle,
    which already read them for this position — avoiding a second
    ``open_stop_orders`` read and a second ``user_fills`` read per cycle. Either
    ``_UNSET`` ⇒ read it here (the arm endpoint path is unchanged). A supplied
    ``current_sl`` of None is honoured as "no stop" (not re-read).
    """
    if current_sl is _UNSET:
        current_sl, _sl_ok = await _current_sl(client, symbol, side, entry)
    # ONE user_fills read shared by opened_at + the reopen signature below.
    if fills is _UNSET:
        fills = await _safe_user_fills(client, symbol)
    # r1 is fixed at baseline time. With no known SL it can't be computed → 0.0,
    # which evaluate_rules reads as "no R signal" (no auto-BE / no time-stop-R
    # gate) — safe until a real stop exists.
    r1 = abs(entry - current_sl) if current_sl is not None else 0.0
    opened_at = await _best_effort_opened_at(
        client, db, symbol, side, now_ms, fills=fills
    )
    invalidation = await _best_effort_invalidation(db, symbol)
    # F2: stable exchange reopen-signature (HL trade-epoch fill / MEXC positionId;
    # None = inconclusive → no reset).
    open_sig = await _open_signature(client, pos, symbol, side, fills=fills)

    # Durable baseline. First sighting inserts; a later sighting refreshes in
    # place (arming/be_done/alert-state preserved), unless entry deviated beyond
    # tolerance → treated as a NEW position by the repo (reset). A changed
    # open_sig on a same-entry re-sighting also forces a reopen reset (F2). §4/§5.
    await db.upsert_position_mgmt(
        symbol,
        side,
        entry_snap=entry,
        initial_sl_snap=current_sl if current_sl is not None else entry,
        r1=r1,
        opened_at=opened_at,
        invalidation_price=invalidation,
        open_sig=open_sig,
    )
    # F1: heal a frozen r1==0 (position first seen without a bracket SL) now that
    # a real protective stop exists. Never overwrites an already-real r1, so the
    # baseline freeze is preserved; unblocks the auto-BE / trail / time-stop R
    # gates that r_available=(r1>0) would otherwise silently keep shut.
    if current_sl is not None:
        await db.heal_r1(symbol, side, current_sl)


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

    current_sl, sl_ok = await _current_sl(client, symbol, side, entry)

    # Mark price — required to evaluate any rule geometry. No mark → skip (the
    # next cycle retries); never guess.
    try:
        ticker = await client.ticker(symbol)
        mark = float(ticker.last_price) if ticker.last_price else None
    except Exception:
        mark = None
    if mark is None or mark <= 0:
        return

    # Durable baseline via the SHARED helper (identical freeze/reset semantics
    # to the arm endpoint). evaluate_rules below still uses the LIVE current_sl
    # read above for the protection-direction check — the baseline only freezes
    # the ORIGINAL initial_sl/r1.
    # Finding 3: reuse the current_sl already read above (line ~"current_sl, sl_ok")
    # so ensure_baseline does NOT issue a second open_stop_orders read this cycle.
    await ensure_baseline(
        db, client, symbol, side, entry, now_ms, pos=pos, current_sl=current_sl
    )
    # Monotonic Chandelier high-water — its OWN UPDATE (never the freeze path),
    # advanced every cycle BEFORE rule evaluation so the trail always trails the
    # best price seen (long: running high; short: running low). Cheap; harmless
    # for non-trail positions (the value is only READ when auto_trail is armed).
    await db.update_high_water(symbol, side, mark)
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
        high_water=mgmt_row.get("high_water"),
        user_override_hw=mgmt_row.get("user_override_hw"),
    )

    # ATR is fetched ONLY for auto_trail-armed positions (klines cost, spec §3).
    # Fail-safe: any fetch/compute error → atr=None → evaluate_rules emits no
    # trail move this cycle (never trail on an unknown ATR).
    armed = baseline.armed_rules if isinstance(baseline.armed_rules, dict) else {}
    atr = None
    if armed.get("auto_trail"):
        atr = await _atr_for(app, client, settings, symbol, now_ms)

    actions = evaluate_rules(
        side=side,
        entry=entry,
        current_sl=current_sl,
        mark=mark,
        mgmt=baseline,
        now_ms=now_ms,
        settings=settings,
        atr=atr,
    )

    # F1: if the current-SL read FAILED (not "no stop", but a lookup error),
    # never move the stop this cycle — moving on an unknown stop could loosen a
    # well-trailed stop down to break-even. Advisory alarms still fire.
    if not sl_ok:
        actions = [a for a in actions if not isinstance(a, MoveSlToBe)]

    # Working copy of the debounce/feed state; persisted once at the end.
    alert_state = dict(baseline.last_alert_state)
    state_dirty = False

    # Advisory alarms always all fire (they never touch an order).
    for action in actions:
        if isinstance(action, Alert):
            alert_state[action.kind] = {
                "active": True,
                "message": action.message,
                "ts": now_ms,
            }
            state_dirty = True

    # ── Single most-protective stop move (Nice-2) ────────────────────────────
    # evaluate_rules can return BOTH an Auto-BE and an Auto-Trail MoveSlToBe in
    # ONE cycle. Applying them sequentially via modify_stop_loss could NET-LOOSEN
    # the live stop (e.g. BE→118 applied, then Trail→115 applied → 118 drops to
    # 115). RULE: among all MoveSlToBe actions this cycle, execute ONLY the most
    # protective one (long: highest new_sl; short: lowest new_sl); ignore the
    # rest. Each candidate already passed evaluate_rules' _is_more_protective /
    # right-side guards, so the winner is strictly a tightening move.
    #
    # be_done latch: a BE move being ELIGIBLE this cycle (any emitted MoveSlToBe
    # with an "auto-BE" reason) means the resulting stop — the MOST protective of
    # BE and Trail — is at least as protective as break-even. So on a successful
    # move we latch be_done whenever BE was eligible, whether the executed move
    # was the BE one or a (higher) trail. Trailing itself has NO latch: a pure
    # trail move never sets be_done and keeps firing as the high-water advances.
    moves = [a for a in actions if isinstance(a, MoveSlToBe)]
    if moves:
        if side == "long":
            chosen = max(moves, key=lambda m: m.new_sl)
        else:
            chosen = min(moves, key=lambda m: m.new_sl)
        be_eligible = any(str(m.reason).startswith("auto-BE") for m in moves)
        is_be_move = str(chosen.reason).startswith("auto-BE")

        # HL-only gate — identical predicate to modify_stop_loss (§3.6). A non-HL
        # client is surfaced as an informational feed entry, never a direct write.
        if not hasattr(client, "place_stop_order"):
            # Debounced: set the ts ONCE (don't re-toast every poll). The arm
            # endpoint rejects arming auto_be/auto_trail on non-HL, so this is
            # only reachable for a stale armed record under a non-HL config.
            if not alert_state.get("auto_be_unavailable"):
                alert_state["auto_be_unavailable"] = {
                    "active": True,
                    "message": (
                        "Auto-Management (BE/Trail) ist nur auf Hyperliquid "
                        "verfuegbar."
                    ),
                    "ts": now_ms,
                }
                state_dirty = True
        else:
            attempts = _be_attempts(app)
            akey = (symbol, side)
            if attempts.get(akey, 0) >= _BE_MAX_ATTEMPTS:
                # Already failed _BE_MAX_ATTEMPTS times — stop hammering the
                # exchange every cycle (I-1). Sticky halt until the position
                # vanishes / process restarts / user re-arms.
                if not alert_state.get("auto_be_error", {}).get("halted"):
                    alert_state["auto_be_error"] = {
                        "active": True,
                        "halted": True,
                        "message": (
                            f"Auto-Management nach {_BE_MAX_ATTEMPTS} "
                            "Fehlversuchen gestoppt — bitte pruefen / neu "
                            "scharfschalten."
                        ),
                        "ts": now_ms,
                    }
                    state_dirty = True
            else:
                try:
                    result = await service.modify_stop_loss(
                        symbol=symbol, side=side, new_sl=chosen.new_sl
                    )
                except Exception as e:  # noqa: BLE001 — must not abort the cycle
                    n = attempts.get(akey, 0) + 1
                    attempts[akey] = n
                    log.warning(
                        "auto-mgmt modify_stop_loss failed for %s %s (%d/%d): %s",
                        symbol,
                        side,
                        n,
                        _BE_MAX_ATTEMPTS,
                        e,
                    )
                    alert_state["auto_be_error"] = {
                        "active": True,
                        "halted": n >= _BE_MAX_ATTEMPTS,
                        "message": (
                            f"Auto-Management fehlgeschlagen "
                            f"({n}/{_BE_MAX_ATTEMPTS}): {e}"
                        ),
                        "ts": now_ms,
                    }
                    state_dirty = True
                    # Do NOT latch be_done — leave it armed to retry until the cap.
                else:
                    # C2: modify_stop_loss signals SOFT failures via its RETURN
                    # dict, NOT an exception: verified=False on
                    # "modify_sl_unverified_old_kept" / "modify_sl_unknown_old_kept"
                    # means the NEW (BE/trail) stop is NOT confirmed resting and the
                    # exchange still holds the OLD looser/initial stop. A non-raising
                    # call is therefore NOT proof of success — only ``verified is
                    # True`` is. Gate the be_done latch AND the "moved" feed on that
                    # flag; otherwise state+UI would falsely claim BE while the
                    # position rests on the initial stop → stopped out at a LOSS at
                    # entry, and auto-BE would never retry (not mgmt.be_done).
                    verified = (
                        isinstance(result, dict) and result.get("verified") is True
                    )
                    if verified:
                        # Confirmed resting: clear the retry cap (both BE and
                        # repeated trailing reset it) + a visible auto-action feed.
                        attempts.pop(akey, None)
                        if be_eligible:
                            # Resulting stop is >= break-even → latch one-shot BE.
                            await db.mark_be_done(symbol, side)
                        if is_be_move:
                            alert_state["auto_be"] = {
                                "active": True,
                                "reason": chosen.reason,
                                "new_sl": chosen.new_sl,
                                "message": (
                                    f"App hat SL auf BE gezogen ({chosen.reason})."
                                ),
                                "ts": now_ms,
                            }
                        else:
                            alert_state["auto_trail"] = {
                                "active": True,
                                "reason": chosen.reason,
                                "new_sl": chosen.new_sl,
                                "message": "App hat SL nachgezogen (Trail).",
                                "ts": now_ms,
                            }
                        state_dirty = True
                    else:
                        # UNVERIFIED soft failure: new stop NOT confirmed, old stop
                        # still held. Do NOT latch be_done (auto-BE stays eligible
                        # next cycle) and NEVER write a "moved"/"done" feed. Emit an
                        # HONEST feed and count it toward the attempt cap so an
                        # endlessly-unverified move can't hammer the exchange forever
                        # (I-1) — same bound the exception path enforces.
                        n = attempts.get(akey, 0) + 1
                        attempts[akey] = n
                        status = (
                            result.get("status")
                            if isinstance(result, dict)
                            else "unverified"
                        )
                        log.warning(
                            "auto-mgmt modify_stop_loss UNVERIFIED for %s %s "
                            "(%d/%d): %s",
                            symbol,
                            side,
                            n,
                            _BE_MAX_ATTEMPTS,
                            status,
                        )
                        alert_state["auto_be_error"] = {
                            "active": True,
                            "halted": n >= _BE_MAX_ATTEMPTS,
                            "message": (
                                f"Auto-Management: SL-Move NICHT bestaetigt "
                                f"({n}/{_BE_MAX_ATTEMPTS}, {status}) — Retry."
                            ),
                            "ts": now_ms,
                        }
                        state_dirty = True

    if state_dirty:
        await db.set_alert_state(symbol, side, alert_state)


# Auto-BE stops re-attempting after this many consecutive modify failures per
# (symbol, side) — bounds the "hammer the exchange every cycle" case (I-1). The
# counter is in-memory: a process restart or the position vanishing clears it.
_BE_MAX_ATTEMPTS = 3
# A mgmt record is only closed after this many CONSECUTIVE cycles where its
# (symbol, side) is absent from the live snapshot — so a transient empty/partial
# account_snapshot cannot silently wipe a user's arming (I-2).
_CLOSE_GRACE_CYCLES = 2


def _be_attempts(app: Any) -> dict:
    d = getattr(app.state, "tm_be_attempts", None)
    if not isinstance(d, dict):
        d = {}
        app.state.tm_be_attempts = d
    return d


def _absence_counts(app: Any) -> dict:
    d = getattr(app.state, "tm_absence", None)
    if not isinstance(d, dict):
        d = {}
        app.state.tm_absence = d
    return d


def _atr_cache(app: Any) -> dict:
    d = getattr(app.state, "tm_atr_cache", None)
    if not isinstance(d, dict):
        d = {}
        app.state.tm_atr_cache = d
    return d


async def _atr_for(
    app: Any, client: Any, settings: Any, symbol: str, now_ms: int
) -> float | None:
    """Latest Wilder-ATR for ``symbol`` on the trail TF — fail-safe + cached.

    Called ONLY for positions that have ``auto_trail`` armed (klines cost).
    Any error (klines fetch, decode, too-few candles) yields ``None`` so the
    caller NEVER trails on an unknown/bad ATR (spec §3/§4). A short in-memory
    cache keyed by (symbol, tf) with a ~one-cycle TTL means two armed positions
    on the same symbol/tf — and re-entry into the same cycle — never double-fetch.
    """
    tf = settings.tm_trail_atr_tf
    period = settings.tm_trail_atr_period
    cache = _atr_cache(app)
    key = (symbol, tf)
    try:
        ttl_ms = max(1, int(settings.tm_monitor_interval_s)) * 1000
    except Exception:
        ttl_ms = 20_000
    hit = cache.get(key)
    if isinstance(hit, dict) and (now_ms - int(hit.get("ts", 0))) < ttl_ms:
        return hit.get("atr")

    atr: float | None = None
    try:
        candles = await client.klines(symbol, tf, limit_hint=max(period * 3, 60))
        series = compute_atr(candles, period=period)
        atr = next((v for v in reversed(series) if v is not None), None)
        if atr is not None and not (math.isfinite(atr) and atr > 0):
            atr = None  # never act on a non-finite / non-positive ATR
    except Exception:
        atr = None  # fail-safe: no ATR → no trail move this cycle
    cache[key] = {"atr": atr, "ts": now_ms}
    return atr


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

    absence = _absence_counts(app)
    live_keys: set[tuple[Any, Any]] = set()
    for pos in positions:
        symbol = pos.get("symbol") if isinstance(pos, dict) else None
        side = pos.get("side") if isinstance(pos, dict) else None
        if symbol and side in ("long", "short"):
            key = (symbol, side)
            live_keys.add(key)
            # C3: a position that was ABSENT on a prior cycle (absence>0) and is now
            # back is a POTENTIAL same-price reopen within the close grace — the
            # mgmt record was never closed, so the non-deviated frozen upsert path
            # would let a FRESH position inherit be_done=True + a stale high_water
            # (auto-BE never re-fires → stopped at a LOSS; trail uses a stale
            # extreme). Treat reappearance-after-absence as a reopen: reset the
            # baseline BEFORE processing so the fresh position re-arms cleanly. A
            # normal continuous cycle (NO prior absence) never resets → the
            # transient-glitch tolerance and the monotonic high-water are preserved.
            if absence.pop(key, 0) > 0:
                try:
                    await db.reset_position_mgmt_baseline(symbol, side)
                except Exception:
                    log.warning(
                        "trade monitor: reopen baseline reset failed", exc_info=True
                    )
        try:
            await _process_position(app, db, client, settings, service, pos, now_ms)
        except Exception:
            # One bad position must never abort the sweep (spec §3.7).
            log.warning("trade monitor: position cycle failed", exc_info=True)
            continue

    # Positions no longer live → close their mgmt record (frees the OPEN slot so
    # a later re-open starts a fresh baseline). Only after _CLOSE_GRACE_CYCLES
    # CONSECUTIVE absences (I-2): a transient empty/partial snapshot must not
    # silently wipe a user's arming — a single glitchy cycle is forgiven.
    attempts = _be_attempts(app)
    try:
        for row in await db.list_open_position_mgmt():
            key = (row.get("symbol"), row.get("side"))
            if key in live_keys:
                absence.pop(key, None)
                continue
            n = absence.get(key, 0) + 1
            absence[key] = n
            if n >= _CLOSE_GRACE_CYCLES:
                await db.close_position_mgmt(row.get("symbol"), row.get("side"))
                absence.pop(key, None)
                attempts.pop(key, None)  # a vanished position clears its BE-retry cap
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
