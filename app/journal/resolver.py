"""Pure shadow-outcome geometry for the journal feedback-loop.

ADVISORY / MEASUREMENT ONLY. Nothing here places, moves, or cancels an order —
it only decides whether a proposal's tp1 or stop-loss would have been touched
by subsequent price action, to measure the KI's raw call quality.

SHADOW-FILL SIMPLIFICATION (documented so the stats are never misread as real
PnL): the eval assumes the trade filled at EXACTLY entry_price at proposal time
t0, regardless of whether price ever traded back to a limit entry. There are NO
fees, funding, or slippage in realized_r, and only tp1 is tracked (not tp2/tp3,
partial scale-outs, or SL-to-BE management). A +2R shadow win is gross, not net,
and measures the level thesis — not achievable fills.

Intrabar ambiguity: when tp1 and sl fall inside the SAME candle we cannot know
which was hit first, so we resolve PESSIMISTICALLY to LOSS (ambiguous=1). This
slightly UNDERstates the win rate, deliberately, to avoid flattering the KI.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger("app.journal.resolver")

# tf -> seconds. Mirrors app/mexc/client._INTERVAL_SECONDS (kept local so the
# resolver has no import dependency on an exchange module).
_TF_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1H": 3600,
    "4H": 14_400,
    "1D": 86_400,
    "Min5": 300,
    "Min15": 900,
    "Min60": 3600,
    "Hour4": 14_400,
    "Day1": 86_400,
}

# Cap so a tiny tf over a long window never asks for an absurd number of bars.
_MAX_LIMIT_HINT = 1000
_DEFAULT_LIMIT_HINT = 500

# F2-06: the resolution window is floored at the base (24h) but scaled UP on
# higher TFs so a slow winner isn't cut off before it can resolve (which would
# otherwise bias the EXPIRED bucket toward higher-TF setups). 96 bars is ~4
# days on 1H, ~16 days on 4H, ~96 days on 1D.
_WINDOW_TF_BARS = 96

# F2-07: flat cost model for the NET realized R. taker_fee is charged on BOTH
# legs (round-trip = 2x); slippage_frac is a single round-trip estimate. Both
# are fractions of price (0.0006 = 0.06%), NOT the "percent" convention used in
# app/config.py. These are deliberately conservative so net R never flatters.
_DEFAULT_TAKER_FEE = 0.0006
_DEFAULT_SLIPPAGE_FRAC = 0.0005

# Terminal + non-terminal states.
PENDING = "PENDING"
WIN = "WIN"
LOSS = "LOSS"
EXPIRED = "EXPIRED"
SKIPPED = "SKIPPED"
# F2-02: a limit entry whose price was never touched by post-t0 price action —
# the trade never filled, so it is NOT a real WIN/LOSS. Excluded from win-rate,
# counted separately (see app/journal/stats.py).
NO_FILL = "NO_FILL"


@dataclass(frozen=True)
class Outcome:
    status: str
    resolved_price: float | None = None
    realized_r: float | None = None
    ambiguous: bool = False
    realized_r_net: float | None = None


def _parse_iso_ms(created_at: str | datetime) -> int:
    """t0 as epoch milliseconds (candle.time is ms). Naive datetimes/strings
    are treated as UTC."""
    if isinstance(created_at, datetime):
        dt = created_at
    else:
        s = str(created_at).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _created_dt(created_at: str | datetime) -> datetime:
    if isinstance(created_at, datetime):
        dt = created_at
    else:
        s = str(created_at).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _field(candle: Any, name: str) -> float:
    """Read high/low/time from a Candle model OR a plain dict."""
    if isinstance(candle, dict):
        return candle[name]
    return getattr(candle, name)


def geometry_ok(
    direction: str | None,
    entry_price: float | None,
    stop_loss: float | None,
    tp1: float | None,
) -> bool:
    """True only if the levels are internally consistent for the direction.

    long  requires tp1 > entry > sl; short requires tp1 < entry < sl. A
    degenerate proposal (e.g. long with tp1 <= entry) is not shadow-resolvable.
    """
    if direction not in ("long", "short"):
        return False
    if entry_price is None or stop_loss is None or tp1 is None:
        return False
    if direction == "long":
        return tp1 > entry_price and stop_loss < entry_price
    return tp1 < entry_price and stop_loss > entry_price


def _touches(
    direction: str, candle: Any, stop_loss: float, tp1: float
) -> tuple[bool, bool]:
    """(tp1_touched, sl_touched) within this candle's [low, high]."""
    high = _field(candle, "high")
    low = _field(candle, "low")
    if direction == "long":
        return (high >= tp1, low <= stop_loss)
    # short
    return (low <= tp1, high >= stop_loss)


