"""Proxy Hyperliquid public WebSocket streams to the browser.

Subscribes to trades (+ optional candle) for one coin and forwards
normalized JSON events to a FastAPI WebSocket client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config import Settings

log = logging.getLogger("app.realtime.hl")

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


async def proxy_hyperliquid_market(
    client_ws: WebSocket,
    settings: Settings,
    *,
    symbol: str,
    tf: str = "15m",
) -> None:
    """Bridge HL public WS → browser until client disconnects."""
    coin = to_coin(symbol)
    interval = TF_TO_HL.get(tf, "15m")
    url = hl_ws_url(settings)

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

    try:
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
            max_size=8 * 1024 * 1024,
        ) as upstream:
            subs = [
                {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}},
                {
                    "method": "subscribe",
                    "subscription": {
                        "type": "candle",
                        "coin": coin,
                        "interval": interval,
                    },
                },
                # bbo pushes on every top-of-book change → sub-second price
                # movement even when no trade prints (crucial on testnet where
                # trades are sparse and the chart would otherwise look frozen).
                {"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}},
            ]
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

            # Concurrent: upstream → client, client → control (unsubscribe/close)
            async def pump_upstream() -> None:
                async for raw in upstream:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    out = _normalize_hl(msg, coin=coin)
                    if out is None:
                        continue
                    await client_ws.send_json(out)

            async def pump_client() -> None:
                while True:
                    data = await client_ws.receive_json()
                    # optional resubscribe
                    if isinstance(data, dict) and data.get("type") == "ping":
                        await client_ws.send_json({"type": "pong"})

            t1 = asyncio.create_task(pump_upstream())
            t2 = asyncio.create_task(pump_client())
            done, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    raise exc
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


def _normalize_hl(msg: dict[str, Any], *, coin: str) -> dict[str, Any] | None:
    ch = msg.get("channel")
    data = msg.get("data")

    if ch == "subscriptionResponse":
        return {"type": "subscribed", "data": data}

    if ch == "trades" and isinstance(data, list):
        trades = []
        for t in data:
            if str(t.get("coin", "")).upper() not in ("", coin):
                # still accept if coin missing
                pass
            try:
                trades.append(
                    {
                        "px": float(t["px"]),
                        "sz": float(t.get("sz") or 0),
                        "side": t.get("side"),
                        "time": int(t.get("time") or 0),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
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
            try:
                # HL candle: t,T,o,h,l,c,v,s,i
                out_bars.append(
                    {
                        "time_ms": int(c.get("t") or 0),
                        "open": float(c["o"]),
                        "high": float(c["h"]),
                        "low": float(c["l"]),
                        "close": float(c["c"]),
                        "vol": float(c.get("v") or 0),
                        "interval": c.get("i"),
                        "coin": c.get("s") or coin,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
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
        levels = data.get("bbo") or []
        try:
            bid = float(levels[0]["px"]) if levels and levels[0] else None
            ask = float(levels[1]["px"]) if len(levels) > 1 and levels[1] else None
        except (KeyError, TypeError, ValueError, IndexError):
            bid = ask = None
        mid = None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        if mid is None:
            return None
        return {
            "type": "mid",
            "coin": coin,
            "px": mid,
            "time": int(data.get("time") or 0),
        }

    if ch == "allMids" and isinstance(data, dict):
        mids = data.get("mids") or data
        if coin in mids:
            try:
                return {
                    "type": "mid",
                    "coin": coin,
                    "px": float(mids[coin]),
                }
            except (TypeError, ValueError):
                return None

    return None
