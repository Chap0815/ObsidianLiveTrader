"""Q-06(a): MEXC REST-poll fallback in /ws/market must back off on repeated
ticker errors instead of hammering the exchange at a flat 1s cadence."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect

from app.main import (
    MEXC_POLL_BASE_DELAY_S,
    MEXC_POLL_MAX_DELAY_S,
    _mexc_ping_pong,
    _mexc_poll_fallback,
)


class _FailingClient:
    async def ticker(self, symbol):
        raise RuntimeError("boom")


class _FakeWS:
    """Fake browser socket: raises WebSocketDisconnect once it has received
    ``max_sends`` frames, so the poll loop under test ends deterministically
    without needing real time to pass."""

    def __init__(self, max_sends: int):
        self.sent: list = []
        self.max_sends = max_sends

    async def send_json(self, data):
        self.sent.append(data)
        if len(self.sent) >= self.max_sends:
            raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_mexc_poll_backs_off_on_error():
    ws = _FakeWS(max_sends=5)
    delays: list = []

    async def fake_sleep(d):
        delays.append(d)

    with patch("app.main.asyncio.sleep", new=fake_sleep):
        await _mexc_poll_fallback(ws, _FailingClient(), "BTC")

    # Growing delay per consecutive error, not a flat 1s spam, and capped.
    assert delays == [
        MEXC_POLL_BASE_DELAY_S * 2,
        MEXC_POLL_BASE_DELAY_S * 4,
        MEXC_POLL_BASE_DELAY_S * 8,
        MEXC_POLL_BASE_DELAY_S * 16,
    ]
    assert all(d <= MEXC_POLL_MAX_DELAY_S for d in delays)
    assert all(d["status"] == "error" for d in ws.sent)


@pytest.mark.asyncio
async def test_mexc_poll_resets_delay_after_success():
    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        async def ticker(self, symbol):
            self.calls += 1
            if self.calls in (1, 2):
                raise RuntimeError("boom")

            class _T:
                last_price = 100.0
                timestamp = 123

            return _T()

    ws = _FakeWS(max_sends=4)
    delays: list = []

    async def fake_sleep(d):
        delays.append(d)

    with patch("app.main.asyncio.sleep", new=fake_sleep):
        await _mexc_poll_fallback(ws, _FlakyClient(), "BTC")

    # Two errors grow the delay, then a success resets it back to base.
    assert delays == [
        MEXC_POLL_BASE_DELAY_S * 2,
        MEXC_POLL_BASE_DELAY_S * 4,
        MEXC_POLL_BASE_DELAY_S,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_price", [True, 0, -1, float("nan"), float("inf"), "garbage"]
)
async def test_mexc_poll_rejects_invalid_live_price(bad_price):
    class _Client:
        async def ticker(self, symbol):
            return SimpleNamespace(last_price=bad_price, timestamp=1_700_000_000_000)

    ws = _FakeWS(max_sends=1)

    await _mexc_poll_fallback(ws, _Client(), "BTC")

    assert ws.sent == [
        {
            "type": "status",
            "status": "error",
            "error": "Ticker unavailable — retrying",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_timestamp",
    [True, -1, 1_700_000_000_000.5, 999_999_999_999, 1_800_000_300_001],
)
async def test_mexc_poll_invalid_timestamp_uses_receive_time_fallback(
    monkeypatch, bad_timestamp
):
    class _Client:
        async def ticker(self, symbol):
            return SimpleNamespace(last_price=100.0, timestamp=bad_timestamp)

    monkeypatch.setattr("app.main._time.time", lambda: 1_800_000_000.0)
    ws = _FakeWS(max_sends=1)

    await _mexc_poll_fallback(ws, _Client(), "BTC")

    assert ws.sent == [
        {"type": "mid", "coin": "BTC", "px": 100.0, "time": 0}
    ]


@pytest.mark.asyncio
async def test_mexc_poll_rejects_timestamp_regression():
    timestamps = iter([1_700_000_100_000, 1_700_000_000_000])

    class _Client:
        async def ticker(self, symbol):
            timestamp = next(timestamps)
            return SimpleNamespace(last_price=timestamp / 1_000_000, timestamp=timestamp)

    async def no_wait(_delay):
        return None

    ws = _FakeWS(max_sends=2)
    with patch("app.main.asyncio.sleep", new=no_wait):
        await _mexc_poll_fallback(ws, _Client(), "BTC")

    assert ws.sent == [
        {
            "type": "mid",
            "coin": "BTC",
            "px": 1_700_000.1,
            "time": 1_700_000_100_000,
        },
        {
            "type": "status",
            "status": "error",
            "error": "Ticker unavailable — retrying",
        },
    ]


@pytest.mark.asyncio
async def test_mexc_ping_pong_echoes_client_ping():
    """Task 4/E3-03: the MEXC branch must answer a client app-ping with a
    pong (mirrors hl_proxy._pump_client) so the browser's pong-watchdog does
    not false-trigger a reconnect while on the MEXC exchange."""

    class _RecvFakeWS:
        def __init__(self, frames):
            self.frames = list(frames)
            self.sent: list = []

        async def receive_text(self):
            if not self.frames:
                raise WebSocketDisconnect()
            return self.frames.pop(0)

        async def send_json(self, data):
            self.sent.append(data)

    ws = _RecvFakeWS(
        [
            json.dumps({"type": "ping"}),
            "not valid json",
            json.dumps({"type": "something-else"}),
        ]
    )

    with pytest.raises(WebSocketDisconnect):
        await _mexc_ping_pong(ws)

    # Only the well-formed ping frame produced a pong; garbage/other frames
    # were ignored rather than crashing the loop.
    assert ws.sent == [{"type": "pong"}]


@pytest.mark.asyncio
async def test_mexc_poll_cancellation_settles_both_child_tasks():
    ticker_started = asyncio.Event()
    receive_started = asyncio.Event()
    ticker_cancelled = False
    receive_cancelled = False

    class _BlockingClient:
        async def ticker(self, symbol):
            nonlocal ticker_cancelled
            ticker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ticker_cancelled = True

    class _BlockingWS:
        async def receive_text(self):
            nonlocal receive_cancelled
            receive_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                receive_cancelled = True

        async def send_json(self, data):
            raise AssertionError("no frame expected while ticker is blocked")

    task = asyncio.create_task(
        _mexc_poll_fallback(_BlockingWS(), _BlockingClient(), "BTC")
    )
    await asyncio.wait_for(ticker_started.wait(), timeout=1.0)
    await asyncio.wait_for(receive_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ticker_cancelled is True
    assert receive_cancelled is True
