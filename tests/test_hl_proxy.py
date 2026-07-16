"""Hyperliquid WS proxy — trade message normalization (F-21) and client-frame
tolerance (O-10/Q-06(b))."""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.realtime.hl_proxy import _normalize_hl, _pump_client


def test_trade_message_wrong_coin_is_skipped_not_forwarded():
    """F-21: a trade for a different coin must be skipped (continue), not
    fall through and be forwarded as the requested coin's trade."""
    msg = {
        "channel": "trades",
        "data": [
            {"coin": "BTC", "px": "60000.0", "sz": "0.5", "side": "S", "time": 1000},
            {"coin": "ETH", "px": "3000.0", "sz": "1.0", "side": "B", "time": 2000},
        ],
    }
    out = _normalize_hl(msg, coin="BTC")
    assert out is not None
    assert out["coin"] == "BTC"
    # Must reflect the real BTC trade, NOT the later foreign ETH one.
    assert out["px"] == 60000.0
    assert len(out["trades"]) == 1
    assert all(t["px"] == 60000.0 for t in out["trades"])


def test_trade_message_all_foreign_coin_yields_no_trades():
    msg = {
        "channel": "trades",
        "data": [
            {"coin": "ETH", "px": "3000.0", "sz": "1.0", "side": "B", "time": 1000},
        ],
    }
    out = _normalize_hl(msg, coin="BTC")
    assert out is None


def test_trade_message_missing_coin_field_still_accepted():
    """Existing behavior: a trade with no coin field at all is accepted
    (the venue sometimes omits it on the subscribed-coin stream)."""
    msg = {
        "channel": "trades",
        "data": [
            {"px": "60000.0", "sz": "0.5", "side": "S", "time": 1000},
        ],
    }
    out = _normalize_hl(msg, coin="BTC")
    assert out is not None
    assert out["px"] == 60000.0


class _FakeClientWS:
    """Minimal stand-in for starlette WebSocket, driven by a scripted frame
    sequence: strings are delivered as-is via receive_text(), exceptions are
    raised."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list = []

    async def receive_text(self):
        item = self._frames.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_hl_proxy_ignores_non_json_frame():
    """O-10/Q-06(b): a non-JSON client frame must be ignored (loop keeps
    reading) instead of raising and tearing down the whole proxy pump."""
    ws = _FakeClientWS(["not-json-garbage-{{{", WebSocketDisconnect()])
    with pytest.raises(WebSocketDisconnect):
        await _pump_client(ws)
    # Both frames were consumed: the garbage one was ignored, not fatal —
    # only the real disconnect ended the loop.
    assert ws._frames == []


@pytest.mark.asyncio
async def test_hl_proxy_pump_client_answers_ping_after_garbage_frame():
    ws = _FakeClientWS(
        ["}}}not json", '{"type": "ping"}', WebSocketDisconnect()]
    )
    with pytest.raises(WebSocketDisconnect):
        await _pump_client(ws)
    assert ws.sent == [{"type": "pong"}]
