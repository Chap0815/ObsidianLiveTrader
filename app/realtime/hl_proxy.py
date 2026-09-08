"""Proxy Hyperliquid public WebSocket streams to the browser.

Subscribes to trades (+ optional candle) for one coin and forwards
normalized JSON events to a FastAPI WebSocket client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config import Settings

log = logging.getLogger("app.realtime.hl")

# E3-03: HL kills an idle subscription after ~60s of app-level silence on
# sparse coins, so we send an app-level {"method":"ping"} well inside that
# window. The idle watchdog (E3-02) treats a *received*-side silence longer
# than this as a dead upstream and forces a reconnect — HL still pushes its
# own control traffic (and our heartbeat's pong) frequently enough that a real
# stall is the only way to hit it. Backoff is our own (independent of the
# websockets library ping) and caps at 10s. All are module-level so tests can
# shrink them.
# Heartbeat MUSS kleiner als der Idle-Watchdog sein: sonst feuert der
# Watchdog (recv-Timeout) auf einem ruhigen Coin, BEVOR der erste App-Ping
# raus ist, dessen pong den Read wieder wecken wuerde -> Dauer-Reconnect.
HL_HEARTBEAT_INTERVAL = 30.0
HL_IDLE_TIMEOUT = 45.0
HL_BACKOFF_START = 1.0
HL_BACKOFF_CAP = 10.0

# UI TF → HL candle interval
TF_TO_HL = {
    "5m": "5m",
    "15m": "15m",
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


def _reset_backoff_after_session(current_backoff: float, *, session_healthy: bool) -> float:
    """Decide the reconnect delay after an upstream session ended.

    A session that proved *healthy* — it forwarded at least one genuine
    upstream data frame — resets to ``HL_BACKOFF_START`` so a later genuine
    drop reconnects fast (and a single blip after a long healthy run never
    inherits a big delay). A session that ended without ever forwarding real
    data — connect+subscribe "succeeded" but the upstream dropped immediately
    (flapping) — keeps the grown backoff so repeated flaps ramp toward
    ``HL_BACKOFF_CAP`` instead of hammering the venue ~60x/min.

    Reads the module-level constants at call time so tests can shrink them.
    """
    if session_healthy:
        return HL_BACKOFF_START
    return current_backoff


def hl_ws_url(settings: Settings) -> str:
    if settings.hl_base_url:
        base = settings.hl_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/ws"
        if base.startswith("wss://") or base.startswith("ws://"):
            return base if base.endswith("/ws") else base + "/ws"
    if settings.hl_testnet:
        return "wss://api.hyperliquid-testnet.xyz/ws"
    return "wss://api.hyperliquid.xyz/ws"


def to_coin(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace("-", "_")
    return s.split("_")[0] if s else "BTC"


def _finite_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_float(value: Any) -> float | None:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _timestamp_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


async def _pump_client(client_ws: WebSocket) -> None:
    """Read client control frames (ping/resubscribe) until disconnect.

    O-10/Q-06(b): the browser side of this socket is not fully trusted to
    only ever send well-formed JSON. Uses ``receive_text`` + tolerant
    ``json.loads`` and *ignores* any frame that fails to parse, instead of
    letting the previous ``receive_json`` raise ``JSONDecodeError`` — which
    would complete this task and (via the caller's FIRST_COMPLETED wait)
    tear down the whole proxy, including the healthy upstream pump, over a
    single garbage frame. A genuine disconnect still propagates
    ``WebSocketDisconnect`` so the caller ends the proxy cleanly.
    """
    while True:
        raw = await client_ws.receive_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "ping":
            await client_ws.send_json({"type": "pong"})


async def proxy_hyperliquid_market(
    client_ws: WebSocket,
    settings: Settings,
    *,
    symbol: str,
    tf: str = "15m",
) -> None:
    """Bridge HL public WS → browser until the *browser* disconnects.

    E3-02/E3-03: the upstream side is resilient. A clean upstream close, an
    upstream error, or a silent data stop (idle watchdog) no longer ends the
    browser session — it triggers a backoff-limited reconnect + resubscribe of
    the SAME subscriptions, with a `degraded` status frame in between. The one
    thing that ends the loop is the browser going away
    (``WebSocketDisconnect`` propagating out of the shared client pump).
    """
    coin = to_coin(symbol)
    interval = TF_TO_HL.get(tf, "15m")
    url = hl_ws_url(settings)
    subs = [
        {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}},
        {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": coin, "interval": interval},
        },
        # bbo pushes on every top-of-book change → sub-second price movement
        # even when no trade prints (crucial on testnet where trades are sparse
        # and the chart would otherwise look frozen).
        {"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}},
    ]

    await client_ws.send_json(
        {
            "type": "status",
            "status": "connecting",
            "exchange": "hyperliquid",
            "coin": coin,
            "tf": tf,
            "upstream": url,
        }
    )

    # The client control pump lives for the WHOLE session, spanning every
    # upstream reconnect. It is the single source of "browser gone": when it
    # completes it raised WebSocketDisconnect, which we propagate to end.
    client_task = asyncio.create_task(_pump_client(client_ws))
    backoff = HL_BACKOFF_START
    try:
        while not client_task.done():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=15,
                    max_size=8 * 1024 * 1024,
                ) as upstream:
                    for sub in subs:
                        await upstream.send(json.dumps(sub))

                    await client_ws.send_json(
                        {
                            "type": "status",
                            "status": "live",
                            "exchange": "hyperliquid",
                            "coin": coin,
                            "tf": tf,
                            "subscriptions": ["trades", f"candle:{interval}"],
                        }
                    )
                    # NOTE: backoff is intentionally NOT reset on a bare
                    # connect+subscribe. A broken upstream can complete the
                    # handshake and "accept" our subscribe frames (only local
                    # buffer writes, always succeeds) then drop immediately;
                    # resetting here made every such flap restart at
                    # HL_BACKOFF_START → the exponential backoff was never
                    # reached → ~60 reconnects/min against the venue. We reset
                    # only once the session proved healthy (forwarded real data)
                    # — that still lets a single blip after a long healthy run
                    # reconnect fast, without inheriting a long delay.
                    healthy = await _run_upstream_session(
                        client_ws, upstream, coin, client_task
                    )
                    backoff = _reset_backoff_after_session(
                        backoff, session_healthy=healthy
                    )
            except WebSocketDisconnect:
                raise
            except Exception as e:
                # Upstream connect/subscribe/pump failed → reconnect below.
                log.warning("hl upstream ended coin=%s: %s", coin, e)

            if client_task.done():
                break
            # Upstream side ended while the browser is still here → tell the
            # browser we're degraded, back off, then reconnect + resubscribe.
            try:
                await client_ws.send_json(
                    {"type": "status", "status": "degraded", "coin": coin}
                )
            except WebSocketDisconnect:
                raise
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, HL_BACKOFF_CAP)
    except WebSocketDisconnect:
        log.info("browser disconnected coin=%s", coin)
    except Exception as e:
        log.warning("hl proxy error coin=%s: %s", coin, e)
        try:
            await client_ws.send_json(
                {"type": "status", "status": "error", "error": str(e), "coin": coin}
            )
        except Exception:
            pass
    finally:
        if not client_task.done():
            client_task.cancel()
        await asyncio.gather(client_task, return_exceptions=True)


async def _run_upstream_session(
    client_ws: WebSocket,
    upstream: Any,
    coin: str,
    client_task: asyncio.Task,
) -> bool:
    """Pump ONE upstream connection concurrently with the shared client pump.

    Returns normally when the *upstream* ended (clean close, error or idle
    watchdog timeout) → the caller reconnects. The return value is the
    session's *health*: ``True`` iff at least one genuine upstream data frame
    was forwarded to the browser (a bare ``subscriptionResponse`` control ack
    does NOT count — an upstream that flaps right after subscribing must not be
    mistaken for a working stream). Raises ``WebSocketDisconnect`` when the
    *browser* went away → the caller ends the whole proxy.

    Follows the file's FIRST_COMPLETED + cancel-pending pattern. The upstream
    read/heartbeat tasks are fully torn down (cancelled + awaited) before this
    returns, so no reconnect ever leaks a task from the previous connection.
    The shared ``client_task`` is NEVER cancelled here — it outlives the
    session and is owned by the caller.
    """

    forwarded_real_data = False

    async def pump_upstream() -> None:
        nonlocal forwarded_real_data
        while True:
            # Idle watchdog (E3-02): a silent upstream that never closes still
            # gets torn down so the caller can reconnect.
            raw = await asyncio.wait_for(upstream.recv(), timeout=HL_IDLE_TIMEOUT)
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            out = _normalize_hl(msg, coin=coin)
            if out is None:
                # HL's app-ping reply ({"channel":"pong"}) and any other
                # unhandled frame fall through here → not forwarded.
                continue
            await client_ws.send_json(out)
            # A forwarded *data* frame proves the stream is genuinely alive.
            # The subscription ack ("subscribed") is only a control reply and
            # is NOT proof — a flapping upstream can ack + drop immediately.
            if out.get("type") != "subscribed":
                forwarded_real_data = True

    async def heartbeat() -> None:
        # E3-03: HL enforces an app-level ping on inactivity (60s idle kill on
        # sparse coins). Its `pong` reply is dropped by _normalize_hl above.
        while True:
            await asyncio.sleep(HL_HEARTBEAT_INTERVAL)
            await upstream.send(json.dumps({"method": "ping"}))

    up_task = asyncio.create_task(pump_upstream())
    hb_task = asyncio.create_task(heartbeat())
    try:
        done, _pending = await asyncio.wait(
            {up_task, hb_task, client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (up_task, hb_task):
            if not t.done():
                t.cancel()
        # Await the upstream-scoped tasks so cancellation completes and no
        # "exception was never retrieved" warning leaks across reconnects.
        await asyncio.gather(up_task, hb_task, return_exceptions=True)

    if client_task in done:
        # Browser gone → surface it so the caller ends the proxy.
        exc = client_task.exception()
        if exc is not None:
            raise exc
        return forwarded_real_data
    # Upstream side finished. A send to a dead browser raises
    # WebSocketDisconnect → propagate; anything else (idle TimeoutError,
    # ConnectionClosed, …) just means: reconnect.
    for t in done:
        if isinstance(t.exception(), WebSocketDisconnect):
            raise t.exception()
    return forwarded_real_data


def _normalize_hl(msg: Any, *, coin: str) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return None
    ch = msg.get("channel")
    data = msg.get("data")

    if ch == "subscriptionResponse":
        return {"type": "subscribed", "data": data}

    if ch == "trades" and isinstance(data, list):
        trades = []
        for t in data:
            if not isinstance(t, dict):
                continue
            row_coin = t.get("coin", "")
            if not isinstance(row_coin, str):
                continue
            if row_coin.upper() not in ("", coin):
                # Wrong coin — skip it, do not forward as this coin's trade
                # (F-21). Still accept if coin is missing.
                continue
            px = _positive_float(t.get("px"))
            size_raw = t.get("sz")
            size = 0.0 if size_raw in (None, "") else _finite_float(size_raw)
            if px is None or size is None or size < 0:
                continue
            trades.append(
                {
                    "px": px,
                    "sz": size,
                    "side": t.get("side"),
                    "time": _timestamp_or_zero(t.get("time")),
                }
            )
        if not trades:
            return None
        last = trades[-1]
        return {
            "type": "trade",
            "coin": coin,
            "px": last["px"],
            "sz": last["sz"],
            "side": last["side"],
            "time": last["time"],
            "trades": trades,
        }

    if ch == "candle":
        # data can be list or single candle object
        candles = data if isinstance(data, list) else [data]
        out_bars = []
        for c in candles:
            if not isinstance(c, dict):
                continue
            row_coin_raw = c.get("s")
            if row_coin_raw is not None and not isinstance(row_coin_raw, str):
                continue
            row_coin = (row_coin_raw or "").upper()
            if row_coin and row_coin != coin:
                continue
            open_px = _positive_float(c.get("o"))
            high_px = _positive_float(c.get("h"))
            low_px = _positive_float(c.get("l"))
            close_px = _positive_float(c.get("c"))
            volume_raw = c.get("v")
            volume = 0.0 if volume_raw in (None, "") else _finite_float(volume_raw)
            if (
                None in (open_px, high_px, low_px, close_px)
                or volume is None
                or volume < 0
            ):
                continue
            if high_px < max(open_px, close_px) or low_px > min(open_px, close_px):
                continue
            # HL candle: t,T,o,h,l,c,v,s,i
            out_bars.append(
                {
                    "time_ms": _timestamp_or_zero(c.get("t")),
                    "open": open_px,
                    "high": high_px,
                    "low": low_px,
                    "close": close_px,
                    "vol": volume,
                    "interval": c.get("i"),
                    "coin": row_coin or coin,
                }
            )
        if not out_bars:
            return None
        bar = out_bars[-1]
        return {
            "type": "candle",
            "coin": coin,
            "bar": bar,
            "bars": out_bars,
        }

    if ch == "bbo" and isinstance(data, dict):
        # data: {"coin","time","bbo":[bid, ask]} where each is {"px","sz","n"}
        row_coin_raw = data.get("coin")
        if row_coin_raw is not None and not isinstance(row_coin_raw, str):
            return None
        row_coin = (row_coin_raw or "").upper()
        if row_coin and row_coin != coin:
            return None
        levels = data.get("bbo") or []
        try:
            bid = _positive_float(levels[0].get("px")) if levels and levels[0] else None
            ask = (
                _positive_float(levels[1].get("px"))
                if len(levels) > 1 and levels[1]
                else None
            )
        except (AttributeError, KeyError, TypeError, ValueError, IndexError):
            bid = ask = None
        mid = None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        if mid is None or not math.isfinite(mid) or mid <= 0:
            return None
        return {
            "type": "mid",
            "coin": coin,
            "px": mid,
            "time": _timestamp_or_zero(data.get("time")),
        }

    if ch == "allMids" and isinstance(data, dict):
        mids = data.get("mids") or data
        if isinstance(mids, dict) and coin in mids:
            price = _positive_float(mids[coin])
            if price is not None:
                return {"type": "mid", "coin": coin, "px": price}

    return None
