"""Roundtrip / fail-closed regression tests for the three performance findings.

These lock the LATENCY refactors (they must not silently regress back to the
extra roundtrips) AND prove the money-safety semantics are unchanged:

- Finding 1: Preview/Confirm fetch contract/ticker/account CONCURRENTLY; a read
  that raises still fails-closed with the identical OrderError class/message.
- Finding 2: HL reads assets+positions from ONE clearinghouseState fetch; MEXC
  reuses the gate's positions snapshot for the pre_hold / positionId lookup
  instead of a second live read.
- Finding 3: the monitor reads open_stop_orders once and user_fills once per
  position per cycle (baseline reuses the values the cycle already read).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.hyperliquid.errors import HyperliquidError
from app.models import ContractMeta, OrderTicket, Ticker
from app.orders import monitor
from app.orders.service import OrderError, OrderService
from app.orders.tokens import PreviewStore


# ── shared order-flow fixtures (mirror tests/test_orders_flow.py) ────────────
def _settings(**kwargs) -> Settings:
    base = dict(
        trading_enabled=True,
        max_leverage=20,
        max_risk_pct=1.0,
        min_rrr=2.0,
        strict_rrr=True,
        risk_slippage_pct=0.05,
        allow_unprotected_entry=False,
        max_notional_usdt=5000.0,
        preview_token_ttl_seconds=60,
        mexc_api_key="test-key",
        mexc_api_secret="test-secret",
        local_api_token="test-token",
    )
    base.update(kwargs)
    return Settings(**base)


def _contract() -> ContractMeta:
    return ContractMeta(
        symbol="BTC_USDT",
        contract_size=0.0001,
        price_unit=0.1,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1_000_000.0,
        max_leverage=125,
        min_leverage=1,
        api_allowed=True,
        state=0,
    )


def _good_ticket(**kwargs) -> OrderTicket:
    base = dict(
        symbol="BTC_USDT",
        side="long",
        order_type="limit",
        vol=1.0,
        leverage=5,
        price=100_000.0,
        entry=100_000.0,
        stop_loss=99_000.0,
        take_profit=102_000.0,
        open_type=1,
    )
    base.update(kwargs)
    return OrderTicket(**base)


_ASSETS = [{"currency": "USDT", "equity": 10_000.0, "availableBalance": 9_000.0}]


class CountingHLClient:
    """HL-shaped client that COUNTS clearinghouseState fetches.

    account_state()/assets()/positions() each represent one user_state fetch, so
    the counter is the number of clearinghouseState roundtrips a flow issued.
    Having `place_stop_order` present marks it as HL to the monitor.
    """

    exchange_id = "hyperliquid"

    def __init__(self) -> None:
        self.user_state_fetches = 0
        self.account_state_calls = 0
        self.assets_calls = 0
        self.positions_calls = 0
        self.fail_account = False
        self.place_stop_order = lambda *a, **k: None

    async def account_state(self, symbol=None, *, fresh=False):
        self.account_state_calls += 1
        if self.fail_account:
            raise HyperliquidError("429 rate limited (user_state)")
        self.user_state_fetches += 1
        return list(_ASSETS), []

    async def assets(self, *, fresh=False):
        self.assets_calls += 1
        if self.fail_account:
            raise HyperliquidError("429 rate limited (user_state)")
        self.user_state_fetches += 1
        return list(_ASSETS)

    async def positions(self, symbol=None, *, fresh=False):
        self.positions_calls += 1
        self.user_state_fetches += 1
        return []

    async def contract_meta(self, symbol):
        return _contract()

    async def ticker(self, symbol):
        return Ticker(symbol="BTC_USDT", last_price=100_000.0)


# ── FINDING 2 (HL): assets+positions come from ONE user_state fetch ──────────
@pytest.mark.asyncio
async def test_preview_hl_issues_single_user_state_fetch():
    client = CountingHLClient()
    svc = OrderService(client, _settings(), PreviewStore())

    out = await svc.preview(_good_ticket())

    assert out["ok"] is True
    # Before the fix: _balances→assets (1) + _existing_risk→positions (1) = 2
    # identical clearinghouseState fetches. After: one combined account_state.
    assert client.account_state_calls == 1
    assert client.user_state_fetches == 1
    assert client.assets_calls == 0
    assert client.positions_calls == 0


# ── FINDING 1: fail-closed is preserved through asyncio.gather ───────────────
@pytest.mark.asyncio
async def test_preview_account_429_fails_closed_as_dict():
    client = CountingHLClient()
    client.fail_account = True
    svc = OrderService(client, _settings(), PreviewStore())

    out = await svc.preview(_good_ticket())

    assert out["ok"] is False
    assert out["token"] is None
    assert any("equity unavailable" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_confirm_account_429_fails_closed_raises():
    client = CountingHLClient()
    store = PreviewStore()
    svc = OrderService(client, _settings(), store)

    prev = await svc.preview(_good_ticket())
    assert prev["ok"] is True

    # The account read 429s on confirm → must fail closed with the SAME error
    # class the balances path always raised (never place with unknown equity).
    client.fail_account = True
    with pytest.raises(OrderError, match="equity unavailable"):
        await svc.confirm(prev["token"])


@pytest.mark.asyncio
async def test_preview_contract_and_ticker_errors_keep_priority():
    class ContractFails(CountingHLClient):
        async def contract_meta(self, symbol):
            raise HyperliquidError("boom-contract")

    class TickerFails(CountingHLClient):
        async def ticker(self, symbol):
            raise HyperliquidError("boom-ticker")

    svc_c = OrderService(ContractFails(), _settings(), PreviewStore())
    with pytest.raises(OrderError, match="contract meta failed"):
        await svc_c.preview(_good_ticket())

    svc_t = OrderService(TickerFails(), _settings(), PreviewStore())
    with pytest.raises(OrderError, match="ticker failed"):
        await svc_t.preview(_good_ticket())


# ── FINDING 2 (MEXC): pre_hold reuses the injected positions snapshot ────────
class CountingMexcClient:
    exchange_id = "mexc"

    def __init__(self) -> None:
        self.positions_calls = 0

    async def positions(self, symbol=None, *, fresh=False):
        self.positions_calls += 1
        return []


@pytest.mark.asyncio
async def test_mexc_pre_hold_reuses_injected_positions_no_fetch():
    client = CountingMexcClient()
    svc = OrderService(client, _settings(), PreviewStore())
    snapshot = [
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "hold_vol": 3.0,
            "open_type": 1,
            "position_id": 42,
        }
    ]

    triple, pid = await svc._mexc_pre_hold_and_position_id(
        "BTC_USDT", "long", positions=snapshot
    )

    assert triple == (3.0, 1, True)  # checked=True: a supplied read is reliable
    assert pid == 42
    assert client.positions_calls == 0  # NO second live positions read


@pytest.mark.asyncio
async def test_mexc_pre_hold_self_fetches_when_not_injected():
    client = CountingMexcClient()
    svc = OrderService(client, _settings(), PreviewStore())

    triple, pid = await svc._mexc_pre_hold_and_position_id("BTC_USDT", "long")

    assert triple == (0.0, 1, True)
    assert pid is None
    assert client.positions_calls == 1  # legacy self-read path intact


# ── FINDING 3: monitor reads open_stop_orders + user_fills once per position ─
class CountingMonitorClient:
    """HL monitor client counting the per-position protective/fill reads."""

    def __init__(self) -> None:
        self.open_stop_orders_calls = 0
        self.user_fills_calls = 0
        self.place_stop_order = lambda *a, **k: None  # marks it HL

    async def account_snapshot(self, *, fresh=False):
        return {
            "positions": [
                {"symbol": "BTC_USDT", "side": "long", "entry_price": 100.0,
                 "hold_vol": 1.0}
            ]
        }

    async def ticker(self, symbol):
        return SimpleNamespace(last_price=102.0)

    async def open_stop_orders(self, symbol):
        self.open_stop_orders_calls += 1
        return [{"triggerPrice": 98.0, "orderType": "Stop"}]

    async def user_fills(self, symbol=None, limit=100):
        self.user_fills_calls += 1
        return [{"dir": "Open Long", "time": 5000, "start_position": 0.0}]


@pytest.mark.asyncio
async def test_monitor_reads_stop_and_fills_once_per_cycle(tmp_path, monkeypatch):
    from app.db.repo import Database

    db = Database(str(tmp_path / "roundtrip.db"))
    await db.init()

    client = CountingMonitorClient()
    app = SimpleNamespace(state=SimpleNamespace())
    app.state.db = db
    app.state.mexc = client
    app.state.exchange = client
    app.state.preview_store = None
    app.state.trade_lock = None
    # Spy out the write-service so no auto-BE modify path adds reads.
    monkeypatch.setattr(
        monitor,
        "_make_order_service",
        lambda a, c, s: SimpleNamespace(),
    )

    await monitor._run_one_cycle(app, 1_700_000_000_000)

    # Before the fix: _current_sl ran at _process_position AND inside
    # ensure_baseline (2x open_stop_orders); user_fills ran in
    # _best_effort_opened_at AND _hl_epoch_signature (2x). Now: one each.
    assert client.open_stop_orders_calls == 1
    assert client.user_fills_calls == 1
