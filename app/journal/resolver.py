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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

# Terminal + non-terminal states.
PENDING = "PENDING"
WIN = "WIN"
LOSS = "LOSS"
EXPIRED = "EXPIRED"
SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Outcome:
    status: str
    resolved_price: float | None = None
    realized_r: float | None = None
    ambiguous: bool = False


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
) -> Outcome:
    """Decide a shadow outcome for one journal entry. Pure, no I/O.

    Scans candles with `time >= created_at` (t0) in chronological order; the
    first candle that produces a terminal state wins. See module docstring for
    the shadow-fill and intrabar-ambiguity limitations.
    """
    if not geometry_ok(direction, entry_price, stop_loss, tp1):
        return Outcome(status=SKIPPED)

    assert direction is not None and entry_price is not None
    assert stop_loss is not None and tp1 is not None

    t0_ms = _parse_iso_ms(created_at)
    reward = abs(tp1 - entry_price)
    risk = abs(entry_price - stop_loss)
    # geometry_ok guarantees risk > 0, but stay defensive.
    realized_win_r = (reward / risk) if risk > 0 else None

    # Chronological scan of candles at/after t0.
    ordered = sorted(
        (c for c in candles if _field(c, "time") >= t0_ms),
        key=lambda c: _field(c, "time"),
    )
    for candle in ordered:
        tp_hit, sl_hit = _touches(direction, candle, stop_loss, tp1)
        if tp_hit and sl_hit:
            # Intrabar ambiguity -> pessimistic LOSS.
            return Outcome(
                status=LOSS, resolved_price=stop_loss, realized_r=-1.0, ambiguous=True
            )
        if tp_hit:
            return Outcome(status=WIN, resolved_price=tp1, realized_r=realized_win_r)
        if sl_hit:
            return Outcome(status=LOSS, resolved_price=stop_loss, realized_r=-1.0)

    # No terminal candle: expire only once the window has elapsed.
    elapsed = (now - _created_dt(created_at)).total_seconds()
    if elapsed >= window_s:
        return Outcome(status=EXPIRED)
    return Outcome(status=PENDING)
