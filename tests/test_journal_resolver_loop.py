"""Task 4: single-pass resolver + fail-safe + clean cancellation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db.repo import Database
from app.journal.resolver import resolve_pending_once, run_resolver_loop

T0 = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
T0_ISO = T0.isoformat()
T0_MS = int(T0.timestamp() * 1000)
WINDOW = 24 * 3600


def _candle(offset_min, high, low):
    return {"time": T0_MS + offset_min * 60_000, "high": high, "low": low}


class FakeClient:
    def __init__(self, candles_by_symbol):
        self.candles_by_symbol = candles_by_symbol
        self.calls = []

    async def klines(self, symbol, interval, limit_hint=200):
        self.calls.append((symbol, interval, limit_hint))
        return self.candles_by_symbol[symbol]


class RaisingClient:
    async def klines(self, symbol, interval, limit_hint=200):
        raise RuntimeError("exchange down")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "resolver.db")


async def _seed_long(db, **over):
    kw = dict(
        symbol="BTC_USDT", tf="15m", htf="1H", action="BUY", direction="long",
        setup_confidence="high", entry_price=100.0, stop_loss=99.0, tp1=102.0,
        rrr=2.0, provider="claude", model="m", scanner_summary=None,
        last_price_t0=100.0, created_at=T0_ISO,
    )
    kw.update(over)
    return await db.insert_journal_entry(**kw)


@pytest.mark.asyncio
async def test_single_pass_resolves_win_and_loss(db_path):
    db = Database(db_path)
    await db.init()
    win_id = await _seed_long(db, symbol="BTC_USDT")
    loss_id = await _seed_long(db, symbol="ETH_USDT")
    client = FakeClient({
        "BTC_USDT": [_candle(10, 102.5, 100.0)],   # hits tp1
        "ETH_USDT": [_candle(10, 100.2, 98.5)],    # hits sl
    })
    now = T0 + timedelta(hours=1)
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)

    rows = {r["id"]: r for r in await db.recent_journal()}
    assert rows[win_id]["status"] == "WIN"
    assert rows[win_id]["realized_r"] == pytest.approx(2.0)
    assert rows[loss_id]["status"] == "LOSS"
    assert rows[loss_id]["realized_r"] == -1.0


@pytest.mark.asyncio
async def test_single_pass_expires_after_window(db_path):
    db = Database(db_path)
    await db.init()
    jid = await _seed_long(db)
    # A candle at t0 itself gives full coverage back to created_at.
    client = FakeClient({"BTC_USDT": [_candle(0, 100.5, 99.5), _candle(10, 100.5, 99.5)]})
    now = T0 + timedelta(hours=48)  # past window
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    rows = await db.recent_journal()
    assert rows[0]["status"] == "EXPIRED"


@pytest.mark.asyncio
async def test_single_pass_leaves_pending_when_fetch_misses_t0(db_path):
    # Regression: a row is much older than window_s (resolver was down a long
    # time / row was already stale on its first resolve). The klines fetch
    # from the exchange only ever returns recent candles that don't reach
    # back to t0 (simulating a fetch sized only to `window_s`). Even though
    # the window has elapsed and there's no touch in what we got, this must
    # NOT be silently classified EXPIRED -- a real WIN/LOSS could be hiding
    # before the earliest fetched candle.
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    now = T0 + timedelta(hours=200)  # far past window_s (24h)
    # Candles start ~190h after t0 -- well short of covering all the way back.
    client = FakeClient({
        "BTC_USDT": [_candle(190 * 60, 100.5, 99.5), _candle(195 * 60, 100.5, 99.5)]
    })
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_single_pass_sizes_fetch_to_reach_t0(db_path):
    # The resolver must size its kline request to cover from the row's t0 to
    # now, not just `window_s` back -- otherwise a stale row can never regain
    # coverage. Assert the FakeClient was asked for a wider limit_hint than a
    # window_s-only sizing would produce (15m bars, window_s=24h -> baseline
    # limit_hint is small; row is 100h old, needing far more bars).
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    now = T0 + timedelta(hours=100)
    client = FakeClient({"BTC_USDT": [_candle(0, 100.5, 99.5)]})
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    assert len(client.calls) == 1
    _, _, limit_hint = client.calls[0]
    # window_s(24h)-only sizing at 15m bars would be ceil(24h/15m)+5 = 101.
    # Covering the row's real 100h age needs ceil(100h/15m)+5 = 405.
    assert limit_hint > 101


@pytest.mark.asyncio
async def test_far_stale_pending_row_forced_terminal_not_stuck_forever(db_path):
    """Defect E regression: once a row's age exceeds the horizon a
    now-anchored, _MAX_LIMIT_HINT-capped kline fetch can ever reach back to
    (1000 bars * 900s for 15m = ~250h), the row must NOT stay PENDING
    forever -- it must be forced to a terminal, non-WIN/LOSS status so it
    stops rotting the P2 calibration sample and stops being re-fetched every
    cycle indefinitely."""
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    now = T0 + timedelta(hours=260)  # past the ~250h horizon for 15m/1000 bars
    # Simulates a real now-anchored fetch: candles are recent, nowhere near t0,
    # and never touch tp1/sl -- exactly what keeps the row PENDING forever
    # today.
    client = FakeClient({
        "BTC_USDT": [_candle(255 * 60, 100.5, 99.5), _candle(259 * 60, 100.5, 99.5)]
    })
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    rows = await db.recent_journal()
    assert rows[0]["status"] != "PENDING"
    assert rows[0]["status"] not in ("WIN", "LOSS")

    stats = await db.journal_stats()
    assert stats["wins"] == 0
    assert stats["losses"] == 0


@pytest.mark.asyncio
async def test_stale_row_within_horizon_still_untouched(db_path):
    """A row older than window_s but still within the _MAX_LIMIT_HINT horizon
    must be left exactly as before (PENDING, retried next cycle) -- the new
    hard ceiling must not fire early."""
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    now = T0 + timedelta(hours=200)  # within ~250h horizon for 15m
    client = FakeClient({
        "BTC_USDT": [_candle(190 * 60, 100.5, 99.5), _candle(195 * 60, 100.5, 99.5)]
    })
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_pending_stays_when_window_open(db_path):
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    client = FakeClient({"BTC_USDT": [_candle(10, 100.5, 99.5)]})
    now = T0 + timedelta(hours=1)
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_empty_klines_payload_never_fabricates_outcome(db_path):
    """client.klines(...) returning an empty list (e.g. delisted symbol /
    empty payload) must never produce a fabricated WIN/LOSS/EXPIRED -- the
    row stays PENDING both within the window and past it (no coverage of t0
    is ever possible with zero candles)."""
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    client = FakeClient({"BTC_USDT": []})

    # Within window: PENDING (unsurprising).
    await resolve_pending_once(db, client, window_s=WINDOW, now=T0 + timedelta(hours=1))
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"

    # Past window, still empty payload: must stay PENDING, never EXPIRED.
    await resolve_pending_once(db, client, window_s=WINDOW, now=T0 + timedelta(hours=48))
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_raising_client_leaves_rows_pending(db_path):
    db = Database(db_path)
    await db.init()
    await _seed_long(db)
    # Must not raise; row stays PENDING.
    await resolve_pending_once(db, RaisingClient(), window_s=WINDOW, now=T0 + timedelta(hours=1))
    rows = await db.recent_journal()
    assert rows[0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_loop_can_be_cancelled_cleanly(db_path):
    db = Database(db_path)
    await db.init()
    await _seed_long(db)

    class FakeApp:
        class state:
            pass

    app = FakeApp()
    app.state.db = db
    app.state.mexc = FakeClient({"BTC_USDT": [_candle(10, 102.5, 100.0)]})

    task = asyncio.create_task(run_resolver_loop(app))
    await asyncio.sleep(0.05)  # let one cycle run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # the cycle that ran should have resolved the row to WIN
    rows = await db.recent_journal()
    assert rows[0]["status"] == "WIN"