def resolution_window_s(tf: str, base_window_s: float) -> float:
    """F2-06: tf-scaled resolution window. Floored at `base_window_s` (24h) but
    stretched to `_WINDOW_TF_BARS` bars on higher TFs so a slow winner isn't
    prematurely EXPIRED. Unknown/degenerate tf falls back to the base window."""
    tf_s = _TF_SECONDS.get(tf)
    if not tf_s or tf_s <= 0:
        return base_window_s
    return max(base_window_s, float(_WINDOW_TF_BARS * tf_s))


def _net_r(
    gross_r: float | None,
    entry_price: float,
    risk: float,
    taker_fee: float,
    slippage_frac: float,
) -> float | None:
    """F2-07: gross R minus round-trip costs, expressed in R units.

    cost_frac (fraction of price) = taker_fee*2 (both legs) + slippage_frac.
    Converted to R by dividing by the risk fraction (risk/entry_price):
        cost_r = cost_frac * entry_price / risk.
    Costs are a drag in BOTH directions, so they always subtract (a loss gets
    MORE negative, a win smaller). None gross (EXPIRED/NO_FILL/SKIPPED) -> None.
    """
    if gross_r is None or risk <= 0 or not entry_price:
        return gross_r
    cost_frac = 2.0 * taker_fee + slippage_frac
    cost_r = cost_frac * (entry_price / risk)
    return gross_r - cost_r


def _entry_fill_index(
    ordered: list[Any], entry_price: float, order_type: str | None
) -> int | None:
    """F2-02: index into `ordered` (candles at/after t0) of the first bar that
    would fill the entry, or None if it was never touched.

    - market order: fills immediately at the first available bar (index 0).
    - limit order (default): fills on the first bar whose [low, high] straddles
      entry_price, i.e. price actually traded through the limit.
    """
    if not ordered:
        return None
    if order_type == "market":
        return 0
    for i, candle in enumerate(ordered):
        if _field(candle, "low") <= entry_price <= _field(candle, "high"):
            return i
    return None


