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
    client = FakeClient({"BTC_USDT": [_candle(10, 100.5, 99.5)]})  # never touches
    now = T0 + timedelta(hours=48)  # past window
    await resolve_pending_once(db, client, window_s=WINDOW, now=now)
    rows = await db.recent_journal()
    assert rows[0]["status"] == "EXPIRED"


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
