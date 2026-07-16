"""Q-06(a): MEXC REST-poll fallback in /ws/market must back off on repeated
ticker errors instead of hammering the exchange at a flat 1s cadence."""

from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect

from app.main import (
    MEXC_POLL_BASE_DELAY_S,
    MEXC_POLL_MAX_DELAY_S,
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