def resolve_entry(
    *,
    direction: str | None,
    entry_price: float | None,
    stop_loss: float | None,
    tp1: float | None,
    created_at: str | datetime,
    candles: Iterable[Any],
    now: datetime,
    window_s: float,
    order_type: str | None = None,
    taker_fee: float = _DEFAULT_TAKER_FEE,
    slippage_frac: float = _DEFAULT_SLIPPAGE_FRAC,
) -> Outcome:
    """Decide a shadow outcome for one journal entry. Pure, no I/O.

    First locates the ENTRY-fill bar (F2-02): TP/SL are only scanned from the
    bar the entry was actually touched onward. A limit that price never traded
    back to resolves to NO_FILL (never a fictional WIN/LOSS). From the fill bar
    on, the first candle that produces a terminal state wins. See module
    docstring for the shadow-fill and intrabar-ambiguity limitations.
    """
    if not geometry_ok(direction, entry_price, stop_loss, tp1):
        return Outcome(status=SKIPPED)

    assert direction is not None and entry_price is not None
    assert stop_loss is not None and tp1 is not None

    # Materialize once: `candles` may be a one-shot generator, and we need to
    # scan it twice below (touch-scan, then coverage check).
    all_candles = list(candles)

    t0_ms = _parse_iso_ms(created_at)
    reward = abs(tp1 - entry_price)
    risk = abs(entry_price - stop_loss)
    # geometry_ok guarantees risk > 0, but stay defensive.
    realized_win_r = (reward / risk) if risk > 0 else None

    # Chronological scan of candles at/after t0.
    ordered = sorted(
        (c for c in all_candles if _field(c, "time") >= t0_ms),
        key=lambda c: _field(c, "time"),
    )

    # F2-02: find the fill bar; only scan TP/SL from there. Intrabar ambiguity
    # extends to the fill bar itself (entry + SL in the same candle -> LOSS).
    fill_index = _entry_fill_index(ordered, entry_price, order_type)
    if fill_index is not None:
        for candle in ordered[fill_index:]:
            tp_hit, sl_hit = _touches(direction, candle, stop_loss, tp1)
            if tp_hit and sl_hit:
                # Intrabar ambiguity -> pessimistic LOSS.
                return Outcome(
                    status=LOSS,
                    resolved_price=stop_loss,
                    realized_r=-1.0,
                    ambiguous=True,
                    realized_r_net=_net_r(
                        -1.0, entry_price, risk, taker_fee, slippage_frac
                    ),
                )
            if tp_hit:
                return Outcome(
                    status=WIN,
                    resolved_price=tp1,
                    realized_r=realized_win_r,
                    realized_r_net=_net_r(
                        realized_win_r, entry_price, risk, taker_fee, slippage_frac
                    ),
                )
            if sl_hit:
                return Outcome(
                    status=LOSS,
                    resolved_price=stop_loss,
                    realized_r=-1.0,
                    realized_r_net=_net_r(
                        -1.0, entry_price, risk, taker_fee, slippage_frac
                    ),
                )

    # No terminal candle found. Expiring / NO_FILL is only safe once (a) the
    # window has genuinely elapsed AND (b) the fetched candle history actually
    # reaches back to t0 -- otherwise a real fill / WIN / LOSS could be hiding
    # before our earliest fetched candle, and reporting a terminal state would
    # silently discard it. Coverage-less/incomplete data (resolver was down
    # longer than the fetch window, or an empty/delisted payload) always stays
    # PENDING so the next cycle (with a wider/fresh fetch) gets another chance.
    elapsed = (now - _created_dt(created_at)).total_seconds()
    if elapsed >= window_s:
        candle_times = [_field(c, "time") for c in all_candles]
        earliest = min(candle_times) if candle_times else None
        covers_t0 = earliest is not None and earliest <= t0_ms
        if covers_t0:
            # Entry never touched over a fully-covered, elapsed window -> the
            # limit never filled. A filled-but-untouched-TP/SL entry -> EXPIRED.
            return Outcome(status=NO_FILL if fill_index is None else EXPIRED)
    return Outcome(status=PENDING)


def _limit_hint_for(tf: str, window_s: float) -> int:
    """Bars needed to cover the resolution window at this tf, plus buffer."""
    tf_s = _TF_SECONDS.get(tf)
    if not tf_s or tf_s <= 0:
        return _DEFAULT_LIMIT_HINT
    n = math.ceil(window_s / tf_s) + 5
    return max(10, min(_MAX_LIMIT_HINT, n))


def _stale_horizon_s(tf: str) -> float | None:
    """Defect E: the oldest t0 a now-anchored, _MAX_LIMIT_HINT-capped kline
    fetch can EVER reach back to, in seconds. `client.klines()` always
    fetches the most recent N bars from `now` (never anchored to a row's t0),
    so once a PENDING row's age exceeds `_MAX_LIMIT_HINT * tf_s`, the earliest
    candle any future fetch returns can never cover t0 again -- `covers_t0`
    in resolve_entry is permanently False and the row would stay PENDING
    forever. Returns None for an unknown/degenerate tf (no seconds mapping,
    so no horizon can be computed -- existing behavior is left untouched)."""
    tf_s = _TF_SECONDS.get(tf)
    if not tf_s or tf_s <= 0:
        return None
    return float(_MAX_LIMIT_HINT * tf_s)


