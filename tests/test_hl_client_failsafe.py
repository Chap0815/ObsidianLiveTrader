"""HyperliquidClient exchange-integrity fail-safes (final audit C-1, M-2, H-2).

All tests drive the REAL client with a mocked SDK info object — no keys, no
network. They pin the money-safety invariant that a degraded/errored exchange
response must surface as an error (→ UNKNOWN downstream), never as a confident
"flat account" / "no stop orders".
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.hyperliquid.client import HyperliquidClient
from app.hyperliquid.errors import HyperliquidError


def _client(info: MagicMock) -> HyperliquidClient:
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    c.account_address = "0x" + "2" * 40
    c._info = info
    c._get_info = MagicMock(return_value=info)
    return c


# ── C-1: open_stop_orders must not silently fall back to a non-trigger endpoint ─


@pytest.mark.asyncio
async def test_open_stop_orders_raises_on_frontend_failure_no_fallback():
    """A frontend_open_orders failure must RAISE (→ SL status UNKNOWN), never be
    swapped for the non-trigger-aware open_orders() which would return a
    confident empty list and read as 'no stop' → false auto-flatten."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(side_effect=RuntimeError("HL 503"))
    info.open_orders = MagicMock(return_value=[])  # must never be consulted
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.open_stop_orders("BTC")
    info.open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_open_stop_orders_raises_on_degraded_shape():
    """A 200-OK-but-degraded body (dict/None instead of a list) is not proof of
    'no triggers' — refuse it rather than trust an empty confident list."""
    for degraded in ({}, None, {"foo": "bar"}):
        info = MagicMock()
        info.frontend_open_orders = MagicMock(return_value=degraded)
        info.open_orders = MagicMock(return_value=[])
        c = _client(info)
        with pytest.raises(HyperliquidError):
            await c.open_stop_orders("BTC")
        info.open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_open_stop_orders_returns_triggers_on_recognized_list():
    """Positive path: a recognized list with a trigger row is returned."""
    info = MagicMock()
    info.frontend_open_orders = MagicMock(
        return_value=[
            {"coin": "BTC", "oid": 5, "isTrigger": True, "orderType": "Stop Market",
             "triggerPx": "99000", "reduceOnly": True},
            {"coin": "BTC", "oid": 6, "isTrigger": False, "orderType": "Limit"},
        ]
    )
    c = _client(info)
    out = await c.open_stop_orders("BTC")
    assert len(out) == 1
    assert out[0]["orderId"] == 5
    assert out[0]["triggerPrice"] == "99000"


# ── M-2: a degraded user_state body must NOT be trusted as a flat account ──────

_COMPLETE_STATE = {
    "marginSummary": {
        "accountValue": "1000",
        "totalMarginUsed": "50",
        "totalNtlPos": "500",
    },
    "withdrawable": "950",
    "assetPositions": [],
}

# 200-OK-but-degraded/partial shapes: not a proper user_state payload. A
# complete body always carries BOTH a margin summary AND an assetPositions list;
# anything missing either must fail-closed (raise), never read as "flat/no
# positions" which would hide a real position from risk/flatten logic.
_DEGRADED_STATES = [
    None,
    {},
    [],
    {"foo": "bar"},
    {"marginSummary": {"accountValue": "1000"}},  # positions key missing
    {"assetPositions": []},  # margin summary missing
]


@pytest.mark.parametrize("degraded", _DEGRADED_STATES)
@pytest.mark.asyncio
async def test_assets_raises_on_degraded_user_state(degraded):
    info = MagicMock()
    info.user_state = MagicMock(return_value=degraded)
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.assets()


@pytest.mark.parametrize("degraded", _DEGRADED_STATES)
@pytest.mark.asyncio
async def test_positions_raises_on_degraded_user_state(degraded):
    info = MagicMock()
    info.user_state = MagicMock(return_value=degraded)
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.positions()


@pytest.mark.asyncio
async def test_assets_and_positions_ok_on_complete_state():
    """Positive path: a complete user_state is honoured (equity/flat position)."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    rows = await c.assets()
    assert rows[0]["equity"] == 1000.0
    assert await c.positions() == []


# ── user_state short-TTL cache + 429 resilience (over-poll → 429 → 502 fix) ───


@pytest.mark.asyncio
async def test_assets_and_positions_share_one_user_state_within_ttl():
    """assets()+positions() within the short TTL must collapse to a SINGLE
    upstream user_state fetch. This dedups account_snapshot's 2 reads and rapid
    concurrent endpoint polls that were driving Hyperliquid 429 → /api/market 502."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()
    await c.positions()
    assert info.user_state.call_count == 1


