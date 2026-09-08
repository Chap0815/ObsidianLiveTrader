"""Hyperliquid WS proxy — trade message normalization (F-21), client-frame
tolerance (O-10/Q-06(b)) and upstream reconnect/watchdog/heartbeat
(E3-02/E3-03)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app.realtime import hl_proxy
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


@pytest.mark.parametrize("invalid_coin", [True, False, 77])
def test_trade_rejects_nonstring_coin_matching_subscription_text(invalid_coin):
    msg = {
        "channel": "trades",
        "data": [
            {
                "coin": invalid_coin,
                "px": "60000.0",
                "sz": "0.5",
                "time": 1000,
            }
        ],
    }

    assert _normalize_hl(msg, coin=str(invalid_coin).upper()) is None


@pytest.mark.parametrize("payload", [None, [], "valid-json-scalar", 7, True])
def test_normalizer_ignores_non_object_json_frames(payload):
    """A syntactically valid but non-object upstream frame is malformed data,
    not a reason to tear down an otherwise healthy subscription."""
    assert _normalize_hl(payload, coin="BTC") is None


def test_trade_message_skips_non_object_rows():
    msg = {
        "channel": "trades",
        "data": [
            "malformed-row",
            {"coin": "BTC", "px": "60000.0", "sz": "0.5", "side": "S", "time": 1000},
        ],
    }

    out = _normalize_hl(msg, coin="BTC")

    assert out is not None
    assert out["px"] == 60000.0
    assert len(out["trades"]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"channel": "trades", "data": [{"px": True, "time": 1000}]},
        {
            "channel": "candle",
            "data": {"t": 1000, "o": "1", "h": "NaN", "l": "1", "c": "1"},
        },
        {
            "channel": "bbo",
            "data": {"time": 1000, "bbo": [{"px": "-1"}, {"px": "-1"}]},
        },
        {"channel": "allMids", "data": {"mids": {"BTC": "0"}}},
    ],
)
def test_normalizer_rejects_invalid_live_prices(payload):
    assert _normalize_hl(payload, coin="BTC") is None


@pytest.mark.parametrize(
    "msg",
    [
        {
            "channel": "candle",
            "data": {
                "t": 1000,
                "o": "3000",
                "h": "3010",
                "l": "2990",
                "c": "3005",
                "s": "ETH",
            },
        },
        {
            "channel": "bbo",
            "data": {
                "coin": "ETH",
                "time": 1000,
                "bbo": [{"px": "3000"}, {"px": "3001"}],
            },
        },
    ],
)
def test_price_message_wrong_coin_is_skipped(msg):
    assert _normalize_hl(msg, coin="BTC") is None


@pytest.mark.parametrize("invalid_coin", [False, 0, [], {}])
def test_candle_rejects_falsy_nonstring_coin_identity(invalid_coin):
    msg = {
        "channel": "candle",
        "data": {
            "t": 1000,
            "o": "60000",
            "h": "60010",
            "l": "59990",
            "c": "60000",
            "s": invalid_coin,
        },
    }

    assert _normalize_hl(msg, coin="BTC") is None


@pytest.mark.parametrize("invalid_coin", [False, 0, [], {}])
def test_bbo_rejects_falsy_nonstring_coin_identity(invalid_coin):
    msg = {
        "channel": "bbo",
        "data": {
            "coin": invalid_coin,
            "time": 1000,
            "bbo": [{"px": "59999"}, {"px": "60001"}],
        },
    }

    assert _normalize_hl(msg, coin="BTC") is None


@pytest.mark.parametrize(
    ("payload", "expected_type", "expected_price"),
    [
        (
            {
                "channel": "trades",
                "data": [{"coin": "BTC", "px": "60000", "sz": "0.1", "time": 1000}],
            },
            "trade",
            60000.0,
        ),
        (
            {
                "channel": "candle",
                "data": {
                    "t": 1000,
                    "o": "59990",
                    "h": "60010",
                    "l": "59980",
                    "c": "60000",
                    "v": "2",
                    "s": "BTC",
                },
            },
            "candle",
            60000.0,
        ),
        (
            {
                "channel": "bbo",
                "data": {
                    "coin": "BTC",
                    "time": 1000,
                    "bbo": [{"px": "59999"}, {"px": "60001"}],
                },
            },
            "mid",
            60000.0,
        ),
        (
            {"channel": "allMids", "data": {"mids": {"BTC": "60000"}}},
            "mid",
            60000.0,
        ),
    ],
)
def test_normalizer_keeps_valid_price_channels(payload, expected_type, expected_price):
    out = _normalize_hl(payload, coin="BTC")

    assert out is not None
    assert out["type"] == expected_type
    actual_price = out["bar"]["close"] if expected_type == "candle" else out["px"]
    assert actual_price == expected_price


def test_bbo_invalid_timestamp_keeps_valid_price_with_receive_time_fallback():
    msg = {
        "channel": "bbo",
        "data": {
            "coin": "BTC",
            "time": "not-a-timestamp",
            "bbo": [{"px": "59999"}, {"px": "60001"}],
        },
    }

    out = _normalize_hl(msg, coin="BTC")

    assert out is not None
    assert out["px"] == 60000.0
    assert out["time"] == 0


def test_bbo_non_object_levels_are_ignored():
    msg = {
        "channel": "bbo",
        "data": {"coin": "BTC", "time": 1000, "bbo": ["bad-bid", "bad-ask"]},
    }

    assert _normalize_hl(msg, coin="BTC") is None


@pytest.mark.parametrize("channel", ["trades", "candle"])
def test_negative_realtime_timestamp_uses_receive_time_fallback(channel):
    if channel == "trades":
        msg = {
            "channel": channel,
            "data": [{"coin": "BTC", "px": "60000", "sz": "0.1", "time": -1}],
        }
    else:
        msg = {
            "channel": channel,
            "data": {
                "t": -1,
                "o": "59990",
                "h": "60010",
                "l": "59980",
                "c": "60000",
                "s": "BTC",
            },
        }

    out = _normalize_hl(msg, coin="BTC")

    assert out is not None
    timestamp = out["bar"]["time_ms"] if channel == "candle" else out["time"]
    assert timestamp == 0


@pytest.mark.parametrize(("field", "value"), [("h", "59989"), ("l", "60001")])
def test_realtime_candle_rejects_impossible_ohlc_geometry(field, value):
    candle = {
        "t": 1000,
        "o": "59990",
        "h": "60010",
        "l": "59980",
        "c": "60000",
        "s": "BTC",
    }
    candle[field] = value

    assert _normalize_hl({"channel": "candle", "data": candle}, coin="BTC") is None


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


# --- E3-02/E3-03: reconnect loop + idle watchdog + HL heartbeat -------------


class _FakeUpstream:
    """Stand-in for a websockets connection usable as an async context
    manager. ``recv_items`` is a scripted list (str → returned, exception →
    raised); once exhausted ``recv`` blocks forever, simulating a silent
    upstream. ``on_send`` fires for every frame the proxy sends upstream and
    ``on_enter`` when the connection is entered."""

    def __init__(self, recv_items=None, on_send=None, on_enter=None):
        self._recv_items = list(recv_items or [])
        self.sent: list = []
        self._on_send = on_send
        self._on_enter = on_enter
        self._blocked = asyncio.Event()  # never set → recv() blocks

    async def __aenter__(self):
        if self._on_enter is not None:
            self._on_enter()
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self.sent.append(data)
        if self._on_send is not None:
            self._on_send(self, data)

    async def recv(self):
        if self._recv_items:
            item = self._recv_items.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        await self._blocked.wait()  # silent upstream
        raise AssertionError("unreachable")


class _FakeClientForProxy:
    """Client WS whose control-read blocks until ``release()`` is called,
    after which it raises WebSocketDisconnect (browser gone)."""

    def __init__(self):
        self.sent: list = []
        self._release = asyncio.Event()

    def release(self):
        self._release.set()

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_text(self):
        await self._release.wait()
        raise WebSocketDisconnect()


def _patch_connect(monkeypatch, upstreams):
    """Patch websockets.connect to hand out the given upstreams in order;
    extra reconnects get a permanently-blocking upstream. Returns the call
    list so the test can count reconnect attempts."""
    calls: list = []
    it = iter(upstreams)

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        try:
            return next(it)
        except StopIteration:
            return _FakeUpstream()

    monkeypatch.setattr(hl_proxy.websockets, "connect", fake_connect)
    return calls


def _settings():
    return SimpleNamespace(hl_base_url=None, hl_testnet=True)


@pytest.mark.asyncio
async def test_proxy_reconnects_after_clean_upstream_close(monkeypatch):
    """E3-02: a clean upstream close must NOT tear down the browser session —
    the proxy reconnects (and emits a `degraded` status) while the client
    stays connected."""
    client = _FakeClientForProxy()
    up1 = _FakeUpstream(recv_items=[ConnectionError("upstream closed cleanly")])
    up2 = _FakeUpstream(on_enter=client.release)  # 2nd connect → let client go
    calls = _patch_connect(monkeypatch, [up1, up2])
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_START", 0.01)

    await hl_proxy.proxy_hyperliquid_market(
        client, _settings(), symbol="BTC", tf="15m"
    )

    assert len(calls) >= 2  # upstream was reconnected after the clean close
    assert any(f.get("status") == "degraded" for f in client.sent)


@pytest.mark.asyncio
async def test_proxy_idle_watchdog_forces_reconnect(monkeypatch):
    """E3-03: a silent upstream (never closes, never sends) is detected by the
    idle watchdog, which forces a reconnect instead of hanging forever."""
    client = _FakeClientForProxy()
    up1 = _FakeUpstream()  # recv() blocks → only the watchdog can end it
    up2 = _FakeUpstream(on_enter=client.release)
    calls = _patch_connect(monkeypatch, [up1, up2])
    monkeypatch.setattr(hl_proxy, "HL_IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(hl_proxy, "HL_HEARTBEAT_INTERVAL", 100.0)  # keep hb quiet
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_START", 0.01)

    await hl_proxy.proxy_hyperliquid_market(
        client, _settings(), symbol="BTC", tf="15m"
    )

    assert len(calls) >= 2  # watchdog forced a reconnect
    assert any(f.get("status") == "degraded" for f in client.sent)


@pytest.mark.asyncio
async def test_proxy_sends_hl_app_ping(monkeypatch):
    """E3-03: the proxy must send HL's app-level {"method":"ping"} on the
    heartbeat timer, and an HL `pong`/junk frame must fall through the
    normalizer harmlessly (not forwarded to the browser)."""
    client = _FakeClientForProxy()
    ping_seen = asyncio.Event()

    def on_send(_up, data):
        if json.loads(data).get("method") == "ping":
            ping_seen.set()
            client.release()  # end the session once we've seen the app-ping

    # First frame is an HL pong; then recv blocks so only the heartbeat acts.
    up1 = _FakeUpstream(recv_items=['{"channel":"pong"}'], on_send=on_send)
    _patch_connect(monkeypatch, [up1])
    monkeypatch.setattr(hl_proxy, "HL_HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(hl_proxy, "HL_IDLE_TIMEOUT", 100.0)  # keep watchdog quiet

    await hl_proxy.proxy_hyperliquid_market(
        client, _settings(), symbol="BTC", tf="15m"
    )

    assert ping_seen.is_set()
    ping_frames = [s for s in up1.sent if json.loads(s).get("method") == "ping"]
    assert len(ping_frames) >= 1
    # The HL pong must not be forwarded to the browser as junk.
    assert not any(f.get("channel") == "pong" for f in client.sent)
    assert _normalize_hl({"channel": "pong"}, coin="BTC") is None


# --- Backoff reset only on a *healthy* session (flapping-upstream hardening) --
#
# A broken upstream can complete the WS handshake, "accept" our subscribe
# frames (only local buffer writes, always succeeds) and then drop immediately.
# The old code reset the backoff to HL_BACKOFF_START right after connect+
# subscribe, so every such flap restarted at 1.0 → the exponential backoff was
# never reached → ~60 reconnects/min against the venue. The backoff must only
# reset once the session has proved itself by forwarding real upstream data.


def _never_done_client_task():
    """A client_task that never completes (browser stays connected)."""
    return asyncio.ensure_future(asyncio.Event().wait())


def test_reset_backoff_after_session_healthy_resets_to_start(monkeypatch):
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_START", 1.0)
    # A healthy session (real data forwarded) must reset even a grown backoff.
    assert hl_proxy._reset_backoff_after_session(8.0, session_healthy=True) == 1.0


def test_reset_backoff_after_session_unhealthy_keeps_grown_backoff(monkeypatch):
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_START", 1.0)
    # An unhealthy flap must NOT reset — the grown backoff is kept.
    assert hl_proxy._reset_backoff_after_session(8.0, session_healthy=False) == 8.0


def test_flapping_backoff_ramps_to_cap_not_stuck_at_start(monkeypatch):
    """RED before fix: a run of unhealthy (0-frame) sessions must let the
    backoff ramp toward HL_BACKOFF_CAP, NOT reset to START every iteration."""
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_START", 1.0)
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_CAP", 10.0)
    backoff = hl_proxy.HL_BACKOFF_START
    slept = []
    for _ in range(8):
        backoff = hl_proxy._reset_backoff_after_session(backoff, session_healthy=False)
        slept.append(backoff)  # value actually slept on this iteration
        backoff = min(backoff * 2, hl_proxy.HL_BACKOFF_CAP)
    assert slept == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0]


def test_healthy_then_drop_reconnects_fast(monkeypatch):
    """Normal case: a long healthy session that then genuinely drops must
    reset the backoff so the next reconnect is fast — even if the backoff had
    grown before the session started."""
    monkeypatch.setattr(hl_proxy, "HL_BACKOFF_START", 1.0)
    grown = 8.0
    after = hl_proxy._reset_backoff_after_session(grown, session_healthy=True)
    assert after == 1.0


@pytest.mark.asyncio
async def test_session_reports_unhealthy_when_no_data_forwarded(monkeypatch):
    """A flapping session: connect+subscribe 'succeed' but the upstream drops
    with 0 forwarded frames → _run_upstream_session must report unhealthy."""
    monkeypatch.setattr(hl_proxy, "HL_HEARTBEAT_INTERVAL", 100.0)
    monkeypatch.setattr(hl_proxy, "HL_IDLE_TIMEOUT", 100.0)
    client = _FakeClientForProxy()
    up = _FakeUpstream(recv_items=[ConnectionError("dropped immediately")])
    client_task = _never_done_client_task()
    try:
        healthy = await hl_proxy._run_upstream_session(client, up, "BTC", client_task)
    finally:
        client_task.cancel()
    assert healthy is False


@pytest.mark.asyncio
async def test_session_reports_unhealthy_when_only_sub_ack(monkeypatch):
    """A bare subscriptionResponse (control ack) then drop is NOT proof of a
    healthy data stream → still unhealthy."""
    monkeypatch.setattr(hl_proxy, "HL_HEARTBEAT_INTERVAL", 100.0)
    monkeypatch.setattr(hl_proxy, "HL_IDLE_TIMEOUT", 100.0)
    client = _FakeClientForProxy()
    up = _FakeUpstream(
        recv_items=[
            json.dumps({"channel": "subscriptionResponse", "data": {}}),
            ConnectionError("dropped after ack"),
        ]
    )
    client_task = _never_done_client_task()
    try:
        healthy = await hl_proxy._run_upstream_session(client, up, "BTC", client_task)
    finally:
        client_task.cancel()
    assert healthy is False


@pytest.mark.asyncio
async def test_session_reports_healthy_when_real_data_forwarded(monkeypatch):
    """A session that forwards a real trade frame before the upstream drops
    must report healthy → the caller resets the backoff."""
    monkeypatch.setattr(hl_proxy, "HL_HEARTBEAT_INTERVAL", 100.0)
    monkeypatch.setattr(hl_proxy, "HL_IDLE_TIMEOUT", 100.0)
    client = _FakeClientForProxy()
    up = _FakeUpstream(
        recv_items=[
            json.dumps(
                {
                    "channel": "trades",
                    "data": [
                        {"px": "1.0", "sz": "1.0", "side": "B", "time": 1000},
                    ],
                }
            ),
            ConnectionError("dropped after real data"),
        ]
    )
    client_task = _never_done_client_task()
    try:
        healthy = await hl_proxy._run_upstream_session(client, up, "BTC", client_task)
    finally:
        client_task.cancel()
    assert healthy is True