async def resolve_pending_once(
    db: Any,
    client: Any,
    *,
    window_s: float,
    now: datetime | None = None,
) -> None:
    """One resolver pass over all PENDING journal rows. Fail-safe by design.

    Groups rows by (symbol, tf), fetches klines once per group, applies the
    pure resolve_entry, and writes terminal outcomes. Any per-symbol error
    leaves those rows PENDING for the next cycle; a DB-read failure makes the
    whole pass a no-op. Never raises (except CancelledError propagates).
    """
    now = now or datetime.now(timezone.utc)
    try:
        rows = await db.pending_journal_entries()
    except asyncio.CancelledError:
        raise
    except Exception:
        # DB unavailable -> no-op cycle (same soft-fail posture as analyze).
        return

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows or []:
        groups.setdefault((row["symbol"], row["tf"]), []).append(row)

    for (symbol, tf), grp in groups.items():
        # F2-06: this group's window is the tf-scaled one (floored at window_s),
        # used both for the fetch sizing and the EXPIRED/NO_FILL gate below.
        eff_window_s = resolution_window_s(tf, window_s)
        try:
            # Size the fetch to cover from the OLDEST row's t0 to now, not
            # just `eff_window_s` back from now. If the resolver was down longer
            # than the window (or a row is older than the window on its first
            # resolve), a window-only fetch would never reach t0 and the
            # coverage guard in resolve_entry would (correctly) leave it
            # PENDING forever. Still capped at _MAX_LIMIT_HINT bars.
            max_elapsed_s = eff_window_s
            for row in grp:
                try:
                    elapsed_row = (now - _created_dt(row["created_at"])).total_seconds()
                    if elapsed_row > max_elapsed_s:
                        max_elapsed_s = elapsed_row
                except Exception:
                    continue
            limit_hint = _limit_hint_for(tf, max_elapsed_s)
            candles = await client.klines(symbol, tf, limit_hint)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Exchange down / bad payload: leave rows PENDING, retry next cycle.
            log.warning("journal resolver: klines failed for %s %s", symbol, tf)
            for row in grp:
                try:
                    await db.touch_journal_checked(row["id"])
                except Exception:
                    pass
            continue

        stale_horizon_s = _stale_horizon_s(tf)
        for row in grp:
            try:
                outcome = resolve_entry(
                    direction=row.get("direction"),
                    entry_price=row.get("entry_price"),
                    stop_loss=row.get("stop_loss"),
                    tp1=row.get("tp1"),
                    created_at=row["created_at"],
                    candles=candles,
                    now=now,
                    window_s=eff_window_s,
                    order_type=row.get("order_type"),
                )
                # Defect E fix (b): a row this far stale can NEVER regain
                # coverage back to t0 from a now-anchored, capped fetch -- the
                # pure resolve_entry would (correctly, per its own contract)
                # leave it PENDING forever. Force it to a terminal, non-WIN/
                # LOSS status (reusing EXPIRED -- existing stats queries
                # already filter on status IN ('WIN','LOSS') and a dedicated
                # `expired` count, so this is excluded from win-rate/sum_r and
                # surfaced honestly, not fabricated as a win or loss) so it
                # exits the pending bucket and stops being re-fetched forever.
                if (
                    outcome.status == PENDING
                    and stale_horizon_s is not None
                ):
                    try:
                        elapsed_row = (
                            now - _created_dt(row["created_at"])
                        ).total_seconds()
                    except Exception:
                        elapsed_row = 0.0
                    if elapsed_row > stale_horizon_s:
                        outcome = Outcome(status=EXPIRED)
                if outcome.status != PENDING:
                    await db.update_journal_outcome(
                        row["id"],
                        status=outcome.status,
                        resolved_price=outcome.resolved_price,
                        realized_r=outcome.realized_r,
                        ambiguous=outcome.ambiguous,
                        realized_r_net=outcome.realized_r_net,
                    )
                else:
                    await db.touch_journal_checked(row["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                # A single bad row must not abort the pass.
                continue


async def run_resolver_loop(app: Any) -> None:
    """Background task: sweep PENDING journal rows on an interval.

    Started in lifespan(), cancelled+awaited on shutdown. Independent of the UI
    so proposals for coins the user isn't watching still resolve. NEVER crashes
    the app: every cycle body is wrapped, and it sleeps regardless of outcome.
    Relies on the single-worker invariant so exactly one resolver runs.
    """
    from app.config import get_settings

    try:
        s = get_settings()
        interval = max(5, int(getattr(s, "journal_resolve_interval_s", 60)))
        window_s = float(getattr(s, "journal_window_hours", 24)) * 3600.0
    except Exception:
        interval = 60
        window_s = 24 * 3600.0

    while True:
        try:
            db = getattr(app.state, "db", None)
            client = getattr(app.state, "mexc", None) or getattr(
                app.state, "exchange", None
            )
            if db is not None and client is not None:
                await resolve_pending_once(db, client, window_s=window_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("journal resolver cycle failed", exc_info=True)
        await asyncio.sleep(interval)