@pytest.mark.asyncio
async def test_assets_positions_serve_stale_cache_on_429():
    """A warm cache older than the TTL but within the stale bound must be served
    when the refetch 429s — a transient rate-limit burst degrades to a slightly
    stale read instead of 502-ing the market view / starving the trade monitor."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()  # warm the cache (call_count == 1)
    # Age the cache past the TTL but within the stale bound, then 429 the refetch.
    ts, state, addr = c._user_state_cache
    c._user_state_cache = (time.time() - 5.0, state, addr)
    info.user_state = MagicMock(side_effect=RuntimeError("429 Too Many Requests"))
    rows = await c.assets()
    assert rows[0]["equity"] == 1000.0  # stale equity served, no raise
    assert await c.positions() == []  # stale positions served, no raise


@pytest.mark.asyncio
async def test_assets_positions_raise_on_error_without_warm_cache():
    """No usable cache + upstream error → still fail honestly (never fabricate a
    flat account). Only a genuinely warm cache may absorb a 429."""
    info = MagicMock()
    info.user_state = MagicMock(side_effect=RuntimeError("429 Too Many Requests"))
    c = _client(info)
    with pytest.raises(HyperliquidError):
        await c.assets()
    with pytest.raises(HyperliquidError):
        await c.positions()


@pytest.mark.asyncio
async def test_assets_raises_when_cache_too_stale_on_error():
    """Beyond the bounded stale window the cache is no longer trustworthy: a 429
    with a too-old cache must raise, not serve arbitrarily old equity/positions."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    await c.assets()
    ts, state, addr = c._user_state_cache
    c._user_state_cache = (time.time() - 30.0, state, addr)  # past MAX_STALE
    info.user_state = MagicMock(side_effect=RuntimeError("429"))
    with pytest.raises(HyperliquidError):
        await c.assets()


# ── _to_thread transient-429 retry (read paths retry, money path NEVER) ───────


class _Boom(Exception):
    """Mimics the SDK ClientError enough for _is_rate_limited (has .status_code)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"({status_code}, None, 'null')")
        self.status_code = status_code


@pytest.mark.asyncio
async def test_to_thread_retries_read_path_on_429(monkeypatch):
    """A read/data call that 429s must be retried with backoff and succeed once
    the transient burst clears — this is what rides out a market-scan 429."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)  # no real sleep in test
    c = _client(MagicMock())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Boom(429)
        return "ok"

    assert await c._to_thread(fn) == "ok"
    assert calls["n"] == 3  # two 429s absorbed, third attempt succeeds


@pytest.mark.asyncio
async def test_to_thread_never_retries_money_path_on_429(monkeypatch):
    """A money-path call (place/modify/cancel) must NEVER be auto-resent on 429 —
    the first send may already have landed; a retry could double the order."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)
    c = _client(MagicMock())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _Boom(429)

    with pytest.raises(HyperliquidError):
        await c._to_thread(fn, money_path=True)
    assert calls["n"] == 1  # exactly one attempt, no resend


@pytest.mark.asyncio
async def test_to_thread_does_not_retry_non_429(monkeypatch):
    """Only 429 is transient. A 500/other error fails fast — no retry storm."""
    import app.hyperliquid.client as mod

    monkeypatch.setattr(mod, "_RATE_LIMIT_BACKOFF_S", 0.0)
    c = _client(MagicMock())
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _Boom(500)

    with pytest.raises(HyperliquidError):
        await c._to_thread(fn)
    assert calls["n"] == 1


# ── H-2: totalNtlPos is notional exposure, not unrealized PnL ─────────────────


@pytest.mark.asyncio
async def test_assets_does_not_mislabel_notional_as_unrealized():
    """`marginSummary.totalNtlPos` is total NOTIONAL position value, not
    unrealized PnL. It must not be surfaced under a misleading 'unrealized' key
    that a future PnL/UI caller could trust."""
    info = MagicMock()
    info.user_state = MagicMock(return_value=_COMPLETE_STATE)
    c = _client(info)
    row = (await c.assets())[0]
    assert "unrealized" not in row
    assert row["notional_position"] == 500.0


# ── O5: funding_rate reads from the ctx cache, no wasteful all_mids() ──────────


@pytest.mark.asyncio
async def test_funding_rate_reads_from_ctx_without_all_mids():
    """O5: funding is read straight from meta_and_asset_ctxs (the shared ctx
    cache), so funding_rate() must NOT trigger the all_mids() round-trip that
    ticker() used to do just for an unneeded mid price."""
    info = MagicMock()
    meta_ctx = (
        {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [{"funding": 0.00012}, {"funding": -0.0003}],
    )
    info.meta_and_asset_ctxs = MagicMock(return_value=meta_ctx)
    info.all_mids = MagicMock(side_effect=AssertionError("all_mids must not be called"))
    c = _client(info)
    fr = await c.funding_rate("BTC_USDT")
    assert fr.symbol == "BTC"
    assert fr.funding_rate == 0.00012
    info.all_mids.assert_not_called()


@pytest.mark.asyncio
async def test_funding_rate_falls_back_to_zero_when_ctx_missing_coin():
    """If the ctx doesn't carry the coin (or funding), fall back safely to 0.0
    with the same return shape rather than raising."""
    info = MagicMock()
    meta_ctx = ({"universe": [{"name": "SOL"}]}, [{"funding": 0.001}])
    info.meta_and_asset_ctxs = MagicMock(return_value=meta_ctx)
    c = _client(info)
    fr = await c.funding_rate("BTC_USDT")
    assert fr.symbol == "BTC"
    assert fr.funding_rate == 0.0
