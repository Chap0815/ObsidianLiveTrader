"""Order preview/confirm flow with MOCKED MexcClient — no real keys."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings, get_settings
from app.models import (
    ArmRequest,
    CancelRequest,
    ConfirmRequest,
    ContractMeta,
    ModifySLRequest,
    OrderTicket,
    Ticker,
)
from app.orders.service import (
    OrderError,
    OrderOutcomeUnknown,
    OrderRejectedByExchange,
    OrderService,
    scale_out_errors,
    ticket_to_mexc_body,
)
from app.orders.tokens import PreviewStore, TokenError


def _settings(**kwargs) -> Settings:
    base = dict(
        exchange="mexc",
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


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.contract_meta = AsyncMock(return_value=_contract())
    client.ticker = AsyncMock(
        return_value=Ticker(symbol="BTC_USDT", last_price=100_000.0)
    )
    client.assets = AsyncMock(
        return_value=[
            {
                "currency": "USDT",
                "equity": 10_000.0,
                "availableBalance": 9_000.0,
            }
        ]
    )
    client.positions = AsyncMock(return_value=[])
    client.open_stop_orders = AsyncMock(
        return_value=[
            {"orderId": 777, "stopLossPrice": 99_000.0, "symbol": "BTC_USDT"}
        ]
    )
    client.open_orders = AsyncMock(return_value=[])
    client.set_leverage = AsyncMock(return_value={"success": True})
    client.place_order = AsyncMock(return_value={"orderId": 12345, "stopLossPrice": 99_000.0})
    client.cancel_order = AsyncMock(return_value={"success": True})
    client.close_position_market = AsyncMock(return_value={"orderId": 999})
    return client


@pytest.fixture
def store() -> PreviewStore:
    return PreviewStore()


@pytest.fixture
def client() -> MagicMock:
    return _mock_client()


async def _invoke_money_mutation(svc: OrderService, operation: str) -> None:
    if operation == "confirm":
        await svc.confirm("unused-token")
    elif operation == "close":
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)
    elif operation == "cancel":
        await svc.cancel(order_id="123", symbol="BTC_USDT")
    else:
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["confirm", "close", "cancel", "modify_sl"])
async def test_money_mutation_rejects_replaced_exchange_client_before_work(
    client, store, operation
):
    svc = OrderService(
        client,
        _settings(),
        store,
        client_is_active=lambda candidate: False,
    )
    client.reset_mock()

    with pytest.raises(OrderError, match="Exchange configuration changed"):
        await _invoke_money_mutation(svc, operation)

    assert client.method_calls == []


@pytest.mark.asyncio
async def test_queued_money_mutation_rechecks_client_after_trade_lock(client, store):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = True

    class GateLock:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    svc = OrderService(
        client,
        _settings(),
        store,
        trade_lock=GateLock(),
        client_is_active=lambda candidate: active,
    )
    client.reset_mock()
    task = asyncio.create_task(svc.cancel(order_id="123", symbol="BTC_USDT"))
    await entered.wait()

    active = False
    release.set()
    with pytest.raises(OrderError, match="Exchange configuration changed"):
        await task

    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_service_rejects_replaced_settings_with_same_client(
    client, store, monkeypatch
):
    from types import SimpleNamespace

    import app.main as main

    original = _settings()
    active_settings = original
    monkeypatch.setattr(main, "get_settings", lambda: active_settings)
    state = SimpleNamespace(
        mexc=client,
        exchange=client,
        preview_store=store,
        db=None,
        trade_lock=asyncio.Lock(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    svc = main._order_service(request)
    active_settings = original.model_copy(update={"max_risk_pct": 0.5})
    client.reset_mock()

    with pytest.raises(OrderError, match="Exchange configuration changed"):
        await svc.cancel(order_id="123", symbol="BTC_USDT")

    assert client.method_calls == []


@pytest.mark.asyncio
async def test_preview_rejects_replaced_exchange_client_before_reads(client, store):
    svc = OrderService(
        client,
        _settings(),
        store,
        client_is_active=lambda candidate: False,
    )
    client.reset_mock()

    with pytest.raises(OrderError, match="Exchange configuration changed"):
        await svc.preview(_good_ticket())

    assert client.method_calls == []
    assert store._items == {}


@pytest.mark.asyncio
async def test_preview_rejects_client_replaced_during_exchange_reads(client, store):
    started = asyncio.Event()
    release = asyncio.Event()
    active = True

    async def delayed_ticker(_symbol):
        started.set()
        await release.wait()
        return Ticker(symbol="BTC_USDT", last_price=100_000.0)

    client.ticker = AsyncMock(side_effect=delayed_ticker)
    svc = OrderService(
        client,
        _settings(),
        store,
        client_is_active=lambda candidate: active,
    )
    task = asyncio.create_task(svc.preview(_good_ticket()))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    active = False
    release.set()
    with pytest.raises(OrderError, match="Exchange configuration changed"):
        await task

    assert store._items == {}


@pytest.mark.asyncio
async def test_preview_discards_token_if_client_replaced_during_db_insert(client, store):
    started = asyncio.Event()
    release = asyncio.Event()
    active = True

    class BlockingDb:
        async def insert_preview(self, **_fields):
            started.set()
            await release.wait()

    svc = OrderService(
        client,
        _settings(),
        store,
        db=BlockingDb(),
        client_is_active=lambda candidate: active,
    )
    task = asyncio.create_task(svc.preview(_good_ticket()))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    active = False
    release.set()
    with pytest.raises(OrderError, match="Exchange configuration changed"):
        await task

    assert store._items == {}


@pytest.mark.asyncio
async def test_slow_preview_does_not_return_token_superseded_by_newer_preview(
    client, store
):
    first_insert_started = asyncio.Event()
    release_first_insert = asyncio.Event()

    class BlockingFirstDb:
        calls = 0

        async def insert_preview(self, **_fields):
            self.calls += 1
            if self.calls == 1:
                first_insert_started.set()
                await release_first_insert.wait()

    svc = OrderService(client, _settings(), store, db=BlockingFirstDb())
    older_task = asyncio.create_task(svc.preview(_good_ticket()))
    await asyncio.wait_for(first_insert_started.wait(), timeout=1.0)

    newer = await svc.preview(_good_ticket(vol=2.0))
    release_first_insert.set()
    with pytest.raises(OrderError, match="superseded|expired"):
        await older_task

    assert store.peek(newer["token"]) is not None
    assert store.consume(newer["token"])["ticket"]["vol"] == 2.0


@pytest.mark.asyncio
async def test_preview_does_not_return_token_expired_during_db_insert(
    client, store, monkeypatch
):
    import app.orders.tokens as token_module

    monotonic_now = [100.0]
    monkeypatch.setattr(token_module.time, "monotonic", lambda: monotonic_now[0])

    class ExpiringDb:
        async def insert_preview(self, **_fields):
            monotonic_now[0] = 101.0

    svc = OrderService(
        client,
        _settings(preview_token_ttl_seconds=1),
        store,
        db=ExpiringDb(),
    )

    with pytest.raises(OrderError, match="superseded|expired"):
        await svc.preview(_good_ticket())

    assert store._items == {}


@pytest.mark.asyncio
async def test_preview_reports_ttl_remaining_after_db_insert(client, store, monkeypatch):
    import app.orders.tokens as token_module

    monotonic_now = [100.0]
    monkeypatch.setattr(token_module.time, "monotonic", lambda: monotonic_now[0])

    class DelayedDb:
        async def insert_preview(self, **_fields):
            monotonic_now[0] = 110.0

    svc = OrderService(client, _settings(), store, db=DelayedDb())

    preview = await svc.preview(_good_ticket())

    assert preview["expires_in_seconds"] == 50
    assert store.peek(preview["token"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, [], ([],), ([], [], []), {"assets": [], "positions": []}],
    ids=["null", "empty", "one-item", "three-items", "object"],
)
async def test_combined_account_state_rejects_invalid_envelope(store, payload):
    class Client:
        async def account_state(self, _symbol, *, fresh=False):
            return payload

    svc = OrderService(Client(), _settings(), store)

    with pytest.raises(OrderError, match="combined account state.*invalid"):
        await svc._read_account_state("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [(None, []), ([], None), ([None], []), ([], [None])],
    ids=["null-assets", "null-positions", "bad-asset-row", "bad-position-row"],
)
async def test_combined_account_state_rejects_invalid_collections(store, payload):
    class Client:
        async def account_state(self, _symbol, *, fresh=False):
            return payload

    svc = OrderService(Client(), _settings(), store)

    with pytest.raises(OrderError, match="combined account state.*invalid"):
        await svc._read_account_state("BTC_USDT")


def test_ticket_to_mexc_body_side_and_type():
    t = _good_ticket(side="long", order_type="limit")
    body = ticket_to_mexc_body(
        t,
        rounded_vol=1.0,
        rounded_price=100_000.0,
        external_oid="mlt-test",
        stop_loss=99_000.0,
        take_profit=102_000.0,
    )
    assert body["side"] == 1
    assert body["type"] == 1
    assert body["vol"] == 1.0
    assert body["price"] == 100_000.0
    assert body["stopLossPrice"] == 99_000.0
    assert body["externalOid"] == "mlt-test"

    t2 = _good_ticket(side="short", order_type="market")
    body2 = ticket_to_mexc_body(
        t2,
        rounded_vol=2.0,
        rounded_price=None,
        external_oid="mlt-2",
    )
    assert body2["side"] == 3
    assert body2["type"] == 5
    assert body2["price"] == 0


@pytest.mark.asyncio
async def test_over_leverage_rejected_on_preview(client, store):
    svc = OrderService(client, _settings(), store)
    out = await svc.preview(_good_ticket(leverage=50))
    assert out["ok"] is False
    assert out["token"] is None
    assert any("MAX_LEVERAGE" in e for e in out["errors"])
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_risk_over_max_rejected_on_preview(client, store):
    svc = OrderService(client, _settings(max_risk_pct=1.0), store)
    out = await svc.preview(_good_ticket(vol=50_000, take_profit=200_000))
    assert out["ok"] is False
    assert any("MAX_RISK_PCT" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_preview_rejects_ticker_for_another_symbol(client, store):
    client.ticker = AsyncMock(
        return_value=Ticker(symbol="ETH_USDT", last_price=100_000.0)
    )
    svc = OrderService(client, _settings(), store)

    with pytest.raises(OrderError, match="ticker symbol"):
        await svc.preview(_good_ticket())

    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_rechecks_ticker_symbol_identity(client, store):
    svc = OrderService(client, _settings(trading_enabled=True), store)
    preview = await svc.preview(_good_ticket())
    assert preview["ok"] is True
    client.ticker = AsyncMock(
        return_value=Ticker(symbol="ETH_USDT", last_price=100_000.0)
    )

    with pytest.raises(OrderError, match="ticker symbol"):
        await svc.confirm(preview["token"])

    client.set_leverage.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_rrr_strict_rejected_on_preview(client, store):
    svc = OrderService(client, _settings(strict_rrr=True, min_rrr=2.0), store)
    out = await svc.preview(_good_ticket(take_profit=100_600.0))
    assert out["ok"] is False
    assert any("RRR" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_preview_account_failure_does_not_reflect_exchange_detail(client, store):
    from app.mexc.errors import MexcError

    marker = "SYNTHETIC_PRIVATE_PREVIEW_ACCOUNT_ERROR"
    client.assets = AsyncMock(side_effect=MexcError(marker))
    svc = OrderService(client, _settings(), store)

    out = await svc.preview(_good_ticket())

    assert out["ok"] is False
    assert out["token"] is None
    assert marker not in str(out)
    assert out["errors"] == [
        "equity unavailable; positions unavailable — exchange account data could "
        "not be read"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_last_price", [True, "100000.0"])
async def test_preview_rejects_untyped_ticker_before_issuing_token(
    client, store, bad_last_price
):
    client.ticker = AsyncMock(
        return_value=MagicMock(symbol="BTC_USDT", last_price=bad_last_price)
    )
    svc = OrderService(client, _settings(), store)

    with pytest.raises(OrderError, match="ticker price"):
        await svc.preview(_good_ticket())

    assert store._items == {}
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_rejects_mutated_numeric_string_ticket_before_exchange_reads(
    client, store
):
    ticket = _good_ticket()
    object.__setattr__(ticket, "vol", "1.0")
    svc = OrderService(client, _settings(), store)

    with pytest.raises(OrderError, match="invalid ticket"):
        await svc.preview(ticket)

    client.contract_meta.assert_not_awaited()
    client.ticker.assert_not_awaited()
    client.assets.assert_not_awaited()
    client.positions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_last_price", [True, "100000.0"])
async def test_confirm_rejects_untyped_ticker_before_order_send(
    client, store, bad_last_price
):
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())
    assert preview["ok"] is True
    client.ticker = AsyncMock(
        return_value=MagicMock(symbol="BTC_USDT", last_price=bad_last_price)
    )

    with pytest.raises(OrderError, match="ticker price"):
        await svc.confirm(preview["token"])

    client.set_leverage.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_db_failure_discards_unpublished_token(client, store):
    db = MagicMock()
    db.insert_preview = AsyncMock(side_effect=RuntimeError("sqlite unavailable"))
    svc = OrderService(client, _settings(), store, db=db)

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        await svc.preview(_good_ticket())

    assert store._items == {}
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_db_cancellation_discards_unpublished_token(client, store):
    db = MagicMock()
    db.insert_preview = AsyncMock(side_effect=asyncio.CancelledError)
    svc = OrderService(client, _settings(), store, db=db)

    with pytest.raises(asyncio.CancelledError):
        await svc.preview(_good_ticket())

    assert store._items == {}
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_proposal_provenance_is_validated_and_audited(tmp_path, client, store):
    from app.db.repo import Database

    db = Database(str(tmp_path / "proposal_order.db"))
    await db.init()
    proposal_id = await db.insert_proposal(
        symbol="BTC_USDT", proposal_json={"action": "BUY", "thesis": "source"}
    )
    client.exchange_id = "mexc"
    svc = OrderService(client, _settings(), store, db=db)
    preview = await svc.preview(_good_ticket(proposal_id=proposal_id))
    assert preview["ok"] is True
    await svc.confirm(preview["token"])

    orders = await db.recent_orders()
    assert orders[0]["request"]["_proposal_id"] == proposal_id

    wrong_id = await db.insert_proposal(
        symbol="ETH_USDT", proposal_json={"action": "BUY"}
    )
    with pytest.raises(OrderError, match="does not match"):
        await svc.preview(_good_ticket(proposal_id=wrong_id))


@pytest.mark.asyncio
async def test_confirm_rejects_nonfinite_equity_after_valid_preview(client, store):
    valid = [
        {"currency": "USDT", "equity": 10_000.0, "availableBalance": 9_000.0}
    ]
    invalid = [
        {"currency": "USDT", "equity": "NaN", "availableBalance": 9_000.0}
    ]
    client.assets = AsyncMock(side_effect=[valid, invalid])
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())
    assert preview["ok"] is True
    with pytest.raises(OrderError, match="equity unknown"):
        await svc.confirm(preview["token"])
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_audit_mark_failure_consumes_before_exchange_read(client, store):
    db = MagicMock()
    db.insert_preview = AsyncMock()
    db.mark_preview_used = AsyncMock(side_effect=RuntimeError("sqlite unavailable"))
    svc = OrderService(client, _settings(), store, db=db)
    preview = await svc.preview(_good_ticket())

    client.contract_meta.reset_mock()
    client.ticker.reset_mock()
    client.assets.reset_mock()
    client.positions.reset_mock()
    client.open_orders.reset_mock()

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        await svc.confirm(preview["token"])

    with pytest.raises(TokenError):
        store.consume(preview["token"])
    client.contract_meta.assert_not_awaited()
    client.ticker.assert_not_awaited()
    client.assets.assert_not_awaited()
    client.positions.assert_not_awaited()
    client.open_orders.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_rejects_preview_without_bound_external_oid(client, store):
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())
    with store._lock:
        del store._items[preview["token"]]["payload"]["external_oid"]

    client.contract_meta.reset_mock()
    client.ticker.reset_mock()
    client.assets.reset_mock()
    client.positions.reset_mock()
    client.open_orders.reset_mock()

    with pytest.raises(OrderError, match="externalOid|external_oid|payload"):
        await svc.confirm(preview["token"])

    client.contract_meta.assert_not_awaited()
    client.ticker.assert_not_awaited()
    client.assets.assert_not_awaited()
    client.positions.assert_not_awaited()
    client.open_orders.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_price",
    [None, float("nan"), float("inf"), 0, -1, "not-a-price", "100000.0", 10**400],
)
async def test_confirm_rejects_invalid_preview_price_before_exchange_read(
    client, store, bad_price
):
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())
    with store._lock:
        store._items[preview["token"]]["payload"]["last_price"] = bad_price

    client.contract_meta.reset_mock()
    client.ticker.reset_mock()
    client.assets.reset_mock()
    client.positions.reset_mock()
    client.open_orders.reset_mock()

    with pytest.raises(OrderError, match="last_price|market price|payload"):
        await svc.confirm(preview["token"])

    client.contract_meta.assert_not_awaited()
    client.ticker.assert_not_awaited()
    client.assets.assert_not_awaited()
    client.positions.assert_not_awaited()
    client.open_orders.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket_payload", [None, {"symbol": "BTC_USDT"}])
async def test_confirm_maps_invalid_internal_ticket_before_exchange_read(
    client, store, ticket_payload
):
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())
    with store._lock:
        payload = store._items[preview["token"]]["payload"]
        if ticket_payload is None:
            del payload["ticket"]
        else:
            payload["ticket"] = ticket_payload

    client.contract_meta.reset_mock()
    client.ticker.reset_mock()
    client.assets.reset_mock()
    client.positions.reset_mock()
    client.open_orders.reset_mock()

    with pytest.raises(OrderError, match="preview payload.*ticket"):
        await svc.confirm(preview["token"])

    with pytest.raises(TokenError):
        store.consume(preview["token"])
    client.contract_meta.assert_not_awaited()
    client.ticker.assert_not_awaited()
    client.assets.assert_not_awaited()
    client.positions.assert_not_awaited()
    client.open_orders.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_rejects_numeric_string_in_stored_ticket_before_exchange_read(
    client, store
):
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())
    with store._lock:
        store._items[preview["token"]]["payload"]["ticket"]["vol"] = "1.0"

    client.contract_meta.reset_mock()
    client.ticker.reset_mock()
    client.assets.reset_mock()
    client.positions.reset_mock()
    client.open_orders.reset_mock()

    with pytest.raises(OrderError, match="preview payload.*ticket"):
        await svc.confirm(preview["token"])

    client.contract_meta.assert_not_awaited()
    client.ticker.assert_not_awaited()
    client.assets.assert_not_awaited()
    client.positions.assert_not_awaited()
    client.open_orders.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_unprotected_without_sl_rejected(client, store):
    svc = OrderService(
        client, _settings(allow_unprotected_entry=False), store
    )
    out = await svc.preview(
        _good_ticket(stop_loss=None, take_profit=None)
    )
    assert out["ok"] is False
    assert any("stop_loss" in e.lower() or "UNPROTECTED" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_preview_then_confirm_places_once(client, store):
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    assert prev["ok"] is True
    assert prev["token"]
    assert "risk_usdt" in prev["summary"]

    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    client.place_order.assert_called_once()
    body = client.place_order.call_args[0][0]
    assert body["side"] == 1
    assert body["externalOid"].startswith("mlt-")
    assert "stopLossPrice" in body
    client.set_leverage.assert_called()


# ── FINDING 1: an add-on to an EXISTING same-side MEXC position must forward
# the positionId to change_leverage (MEXC rejects a leverage set on an open
# position without it → every add-on was hard-blocked). HL is unaffected. ─────


@pytest.mark.asyncio
async def test_mexc_addon_confirm_forwards_position_id_to_set_leverage(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    # An open same-side long position already exists (positionId 4242). A nearby
    # liquidatePrice keeps the aggregate same-side risk negligible so the test
    # stays focused on the leverage call, not the risk gate.
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC_USDT",
                "positionType": 1,
                "openType": 1,
                "holdVol": 1.0,
                "holdAvgPrice": 100_000.0,
                "leverage": 5,
                "liquidatePrice": 99_900.0,
                "positionId": 4242,
            }
        ]
    )

    # Fake MEXC change_leverage: with an OPEN position it REQUIRES positionId,
    # else it rejects exactly like the live exchange.
    async def _set_leverage(
        symbol, leverage, open_type, position_type=None, position_id=None
    ):
        if position_id is None:
            raise MexcError(
                "change_leverage: positionId required while a position is open"
            )
        return {"success": True}

    client.set_leverage = AsyncMock(side_effect=_set_leverage)

    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    assert prev["ok"] is True
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    client.place_order.assert_called_once()
    # The existing position's id must have been resolved and forwarded.
    assert client.set_leverage.await_args.kwargs.get("position_id") == 4242


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position_id", [True, False, 0, -1, 1.5, "1.5", "abc", "9" * 5000]
)
async def test_mexc_addon_rejects_invalid_position_id(client, store, position_id):
    client.exchange_id = "mexc"
    svc = OrderService(client, _settings(trading_enabled=True), store)
    positions = [
        {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "openType": 1,
            "holdVol": 1.0,
            "positionId": position_id,
        }
    ]

    (hold, _open_type, checked), resolved_id = (
        await svc._mexc_pre_hold_and_position_id(
            "BTC_USDT", "long", positions=positions
        )
    )

    assert checked is False
    assert hold == pytest.approx(1.0)
    assert resolved_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_context",
    [
        "duplicate",
        "negative_hold",
        "unknown_margin_mode",
        "boolean_margin_mode",
        "missing_position_id",
    ],
)
async def test_mexc_confirm_blocks_invalid_existing_position_context(
    client, store, invalid_context
):
    client.exchange_id = "mexc"
    position = {
        "symbol": "BTC_USDT",
        "positionType": 1,
        "openType": 1,
        "holdVol": 1.0,
        "holdAvgPrice": 100_000.0,
        "leverage": 5,
        "liquidatePrice": 99_900.0,
        "positionId": 4242,
    }
    if invalid_context == "duplicate":
        positions = [position, dict(position, holdVol=2.0, positionId=4343)]
    elif invalid_context == "negative_hold":
        positions = [dict(position, holdVol=-1.0)]
    elif invalid_context == "boolean_margin_mode":
        positions = [dict(position, openType=True)]
    elif invalid_context == "missing_position_id":
        positions = [dict(position, positionId=None)]
    else:
        positions = [dict(position, openType=None)]
    if invalid_context == "negative_hold":
        client.positions = AsyncMock(side_effect=[[position], positions])
    else:
        client.positions = AsyncMock(return_value=positions)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    preview = await svc.preview(_good_ticket())
    assert preview["ok"] is True

    error_pattern = (
        "invalid hold_vol"
        if invalid_context == "negative_hold"
        else "position response.*ambiguous or invalid"
    )
    with pytest.raises(OrderError, match=error_pattern):
        await svc.confirm(preview["token"])

    client.set_leverage.assert_not_awaited()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_hl_confirm_leaves_set_leverage_position_id_none(client, store):
    """FINDING 1 guard: the positionId lookup is MEXC-only. A non-MEXC (HL)
    client must still be called with position_id=None (path unchanged)."""
    client.exchange_id = "hyperliquid"
    client.positions = AsyncMock(
        return_value=[]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    assert client.set_leverage.await_args.kwargs.get("position_id") is None


@pytest.mark.asyncio
async def test_hl_same_side_hold_matches_bare_coin_symbol(client, store):
    client.exchange_id = "hyperliquid"
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC",
                "positionType": 1,
                "openType": 1,
                "holdVol": 0.25,
                "holdAvgPrice": 100_000.0,
            }
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    hold, open_type, checked = await svc._same_side_hold_vol_ok(
        "BTC_USDT", "long"
    )

    assert checked is True
    assert hold == pytest.approx(0.25)
    assert open_type == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exchange_id", "expected_checked"),
    [("mexc", True), ("hyperliquid", False)],
)
async def test_same_side_hold_rejects_noncanonical_hl_quote_symbol(
    client, store, exchange_id, expected_checked
):
    client.exchange_id = exchange_id
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC_USDC",
                "positionType": 1,
                "openType": 1,
                "holdVol": 0.25,
                "holdAvgPrice": 100_000.0,
            }
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    hold, _open_type, checked = await svc._same_side_hold_vol_ok(
        "BTC_USDT", "long"
    )

    assert checked is expected_checked
    assert hold == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "positions",
    [
        None,
        {"symbol": "BTC_USDT"},
        ["not-a-position-row"],
        [
            {
                "symbol": None,
                "side": "long",
                "hold_vol": 0.25,
                "open_type": "isolated",
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "unknown",
                "hold_vol": 0.25,
                "open_type": "isolated",
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "hold_vol": "0.25",
                "open_type": "isolated",
            }
        ],
        [
            {
                "symbol": "btc_usdt",
                "side": "long",
                "hold_vol": 0.25,
                "open_type": "isolated",
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "LONG",
                "hold_vol": 0.25,
                "open_type": "isolated",
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "hold_vol": 0.25,
                "open_type": 1,
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "hold_vol": 0.25,
                "open_type": "1",
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "hold_vol": 0.0,
                "open_type": "isolated",
            }
        ],
    ],
    ids=[
        "none",
        "object-not-list",
        "non-object-row",
        "missing-symbol",
        "bad-side",
        "numeric-string-hold",
        "noncanonical-symbol",
        "noncanonical-side",
        "raw-enum-open-type",
        "numeric-string-open-type",
        "zero-hold",
    ],
)
async def test_same_side_hold_marks_malformed_positions_unreliable(
    client, store, positions
):
    client.positions = AsyncMock(return_value=positions)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    hold, _open_type, checked = await svc._same_side_hold_vol_ok(
        "BTC_USDT", "long"
    )

    assert checked is False
    assert hold == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position",
    [
        {
            "symbol": "btc_usdt",
            "positionType": 1,
            "openType": 1,
            "holdVol": 1.0,
            "positionId": 4242,
        },
        {
            "symbol": "BTC_USDT",
            "side": "LONG",
            "open_type": "isolated",
            "hold_vol": 1.0,
            "position_id": 4242,
        },
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "open_type": 1,
            "hold_vol": 1.0,
            "position_id": 4242,
        },
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "open_type": "1",
            "hold_vol": 1.0,
            "position_id": 4242,
        },
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "open_type": "isolated",
            "hold_vol": 0.0,
            "position_id": None,
        },
    ],
    ids=[
        "noncanonical-symbol",
        "noncanonical-side",
        "raw-enum-open-type",
        "numeric-string-open-type",
        "zero-hold",
    ],
)
async def test_mexc_pre_hold_rejects_noncanonical_position_model(
    client, store, position
):
    client.exchange_id = "mexc"
    svc = OrderService(client, _settings(trading_enabled=True), store)

    (hold, _open_type, checked), position_id = (
        await svc._mexc_pre_hold_and_position_id(
            "BTC_USDT", "long", positions=[position]
        )
    )

    assert checked is False
    assert hold == 0.0
    assert position_id is None


@pytest.mark.asyncio
async def test_double_confirm_fails(client, store):
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    token = prev["token"]
    await svc.confirm(token)
    with pytest.raises(OrderError) as ei:
        await svc.confirm(token)
    assert "token" in str(ei.value).lower() or "used" in str(ei.value).lower() or "invalid" in str(ei.value).lower()
    assert client.place_order.call_count == 1


@pytest.mark.asyncio
async def test_confirm_cancellation_before_submit_stops_order_send(client, store):
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket())
    leverage_started = asyncio.Event()
    release_leverage = asyncio.Event()
    leverage_finished = asyncio.Event()
    leverage_cancelled = asyncio.Event()

    async def blocked_set_leverage(*_args, **_kwargs):
        leverage_started.set()
        try:
            await release_leverage.wait()
        except asyncio.CancelledError:
            leverage_cancelled.set()
            raise
        leverage_finished.set()
        return {"success": True}

    client.set_leverage = AsyncMock(side_effect=blocked_set_leverage)
    confirm_task = asyncio.create_task(svc.confirm(prev["token"]))
    await leverage_started.wait()

    confirm_task.cancel()
    release_leverage.set()
    with pytest.raises(asyncio.CancelledError):
        await confirm_task

    assert not leverage_cancelled.is_set()
    assert leverage_finished.is_set()
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_cancellation_during_submit_finishes_protection(client, store):
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            sl_verify_attempts=1,
            sl_verify_delay_s=0.0,
        ),
        store,
    )
    prev = await svc.preview(_good_ticket())
    submit_started = asyncio.Event()
    release_submit = asyncio.Event()
    submit_cancelled = asyncio.Event()
    protection_checked = asyncio.Event()

    async def blocked_place_order(_body):
        submit_started.set()
        try:
            await release_submit.wait()
        except asyncio.CancelledError:
            submit_cancelled.set()
            raise
        return {"orderId": 12345}

    async def verified_stop_orders(_symbol):
        protection_checked.set()
        return [
            {"orderId": 777, "stopLossPrice": 99_000.0, "symbol": "BTC_USDT"}
        ]

    client.place_order = AsyncMock(side_effect=blocked_place_order)
    client.open_stop_orders = AsyncMock(side_effect=verified_stop_orders)
    confirm_task = asyncio.create_task(svc.confirm(prev["token"]))
    await submit_started.wait()

    confirm_task.cancel()
    release_submit.set()
    with pytest.raises(asyncio.CancelledError):
        await confirm_task

    assert not submit_cancelled.is_set()
    assert protection_checked.is_set()
    client.place_order.assert_awaited_once()
    client.open_stop_orders.assert_awaited_once_with("BTC_USDT")


@pytest.mark.asyncio
async def test_confirm_cancellation_keeps_trade_lock_through_reconciliation(
    client, store
):
    class ProbeLock:
        def __init__(self):
            self._lock = asyncio.Lock()
            self.waiter_started = asyncio.Event()

        async def __aenter__(self):
            if self._lock.locked():
                self.waiter_started.set()
            await self._lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._lock.release()

    lock = ProbeLock()
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            sl_verify_attempts=1,
            sl_verify_delay_s=0.0,
        ),
        store,
        trade_lock=lock,
    )
    prev = await svc.preview(_good_ticket())
    submit_started = asyncio.Event()
    release_submit = asyncio.Event()
    reconciliation_started = asyncio.Event()
    release_reconciliation = asyncio.Event()
    contender_entered = asyncio.Event()

    async def blocked_place_order(_body):
        submit_started.set()
        await release_submit.wait()
        return {"orderId": 12345}

    async def blocked_stop_reconciliation(_symbol):
        reconciliation_started.set()
        await release_reconciliation.wait()
        return [
            {"orderId": 777, "stopLossPrice": 99_000.0, "symbol": "BTC_USDT"}
        ]

    async def contend_for_money_path():
        async with lock:
            contender_entered.set()

    client.place_order = AsyncMock(side_effect=blocked_place_order)
    client.open_stop_orders = AsyncMock(side_effect=blocked_stop_reconciliation)
    confirm_task = asyncio.create_task(svc.confirm(prev["token"]))
    await submit_started.wait()

    confirm_task.cancel()
    contender_task = asyncio.create_task(contend_for_money_path())
    await lock.waiter_started.wait()
    assert not contender_entered.is_set()

    release_submit.set()
    await reconciliation_started.wait()
    assert not contender_entered.is_set()

    release_reconciliation.set()
    with pytest.raises(asyncio.CancelledError):
        await confirm_task
    await contender_task
    assert contender_entered.is_set()


@pytest.mark.asyncio
async def test_confirm_when_trading_enabled_false_fails(client, store):
    # Issue token while armed, then disarm before confirm
    armed = _settings(trading_enabled=True)
    svc_armed = OrderService(client, armed, store)
    prev = await svc_armed.preview(_good_ticket())
    assert prev["ok"] is True

    disarmed = _settings(trading_enabled=False)
    svc_off = OrderService(client, disarmed, store)
    with pytest.raises(OrderError) as ei:
        await svc_off.confirm(prev["token"])
    assert "DISARMED" in str(ei.value) or "TRADING_ENABLED" in str(ei.value)
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_preview_disarmed_no_token(client, store):
    svc = OrderService(client, _settings(trading_enabled=False), store)
    out = await svc.preview(_good_ticket())
    assert out["ok"] is False
    assert out["token"] is None
    assert any("DISARMED" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_expired_token_fails(client, store):
    svc = OrderService(
        client, _settings(trading_enabled=True, preview_token_ttl_seconds=60), store
    )
    # Manually inject expired token
    token = store.create({"ticket": _good_ticket().model_dump(), "gate": {}}, ttl=60)
    # Force expiry
    with store._lock:
        store._items[token]["expires_at"] = 0
    with pytest.raises(OrderError):
        await svc.confirm(token)
    client.place_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("timeout connecting to upstream", id="timeout"),
        pytest.param("http-503", id="http-503"),
    ],
)
async def test_confirm_unrecovered_transport_failure_is_unknown(client, store, failure):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    error = (
        MexcError("opaque upstream failure", raw={"status": 503})
        if failure == "http-503"
        else MexcError(failure)
    )
    client.place_order = AsyncMock(side_effect=error)
    client.order_by_external_oid = AsyncMock(return_value={})
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())

    with pytest.raises(OrderOutcomeUnknown, match="outcome is unknown"):
        await svc.confirm(preview["token"])

    client.place_order.assert_awaited_once()
    client.order_by_external_oid.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_explicit_exchange_rejection_is_not_unknown(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.place_order = AsyncMock(side_effect=MexcError("insufficient margin"))
    client.order_by_external_oid = AsyncMock()
    svc = OrderService(client, _settings(), store)
    preview = await svc.preview(_good_ticket())

    with pytest.raises(OrderError, match="place_order failed") as exc_info:
        await svc.confirm(preview["token"])

    assert type(exc_info.value) is OrderError
    client.order_by_external_oid.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_audit_redacts_and_bounds_untrusted_diagnostics(client, store):
    marker = "SYNTHETIC_AUDIT_SECRET_4821"
    db = MagicMock(insert_order=AsyncMock())
    svc = OrderService(
        client,
        _settings(mexc_api_key=marker, mexc_api_secret="other-synthetic-secret"),
        store,
        db=db,
    )
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    result = await svc._audit_order_best_effort(
        symbol="BTC_USDT",
        side="long",
        request_json={"apiKey": marker, "vol": 1},
        response_json={
            "body": "untrusted body " + marker,
            "nested": {"signature": marker},
            "diagnostic": "x" * 5_000,
            "rows": list(range(105)),
            "cycle": cyclic,
        },
        status="error",
        error="Authorization: Bearer " + marker,
    )

    assert result is None
    fields = db.insert_order.await_args.kwargs
    assert marker not in repr(fields)
    assert fields["request_json"]["apiKey"] == "[redacted]"
    assert fields["response_json"]["body"] == "[redacted]"
    assert fields["response_json"]["nested"]["signature"] == "[redacted]"
    assert fields["response_json"]["diagnostic"].endswith("…[truncated]")
    assert fields["response_json"]["rows"][-1] == "[truncated]"
    assert fields["response_json"]["cycle"]["self"] == "[cycle]"


@pytest.mark.asyncio
async def test_order_audit_redacts_common_credential_key_variants(client, store):
    db = MagicMock(insert_order=AsyncMock())
    svc = OrderService(client, _settings(), store, db=db)
    secrets = {
        "sessionToken": "synthetic-session-value",
        "csrf_token": "synthetic-csrf-value",
        "id-token": "synthetic-id-value",
        "cookie": "synthetic-cookie-value",
        "set_cookie": "synthetic-set-cookie-value",
        "clientCredential": "synthetic-credential-value",
        "secret_key": "synthetic-secret-key-value",
    }

    await svc._audit_order_best_effort(
        symbol="BTC_USDT",
        response_json={**secrets, "diagnosticCode": "E_SYNTHETIC"},
        status="error",
    )

    fields = db.insert_order.await_args.kwargs
    for key, value in secrets.items():
        assert fields["response_json"][key] == "[redacted]"
        assert value not in repr(fields)
    assert fields["response_json"]["diagnosticCode"] == "E_SYNTHETIC"


@pytest.mark.asyncio
async def test_order_audit_redacts_common_credential_labels_in_diagnostics(
    client, store
):
    db = MagicMock(insert_order=AsyncMock())
    svc = OrderService(client, _settings(), store, db=db)
    values = (
        "synthetic-session-value",
        "synthetic-cookie-value",
        "synthetic-credential-value",
        "synthetic-secret-key-value",
    )
    diagnostic = (
        f"sessionToken={values[0]}; Set-Cookie: {values[1]}; "
        f"clientCredential={values[2]}; secretKey: {values[3]}"
    )

    await svc._audit_order_best_effort(
        symbol="BTC_USDT",
        response_json={"diagnostic": diagnostic},
        status="error",
    )

    fields = db.insert_order.await_args.kwargs
    safe_diagnostic = fields["response_json"]["diagnostic"]
    assert safe_diagnostic.count("[redacted]") == len(values)
    assert all(value not in safe_diagnostic for value in values)


@pytest.mark.asyncio
async def test_cancel_calls_client(client, store):
    client.open_orders = AsyncMock(
        return_value=[
            {"orderId": 12345, "oid": "12345", "symbol": "BTC_USDT"}
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)
    out = await svc.cancel(order_id=12345, symbol="BTC_USDT")
    assert out["ok"] is True
    client.cancel_order.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_transport_failure_is_unknown(client, store):
    from app.mexc.errors import MexcError

    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(side_effect=MexcError("network connection lost"))
    svc = OrderService(client, _settings(), store)

    with pytest.raises(OrderOutcomeUnknown, match="Cancel outcome is unknown"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")

    client.cancel_order.assert_awaited_once_with([12345])


@pytest.mark.asyncio
async def test_cancel_cancellation_during_send_finishes_audit(client, store):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    send_cancelled = asyncio.Event()

    async def blocked_cancel_order(_body):
        send_started.set()
        try:
            await release_send.wait()
        except asyncio.CancelledError:
            send_cancelled.set()
            raise
        return {"success": True}

    client.cancel_order = AsyncMock(side_effect=blocked_cancel_order)
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(client, _settings(trading_enabled=False), store, db=db)
    cancel_task = asyncio.create_task(
        svc.cancel(order_id=12345, symbol="BTC_USDT")
    )
    await send_started.wait()

    cancel_task.cancel()
    release_send.set()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task

    assert not send_cancelled.is_set()
    client.cancel_order.assert_awaited_once_with([12345])
    db.insert_order.assert_awaited_once()
    assert db.insert_order.await_args.kwargs["status"] == "cancelled"


@pytest.mark.asyncio
async def test_hl_cancel_keeps_explicit_quote_symbols_distinct(client, store):
    client.exchange_id = "hyperliquid"
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDC"}]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="not found"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")

    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, []])
async def test_cancel_rejects_unrecognized_empty_response(client, store, response):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(return_value=response)
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="unrecognized cancel response"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
async def test_cancel_serializes_exchange_reads_with_trade_mutations(client, store):
    """A manual trigger cancel must not race an in-flight protected SL replacement."""
    lock_entered = asyncio.Event()
    release_lock = asyncio.Event()
    exchange_read = asyncio.Event()

    class GateLock:
        async def __aenter__(self):
            lock_entered.set()
            await release_lock.wait()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def open_orders(_symbol):
        exchange_read.set()
        return [{"orderId": 12345, "symbol": "BTC_USDT"}]

    client.open_orders = AsyncMock(side_effect=open_orders)
    svc = OrderService(
        client,
        _settings(trading_enabled=False),
        store,
        trade_lock=GateLock(),
    )
    operation = asyncio.create_task(
        svc.cancel(order_id=12345, symbol="BTC_USDT")
    )
    observers = [
        asyncio.create_task(lock_entered.wait()),
        asyncio.create_task(exchange_read.wait()),
    ]
    await asyncio.wait(observers, return_when=asyncio.FIRST_COMPLETED)
    serialized = lock_entered.is_set() and not exchange_read.is_set()
    release_lock.set()
    await operation
    for observer in observers:
        observer.cancel()
    await asyncio.gather(*observers, return_exceptions=True)

    assert serialized


@pytest.mark.asyncio
async def test_cancel_unknown_order_blocked(client, store):
    client.open_orders = AsyncMock(return_value=[])
    svc = OrderService(client, _settings(trading_enabled=False), store)
    with pytest.raises(OrderError) as ei:
        await svc.cancel(order_id=99999, symbol="BTC_USDT")
    assert "not found" in str(ei.value).lower()
    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("open_rows", [None, [None]], ids=["null", "non-object-row"])
async def test_cancel_rejects_malformed_open_orders_snapshot(client, store, open_rows):
    client.open_orders = AsyncMock(return_value=open_rows)
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="open orders response.*invalid"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")

    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_rejects_conflicting_open_order_id_aliases(client, store):
    client.open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 12345,
                "oid": 54321,
                "symbol": "BTC_USDT",
            }
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="invalid order identity"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")

    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_tail",
    [
        {"orderId": 22222, "oid": 33333, "symbol": "ETH_USDT"},
        {"symbol": "ETH_USDT"},
        {"oid": "12345", "symbol": "BTC_USDT"},
    ],
    ids=["conflicting-aliases", "missing-id", "duplicate-id"],
)
async def test_cancel_validates_entire_open_order_snapshot_before_send(
    client, store, malformed_tail
):
    client.open_orders = AsyncMock(
        return_value=[
            {"orderId": 12345, "symbol": "BTC_USDT"},
            malformed_tail,
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="invalid order identity"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")

    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_with_symbol_rejects_open_order_without_symbol(client, store):
    client.exchange_id = "mexc"
    client.open_orders = AsyncMock(return_value=[{"orderId": 12345}])
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="not found"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")

    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", [True, 0, -1, "abc", "1.5", "9" * 5000])
async def test_cancel_rejects_invalid_order_id_before_exchange_read(
    client, store, order_id
):
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="positive numeric"):
        await svc.cancel(order_id=order_id, symbol="BTC_USDT")

    client.open_orders.assert_not_awaited()
    client.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_rejects_hyperliquid_inner_status_error(client, store):
    client.exchange_id = "hyperliquid"
    client.open_orders = AsyncMock(
        return_value=[{"oid": 12345, "symbol": "BTC"}]
    )
    client.cancel_order = AsyncMock(
        return_value={
            "status": "ok",
            "response": {"data": {"statuses": [{"error": "order not found"}]}},
        }
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)
    with pytest.raises(OrderError, match="order not found"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
async def test_cancel_rejects_error_marker_outside_statuses(client, store):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(
        return_value=[
            {
                "orderId": 12345,
                "errorCode": 0,
                "result": {"error": "synthetic nested rejection"},
            }
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="synthetic nested rejection"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
async def test_cancel_rejects_empty_hyperliquid_inner_error_marker(client, store):
    client.exchange_id = "hyperliquid"
    client.open_orders = AsyncMock(
        return_value=[{"oid": 12345, "symbol": "BTC"}]
    )
    client.cancel_order = AsyncMock(
        return_value={
            "status": "ok",
            "response": {"data": {"statuses": [{"error": ""}]}},
        }
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="unknown exchange error"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses",
    [None, [None], ["accepted"], [{}], [{"unknown": {}}]],
    ids=["null", "null-row", "unknown-string", "empty-object", "unknown-object"],
)
async def test_cancel_rejects_malformed_hyperliquid_statuses(
    client, store, statuses
):
    client.exchange_id = "hyperliquid"
    client.open_orders = AsyncMock(
        return_value=[{"oid": 12345, "symbol": "BTC"}]
    )
    client.cancel_order = AsyncMock(
        return_value={
            "status": "ok",
            "response": {"data": {"statuses": statuses}},
        }
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="invalid exchange statuses"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"status": "ok"},
        {"status": "ok", "response": {"data": {"statuses": []}}},
    ],
    ids=["missing-statuses", "empty-statuses"],
)
async def test_cancel_rejects_hyperliquid_ok_without_status_evidence(
    client, store, response
):
    client.exchange_id = "hyperliquid"
    client.open_orders = AsyncMock(
        return_value=[{"oid": 12345, "symbol": "BTC"}]
    )
    client.cancel_order = AsyncMock(return_value=response)
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="invalid exchange statuses"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
async def test_cancel_rejects_non_object_response_list_item(client, store):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(return_value=[None])
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="unrecognized exchange response"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        [{"orderId": 99999, "errorCode": 0}],
        [
            {"orderId": 12345, "errorCode": 0},
            {"orderId": 12345, "errorCode": 0},
        ],
        {"success": True, "orderId": 99999},
    ],
    ids=["foreign-id", "duplicate-row", "foreign-id-object"],
)
async def test_cancel_rejects_success_response_with_ambiguous_identity(
    client, store, response
):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(return_value=response)
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="requested order"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["code", "errorCode", "error_code"])
@pytest.mark.parametrize("value", [False, None, 0.0])
async def test_cancel_rejects_invalid_explicit_error_code_marker(
    client, store, marker, value
):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(
        return_value=[{"orderId": 12345, marker: value}]
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match=marker):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, 0, 1, "false"])
async def test_cancel_requires_literal_true_success_marker(client, store, success):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    client.cancel_order = AsyncMock(
        return_value={"success": success, "orderId": 12345}
    )
    svc = OrderService(client, _settings(trading_enabled=False), store)

    with pytest.raises(OrderError, match="success"):
        await svc.cancel(order_id=12345, symbol="BTC_USDT")


@pytest.mark.asyncio
async def test_successful_cancel_survives_local_audit_failure(client, store):
    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    db = MagicMock()
    db.insert_order = AsyncMock(side_effect=RuntimeError("sqlite unavailable"))
    svc = OrderService(client, _settings(trading_enabled=False), store, db=db)

    out = await svc.cancel(order_id=12345, symbol="BTC_USDT")

    assert out["ok"] is True
    assert any("audit log failed" in warning for warning in out["warnings"])
    client.cancel_order.assert_awaited_once()


def test_api_cancel_rejects_invalid_symbol_before_exchange_read(client):
    from fastapi.testclient import TestClient

    from app.main import app

    client.open_orders = AsyncMock(return_value=[])
    with TestClient(app) as tc:
        app.state.mexc = client
        app.state.exchange = client
        response = tc.post(
            "/api/orders/cancel",
            json={"order_id": 12345, "symbol": "../invalid"},
        )

    assert response.status_code == 400
    assert "Invalid symbol" in response.json()["detail"]
    client.open_orders.assert_not_awaited()
    client.cancel_order.assert_not_awaited()


def test_api_cancel_normalizes_valid_symbol_before_exchange_read(client):
    from fastapi.testclient import TestClient

    from app.main import app

    client.open_orders = AsyncMock(
        return_value=[{"orderId": 12345, "symbol": "BTC_USDT"}]
    )
    with TestClient(app) as tc:
        app.state.mexc = client
        app.state.exchange = client
        response = tc.post(
            "/api/orders/cancel",
            json={"order_id": 12345, "symbol": "btc-usdt"},
        )

    assert response.status_code == 200, response.text
    client.open_orders.assert_awaited_once_with("BTC_USDT")
    client.cancel_order.assert_awaited_once()


def test_token_consume_once():
    store = PreviewStore()
    t = store.create({"a": 1}, ttl=30)
    assert store.consume(t)["a"] == 1
    with pytest.raises(TokenError):
        store.consume(t)


def test_token_payload_isolated_from_creator_mutation():
    store = PreviewStore()
    payload = {"ticket": {"vol": 1.0}}
    token = store.create(payload, ttl=30)

    payload["ticket"]["vol"] = 99.0

    assert store.consume(token)["ticket"]["vol"] == 1.0


def test_token_payload_isolated_from_peek_mutation():
    store = PreviewStore()
    token = store.create({"ticket": {"vol": 1.0}}, ttl=30)

    diagnostic = store.peek(token)
    assert diagnostic is not None
    diagnostic["ticket"]["vol"] = 99.0

    assert store.consume(token)["ticket"]["vol"] == 1.0


def test_discard_old_token_preserves_newer_single_slot_token():
    store = PreviewStore()
    old = store.create({"a": 1}, ttl=30)
    new = store.create({"a": 2}, ttl=30)

    store.discard(old)

    assert store.consume(new) == {"a": 2}


@pytest.mark.parametrize(
    "token",
    ["x" * 42, "x" * 44, "!" * 43, "x" * 10_000],
)
def test_confirm_request_rejects_noncanonical_token(token):
    with pytest.raises(ValueError):
        ConfirmRequest(token=token)


def test_confirm_request_accepts_generated_token():
    store = PreviewStore()
    token = store.create({}, ttl=60)
    assert ConfirmRequest(token=token).token == token


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ConfirmRequest, {"token": "x" * 43, "dry_run": True}),
        (CancelRequest, {"order_id": 1, "dry_run": True}),
        (
            ModifySLRequest,
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "new_sl": 99_000,
                "verify_only": True,
            },
        ),
        (
            ArmRequest,
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "rules": {},
                "auto_be": True,
            },
        ),
    ],
)
def test_mutating_request_models_reject_unknown_fields(model, payload):
    with pytest.raises(ValueError):
        model(**payload)


def test_token_ttl_cannot_be_extended_by_wall_clock_rollback(monkeypatch):
    """TTL is elapsed time; moving the system clock back must not revive it."""
    import app.orders.tokens as token_module

    wall_now = [1_000.0]
    monotonic_now = [100.0]
    monkeypatch.setattr(token_module.time, "time", lambda: wall_now[0])
    monkeypatch.setattr(token_module.time, "monotonic", lambda: monotonic_now[0])

    store = PreviewStore()
    token = store.create({"a": 1}, ttl=10)
    wall_now[0] = 1.0
    monotonic_now[0] = 111.0

    with pytest.raises(TokenError, match="expired|invalid"):
        store.consume(token)


def test_token_is_expired_at_exact_monotonic_deadline(monkeypatch):
    """A TTL ends at its deadline; equality must not leave a confirm window."""
    import app.orders.tokens as token_module

    monotonic_now = [100.0]
    monkeypatch.setattr(token_module.time, "monotonic", lambda: monotonic_now[0])

    store = PreviewStore()
    token = store.create({"a": 1}, ttl=10)
    monotonic_now[0] = 110.0

    assert store.peek(token) is None
    with pytest.raises(TokenError, match="expired|invalid"):
        store.consume(token)


@pytest.mark.asyncio
async def test_preview_blocks_existing_same_side_mexc_entry(client, store):
    """A resting entry is future exposure and must not be ignored by the gate."""
    client.exchange_id = "mexc"
    client.open_orders = AsyncMock(
        return_value=[
            {
                "orderId": 77,
                "symbol": "BTC_USDT",
                "side": 1,
                "vol": 5,
                "dealVol": 0,
            }
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    out = await svc.preview(_good_ticket())

    assert out["ok"] is False
    assert out["token"] is None
    assert any("pending" in error.lower() for error in out["errors"])


@pytest.mark.asyncio
async def test_confirm_rechecks_pending_mexc_entries_after_preview(client, store):
    """An order appearing after Preview must block Confirm's fresh recheck."""
    client.exchange_id = "mexc"
    client.open_orders = AsyncMock(return_value=[])
    svc = OrderService(client, _settings(trading_enabled=True), store)
    preview = await svc.preview(_good_ticket())
    assert preview["ok"] is True

    client.open_orders.return_value = [
        {
            "orderId": 78,
            "symbol": "BTC_USDT",
            "side": 1,
            "vol": 5,
            "dealVol": 0,
        }
    ]
    with pytest.raises(OrderError, match="pending"):
        await svc.confirm(preview["token"])

    client.place_order.assert_not_called()


# --- FastAPI route smoke (mocked client on app.state) ---


def test_api_preview_confirm_route(client, store, tmp_path, monkeypatch):
    """Route-level preview→confirm with mocked MEXC; temp DB; armed."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    # Armed trading now requires a non-empty LOCAL_API_TOKEN (fail-closed).
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEXC_API_KEY", "test-key")
    monkeypatch.setenv("MEXC_API_SECRET", "test-secret")
    get_settings.cache_clear()
    _hdr = {"X-Local-Token": "test-token"}

    from fastapi.testclient import TestClient

    from app.db.repo import Database
    from app.main import app

    monkeypatch.setenv("EXCHANGE", "mexc")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        app.state.mexc = client
        app.state.exchange = client
        app.state.preview_store = store
        app.state.db = Database(str(tmp_path / "test.db"))

        r = tc.post(
            "/api/orders/preview",
            json={
                "symbol": "BTC_USDT",
                "side": "long",
                "order_type": "limit",
                "vol": 1,
                "leverage": 5,
                "price": 100000,
                "entry": 100000,
                "stop_loss": 99000,
                "take_profit": 102000,
            },
            headers=_hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        token = body["token"]
        assert token

        r2 = tc.post("/api/orders/confirm", json={"token": token}, headers=_hdr)
        assert r2.status_code == 200, r2.text
        client.place_order.assert_called()

    get_settings.cache_clear()


def test_api_confirm_rejects_noncanonical_token_before_service(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("LOCAL_API_TOKEN", "")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "invalid-token.db"))
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        # If request validation did not stop the call, _order_service() would 503.
        tc.app.state.exchange = None
        tc.app.state.preview_store = None
        response = tc.post("/api/orders/confirm", json={"token": "!" * 43})

    assert response.status_code == 422, response.text
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("path", "payload", "method"),
    [
        ("/api/orders/confirm", {"token": "A" * 43}, "confirm"),
        (
            "/api/orders/cancel",
            {"order_id": 12345, "symbol": "BTC_USDT"},
            "cancel",
        ),
        (
            "/api/orders/close",
            {"symbol": "BTC_USDT", "side": "long", "fraction": 1.0},
            "close_position",
        ),
        (
            "/api/orders/modify-sl",
            {"symbol": "BTC_USDT", "side": "long", "new_sl": 99_000.0},
            "modify_stop_loss",
        ),
    ],
)
def test_unknown_money_outcomes_map_to_secret_free_502(
    monkeypatch, path, payload, method
):
    import app.main as main
    from fastapi.testclient import TestClient

    marker = "SYNTHETIC_PRIVATE_UPSTREAM_DETAIL"
    svc = MagicMock()
    setattr(
        svc,
        method,
        AsyncMock(side_effect=OrderOutcomeUnknown(marker)),
    )
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(path, json=payload)

    assert response.status_code == 502
    assert "outcome is unknown" in response.text
    assert marker not in response.text


@pytest.mark.parametrize(
    ("path", "payload", "method"),
    [
        ("/api/orders/confirm", {"token": "A" * 43}, "confirm"),
        (
            "/api/orders/cancel",
            {"order_id": 12345, "symbol": "BTC_USDT"},
            "cancel",
        ),
        (
            "/api/orders/close",
            {"symbol": "BTC_USDT", "side": "long", "fraction": 1.0},
            "close_position",
        ),
        (
            "/api/orders/modify-sl",
            {"symbol": "BTC_USDT", "side": "long", "new_sl": 99_000.0},
            "modify_stop_loss",
        ),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "wrapped"])
def test_money_routes_never_reflect_exchange_diagnostics(
    monkeypatch, path, payload, method, wrapped
):
    import app.main as main
    from app.mexc.errors import MexcError
    from fastapi.testclient import TestClient

    marker = "SYNTHETIC_PRIVATE_EXCHANGE_DETAIL"
    exchange_error = MexcError(marker)
    if wrapped:
        error = OrderError("operation blocked: " + marker)
        error.__cause__ = exchange_error
        expected_status = 400
    else:
        error = exchange_error
        expected_status = 502
    svc = MagicMock()
    setattr(svc, method, AsyncMock(side_effect=error))
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(path, json=payload)

    assert response.status_code == expected_status
    assert "Exchange request failed" in response.text
    assert marker not in response.text


def test_preview_route_never_reflects_wrapped_exchange_diagnostic(monkeypatch):
    import app.main as main
    from app.mexc.errors import MexcError
    from fastapi.testclient import TestClient

    marker = "SYNTHETIC_PRIVATE_PREVIEW_DETAIL"
    error = OrderError("ticker failed: " + marker)
    error.__cause__ = MexcError(marker)
    svc = MagicMock(preview=AsyncMock(side_effect=error))
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(
        "/api/orders/preview",
        json=_good_ticket().model_dump(mode="json"),
    )

    assert response.status_code == 400
    assert "Exchange request failed" in response.text
    assert marker not in response.text


def test_preview_route_rejects_numeric_string_ticket_before_service(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    svc = MagicMock(preview=AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)
    payload = _good_ticket().model_dump(mode="json")
    payload["vol"] = "1.0"

    response = TestClient(main.app).post("/api/orders/preview", json=payload)

    assert response.status_code == 422
    svc.preview.assert_not_awaited()


def test_order_route_preserves_actionable_business_error(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    svc = MagicMock(
        cancel=AsyncMock(side_effect=OrderError("order is no longer open"))
    )
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(
        "/api/orders/cancel",
        json={"order_id": 12345, "symbol": "BTC_USDT"},
    )

    assert response.status_code == 400
    assert "order is no longer open" in response.text


def test_order_route_never_reflects_semantic_exchange_rejection(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    marker = "SYNTHETIC_PRIVATE_SEMANTIC_REJECTION"
    svc = MagicMock(
        cancel=AsyncMock(
            side_effect=OrderRejectedByExchange(
                "cancel rejected by exchange: " + marker
            )
        )
    )
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(
        "/api/orders/cancel",
        json={"order_id": 12345, "symbol": "BTC_USDT"},
    )

    assert response.status_code == 400
    assert "Exchange request failed" in response.text
    assert marker not in response.text


# ── T2: preview token expiry + single-slot ──────────────────────────────────


def test_preview_token_expiry_and_single_slot():
    from app.orders.tokens import TokenError

    store = PreviewStore()
    first = store.create({"a": 1}, ttl=60)
    second = store.create({"a": 2}, ttl=60)
    # single slot: creating a second token invalidates the first
    with pytest.raises(TokenError):
        store.consume(first)
    assert store.consume(second)["a"] == 2
    # expiry: a past expires_at raises on consume
    third = store.create({"a": 3}, ttl=60)
    with store._lock:
        store._items[third]["expires_at"] = 0
    with pytest.raises(TokenError):
        store.consume(third)


# ── T4: same-side risk aggregates multiple open positions ───────────────────


def test_estimate_same_side_risk_aggregates_multiple():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 2.0,
         "entry_price": 100.0, "liquidate_price": 90.0},
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": 80.0},
        {"symbol": "BTC_USDT", "side": "short", "hold_vol": 5.0,
         "entry_price": 100.0, "liquidate_price": 120.0},
        {"symbol": "ETH_USDT", "side": "long", "hold_vol": 9.0,
         "entry_price": 100.0, "liquidate_price": 90.0},
    ]
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0
    )
    assert total == pytest.approx(40.0)
    assert warnings == []


def test_aggregate_uses_sl_distance_when_present():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 2.0,
         "entry_price": 100.0, "liquidate_price": 90.0, "stop_loss": 95.0},
    ]
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0
    )
    assert total == pytest.approx(10.0)
    assert warnings == []


@pytest.mark.parametrize(
    ("side", "stop_loss", "liquidate_price"),
    [
        ("long", 110.0, 50.0),
        ("short", 90.0, 150.0),
    ],
)
def test_aggregate_does_not_credit_wrong_side_stop_as_protection(
    side, stop_loss, liquidate_price
):
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {
            "symbol": "BTC_USDT",
            "side": side,
            "hold_vol": 1.0,
            "entry_price": 100.0,
            "liquidate_price": liquidate_price,
            "stop_loss": stop_loss,
        },
    ]

    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side=side, contract_size=1.0
    )

    assert total == pytest.approx(50.0)
    assert warnings == []


def test_aggregate_does_not_trust_numeric_string_stop_distance():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "hold_vol": 1.0,
            "entry_price": 100.0,
            "liquidate_price": 80.0,
            "stop_loss": "95.0",
        },
    ]

    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0
    )

    assert total == pytest.approx(20.0)
    assert warnings == []


def test_aggregate_uses_full_liq_distance_without_stop():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": 50.0},
    ]
    total, warnings = estimate_same_side_risk_usdt(
        positions, symbol="BTC_USDT", side="long", contract_size=1.0
    )
    assert total == pytest.approx(50.0)
    assert warnings == []


@pytest.mark.parametrize(
    "contract_size",
    [0.0, -1.0, float("nan"), float("inf"), True, "0.0001", 10**400],
)
def test_existing_risk_rejects_invalid_contract_size(contract_size):
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {
            "symbol": "BTC_USDT",
            "side": "long",
            "hold_vol": 1.0,
            "entry_price": 100.0,
            "liquidate_price": 80.0,
        }
    ]

    with pytest.raises(ValueError, match="contract_size"):
        estimate_same_side_risk_usdt(
            positions,
            symbol="BTC_USDT",
            side="long",
            contract_size=contract_size,
        )


def test_missing_liq_always_blocks():
    from app.orders.service import estimate_same_side_risk_usdt

    positions = [
        {"symbol": "BTC_USDT", "side": "long", "hold_vol": 1.0,
         "entry_price": 100.0, "liquidate_price": None},
    ]
    with pytest.raises(ValueError, match="liquidate_price"):
        estimate_same_side_risk_usdt(
            positions, symbol="BTC_USDT", side="long", contract_size=1.0
        )


@pytest.mark.parametrize(
    "liquidate_price",
    [float("inf"), True, {"value": 80.0}, "not-a-number", "80.0"],
)
def test_existing_risk_rejects_invalid_liquidation_price(liquidate_price):
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 1.0,
        "entry_price": 100.0,
        "liquidate_price": liquidate_price,
    }

    with pytest.raises(ValueError, match="liquidate_price"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side="long", contract_size=1.0
        )


def test_existing_risk_rejects_zero_liquidation_distance():
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 1.0,
        "entry_price": 100.0,
        "liquidate_price": 100.0,
    }

    with pytest.raises(ValueError, match="liquidate_price"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side="long", contract_size=1.0
        )


@pytest.mark.parametrize(
    ("side", "liquidate_price"),
    [("long", 110.0), ("short", 90.0)],
)
def test_existing_risk_rejects_wrong_side_liquidation_price(
    side, liquidate_price
):
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": side,
        "hold_vol": 1.0,
        "entry_price": 100.0,
        "liquidate_price": liquidate_price,
    }

    with pytest.raises(ValueError, match="liquidate_price.*wrong side"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side=side, contract_size=1.0
        )


@pytest.mark.parametrize("field", ["hold_vol", "entry_price"])
@pytest.mark.parametrize("bad_value", [float("nan"), "1.0"])
def test_existing_risk_rejects_invalid_position_geometry(field, bad_value):
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 1.0,
        "entry_price": 100.0,
        "liquidate_price": 80.0,
    }
    position[field] = bad_value

    with pytest.raises(ValueError, match=f"invalid {field}"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side="long", contract_size=1.0
        )


@pytest.mark.parametrize("hold_vol", [-1.0, 0.0], ids=["negative", "zero"])
def test_existing_risk_rejects_nonpositive_position_hold(hold_vol):
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": hold_vol,
        "entry_price": 100.0,
        "liquidate_price": 80.0,
    }

    with pytest.raises(ValueError, match="invalid hold_vol"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side="long", contract_size=1.0
        )


@pytest.mark.parametrize(
    ("position_symbol", "position_side", "allow_base_symbol_alias"),
    [
        ("btc_usdt", "long", False),
        ("BTC_USDT", "long", True),
        ("BTC_USDT", "LONG", False),
    ],
    ids=["noncanonical-mexc", "quoted-hyperliquid", "noncanonical-side"],
)
def test_existing_risk_rejects_noncanonical_position_identity(
    position_symbol, position_side, allow_base_symbol_alias
):
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": position_symbol,
        "side": position_side,
        "hold_vol": 1.0,
        "entry_price": 100.0,
        "liquidate_price": 80.0,
    }

    with pytest.raises(ValueError, match="identity"):
        estimate_same_side_risk_usdt(
            [position],
            symbol="BTC_USDT",
            side="long",
            contract_size=1.0,
            allow_base_symbol_alias=allow_base_symbol_alias,
        )


@pytest.mark.parametrize("field", ["hold_vol", "entry_price", "liquidate_price"])
def test_existing_risk_maps_overflowed_position_numeric_to_validation_error(field):
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 1.0,
        "entry_price": 100.0,
        "liquidate_price": 80.0,
    }
    position[field] = 10**400

    with pytest.raises(ValueError, match=f"invalid.*{field}"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side="long", contract_size=1.0
        )


def test_existing_risk_rejects_nonfinite_position_risk_product():
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 4.0,
        "entry_price": 1e308,
        "liquidate_price": 5e307,
    }

    with pytest.raises(ValueError, match="calculated risk is non-finite"):
        estimate_same_side_risk_usdt(
            [position], symbol="BTC_USDT", side="long", contract_size=1.0
        )


def test_existing_risk_rejects_nonfinite_aggregate_risk_sum():
    from app.orders.service import estimate_same_side_risk_usdt

    position = {
        "symbol": "BTC_USDT",
        "side": "long",
        "hold_vol": 2.0,
        "entry_price": 1e308,
        "liquidate_price": 5e307,
    }

    with pytest.raises(ValueError, match="aggregate risk is non-finite"):
        estimate_same_side_risk_usdt(
            [position, dict(position)],
            symbol="BTC_USDT",
            side="long",
            contract_size=1.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [{"symbol": None, "side": "long"}, {"symbol": "BTC_USDT", "side": "unknown"}],
    ids=["missing-symbol", "unknown-side"],
)
async def test_preview_blocks_position_with_unverified_identity(
    client, store, identity
):
    position = {
        "hold_vol": 1.0,
        "entry_price": 100_000.0,
        "liquidate_price": 90_000.0,
        **identity,
    }
    client.positions = AsyncMock(return_value=[position])
    svc = OrderService(client, _settings(trading_enabled=True), store)

    preview = await svc.preview(_good_ticket())

    assert preview["ok"] is False
    assert preview["token"] is None
    assert any("identity" in error for error in preview["errors"])


@pytest.mark.asyncio
async def test_hl_preview_counts_bare_coin_existing_risk(client, store):
    client.exchange_id = "hyperliquid"
    client.contract_meta = AsyncMock(
        return_value=ContractMeta(
            symbol="BTC_USDT",
            contract_size=1.0,
            price_unit=0.1,
            vol_unit=0.001,
            min_vol=0.001,
            max_vol=1_000_000.0,
            max_leverage=125,
            api_allowed=True,
            state=0,
        )
    )
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC",
                "side": "long",
                "hold_vol": 0.2,
                "entry_price": 100_000.0,
                "liquidate_price": 90_000.0,
                "open_type": "isolated",
            }
        ]
    )
    svc = OrderService(client, _settings(exchange="hyperliquid"), store)

    preview = await svc.preview(
        _good_ticket(
            order_type="market", price=None, vol=0.001, take_profit=103_000.0
        )
    )

    assert preview["ok"] is False
    assert preview["token"] is None
    assert any("MAX_RISK_PCT" in error for error in preview["errors"]), preview


# ── T5: confirm while disarmed consumes the token (no reuse after arming) ────


@pytest.mark.asyncio
async def test_confirm_when_disarmed_consumes_token(client, store):
    from app.orders.tokens import TokenError

    armed = _settings(trading_enabled=True)
    svc_armed = OrderService(client, armed, store)
    prev = await svc_armed.preview(_good_ticket())
    token = prev["token"]
    assert token

    disarmed = _settings(trading_enabled=False)
    svc_off = OrderService(client, disarmed, store)
    with pytest.raises(OrderError) as ei:
        await svc_off.confirm(token)
    assert "DISARMED" in str(ei.value) or "TRADING_ENABLED" in str(ei.value)
    client.place_order.assert_not_called()
    # Token was consumed -> cannot be reused after arming without a fresh preview
    with pytest.raises(TokenError):
        store.consume(token)


# ── Projekt H / Task 1: modify_stop_loss (money-critical) ────────────────────


def _modify_client(**over) -> MagicMock:
    """Mock client for modify-SL: same-side long hold 0.01 @ mark 100k."""
    c = MagicMock()
    exchange_id = over.pop("exchange_id", "hyperliquid")
    position_symbol = "BTC" if exchange_id == "hyperliquid" else "BTC_USDT"
    c.exchange_id = exchange_id
    c.ticker = AsyncMock(
        return_value=Ticker(symbol=position_symbol, last_price=100_000.0)
    )
    c.contract_meta = AsyncMock(
        return_value=_contract().model_copy(update={"symbol": position_symbol})
    )
    c.positions = AsyncMock(
        return_value=[{"symbol": position_symbol, "side": "long",
                       "hold_vol": 0.01, "open_type": "isolated"}]
    )
    old = {"orderId": 111, "symbol": position_symbol, "orderType": "Stop",
           "triggerPrice": 98_000.0}
    new = {"orderId": 555, "symbol": position_symbol, "orderType": "Stop",
           "triggerPrice": 99_000.0, "vol": 0.01}
    # call #1 existing-scan -> [old]; call #2/#3 verify -> [new]
    c.open_stop_orders = AsyncMock(side_effect=[[old], [new], [new]])
    c.place_stop_order = AsyncMock(
        return_value={"orderId": 555, "error": None, "requestedTrigger": 99_000.0,
                      "symbol": position_symbol}
    )
    c.cancel_order = AsyncMock(return_value={"success": True})
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _modify_settings(**kw) -> Settings:
    return _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0, **kw)


@pytest.mark.asyncio
async def test_modify_sl_places_new_then_cancels_old(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["ok"] is True
    assert out["status"] == "modify_sl_ok"
    assert out["new_oid"] == 555
    assert out["cancelled_old"] == [111]
    c.place_stop_order.assert_awaited_once()
    c.cancel_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_modify_sl_cancellation_during_send_finishes_replacement(store):
    c = _modify_client()
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    send_cancelled = asyncio.Event()

    async def blocked_place_stop(*_args, **_kwargs):
        send_started.set()
        try:
            await release_send.wait()
        except asyncio.CancelledError:
            send_cancelled.set()
            raise
        return {
            "orderId": 555,
            "error": None,
            "requestedTrigger": 99_000.0,
            "symbol": "BTC",
        }

    c.place_stop_order = AsyncMock(side_effect=blocked_place_stop)
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(c, _modify_settings(), store, db=db)
    modify_task = asyncio.create_task(
        svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )
    )
    await send_started.wait()

    modify_task.cancel()
    release_send.set()
    with pytest.raises(asyncio.CancelledError):
        await modify_task

    assert not send_cancelled.is_set()
    c.place_stop_order.assert_awaited_once()
    c.cancel_order.assert_awaited_once()
    db.insert_order.assert_awaited_once()
    assert db.insert_order.await_args.kwargs["status"] == "modify_sl_ok"


@pytest.mark.asyncio
async def test_manual_modify_records_override_before_trade_lock_releases(
    store, monkeypatch
):
    class TrackingLock:
        active = False

        async def __aenter__(self):
            self.active = True

        async def __aexit__(self, exc_type, exc, tb):
            self.active = False

    lock = TrackingLock()

    async def record_override(symbol, side):
        assert lock.active is True
        assert (symbol, side) == ("BTC_USDT", "long")

    db = MagicMock()
    db.set_user_override_hw = AsyncMock(side_effect=record_override)
    svc = OrderService(
        _modify_client(),
        _modify_settings(),
        store,
        db=db,
        trade_lock=lock,
    )
    inner = AsyncMock(return_value={"verified": True, "warnings": []})
    monkeypatch.setattr(svc, "_modify_stop_loss_locked", inner)

    await svc.modify_stop_loss(
        symbol="BTC_USDT",
        side="long",
        new_sl=99_000.0,
        record_user_override=True,
    )

    db.set_user_override_hw.assert_awaited_once_with("BTC_USDT", "long")
    assert lock.active is False

    inner.return_value = {"verified": False, "warnings": []}
    await svc.modify_stop_loss(
        symbol="BTC_USDT",
        side="long",
        new_sl=99_000.0,
        record_user_override=True,
    )
    assert db.set_user_override_hw.await_count == 1

    inner.return_value = {"verified": True, "warnings": []}
    db.set_user_override_hw.side_effect = RuntimeError("synthetic DB failure")
    result = await svc.modify_stop_loss(
        symbol="BTC_USDT",
        side="long",
        new_sl=99_000.0,
        record_user_override=True,
    )
    assert any("override marker could not be saved" in item for item in result["warnings"])
    assert lock.active is False


@pytest.mark.asyncio
async def test_modify_sl_new_placement_failure_keeps_old(store):
    from app.hyperliquid.errors import HyperliquidError
    marker = "SYNTHETIC_PRIVATE_STOP_EXCEPTION"
    c = _modify_client(
        place_stop_order=AsyncMock(side_effect=HyperliquidError(marker))
    )
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "old SL left in place" in str(ei.value)
    assert marker not in str(ei.value)
    c.cancel_order.assert_not_called()  # old stop untouched


@pytest.mark.asyncio
async def test_modify_sl_transport_failure_is_unknown_and_keeps_old(store):
    from app.hyperliquid.errors import HyperliquidError

    c = _modify_client(
        place_stop_order=AsyncMock(side_effect=HyperliquidError("request timed out"))
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderOutcomeUnknown, match="stop placement outcome is unknown"):
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)

    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_place_rejected_keeps_old(store):
    marker = "SYNTHETIC_PRIVATE_STOP_REJECTION"
    c = _modify_client(place_stop_order=AsyncMock(
        return_value={"orderId": None, "error": marker}))
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(c, _modify_settings(), store, db=db)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "rejected" in str(ei.value)
    assert marker not in str(ei.value)
    db.insert_order.assert_awaited_once()
    assert marker not in str(db.insert_order.await_args)
    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_conflicting_placement_oid_aliases_keep_old(store):
    c = _modify_client(
        place_stop_order=AsyncMock(
            return_value={"orderId": 555, "oid": 999, "error": None}
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="invalid response"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    assert c.open_stop_orders.await_count == 1
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_explicit_placement_error_with_oid_keeps_old(store):
    marker = "SYNTHETIC_PRIVATE_STOP_ERROR_WITH_OID"
    c = _modify_client(
        place_stop_order=AsyncMock(
            return_value={"orderId": 555, "error": marker}
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="exchange rejected the replacement stop") as ei:
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    assert marker not in str(ei.value)
    assert c.open_stop_orders.await_count == 1
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_verify_timeout_keeps_old(store):
    c = _modify_client()
    # existing-scan -> [old]; verify attempts see NO matching stop.
    c.open_stop_orders = AsyncMock(side_effect=[[{"orderId": 111, "symbol": "BTC_USDT",
                                                  "orderType": "Stop",
                                                  "triggerPrice": 98_000.0}], [], []])
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["verified"] is False
    assert out["status"] == "modify_sl_unverified_old_kept"
    c.cancel_order.assert_not_called()  # never cancel on doubt


@pytest.mark.asyncio
async def test_modify_sl_verify_error_detail_is_not_reflected(store):
    from app.hyperliquid.errors import HyperliquidError

    marker = "SYNTHETIC_PRIVATE_STOP_VERIFY_DETAIL"
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    c.open_stop_orders = AsyncMock(
        side_effect=[[old], HyperliquidError(marker)]
    )
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["status"] == "modify_sl_unknown_old_kept"
    assert marker not in str(out)
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_conflicting_new_oid_aliases_keep_old_stop(store):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    conflicting_new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
        "raw": {"oid": 999},
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [conflicting_new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unknown_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_does_not_cancel_old_stop_with_conflicting_oid_aliases(store):
    c = _modify_client()
    conflicting_old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
        "raw": {"oid": 222},
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[conflicting_old], [new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is True
    assert out["cancelled_old"] == []
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_invalid_raw_oid_keeps_old_stop_without_parser_error(store):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    malformed_new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
        "raw": {"oid": "²"},
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [malformed_new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unknown_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_cancels_duplicate_old_oid_only_once(store):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old, dict(old)], [new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["status"] == "modify_sl_ok"
    assert out["cancelled_old"] == [111]
    c.cancel_order.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_symbol", [None, "ETH_USDT", "BTC_USDC"])
async def test_modify_sl_verify_requires_matching_symbol(store, reported_symbol):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    reported_new = {
        "orderId": 555,
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    if reported_symbol is not None:
        reported_new["symbol"] = reported_symbol
    c.open_stop_orders = AsyncMock(side_effect=[[old], [reported_new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unknown_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_does_not_cancel_symbol_less_existing_order(store):
    c = _modify_client()
    unknown_old = {
        "orderId": 111,
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[unknown_old], [new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is True
    assert out["cancelled_old"] == []
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_symbol", ["ETH_USDT", "BTC_USDC"])
async def test_modify_sl_ignores_other_symbol_when_classifying_existing_sl(
    store, other_symbol
):
    c = _modify_client()
    other_stop = {
        "orderId": 111,
        "symbol": other_symbol,
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[other_stop], [new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["ok"] is True
    assert out["verified"] is True
    assert out["cancelled_old"] == []
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_preserves_unclassified_same_symbol_trigger(store):
    c = _modify_client()
    unknown_trigger = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "plan",
        "triggerPrice": 99_500.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[unknown_trigger], [new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is True
    assert out["cancelled_old"] == []
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_cancel_old_failure_two_stops(store):
    marker = "SYNTHETIC_PRIVATE_OLD_STOP_CANCEL_DETAIL"
    c = _modify_client(cancel_order=AsyncMock(side_effect=RuntimeError(marker)))
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["status"] == "modify_sl_ok_old_cancel_failed"
    assert 111 in out["failed_cancel"]
    assert any("OVER-protected" in w for w in out["warnings"])
    assert marker not in str(out)


@pytest.mark.asyncio
async def test_modify_sl_inner_cancel_error_keeps_old_as_failed(store):
    marker = "SYNTHETIC_PRIVATE_INNER_CANCEL_DETAIL"
    c = _modify_client(
        cancel_order=AsyncMock(
            return_value={
                "status": "ok",
                "response": {"data": {"statuses": [{"error": marker}]}},
            }
        )
    )
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )
    assert out["status"] == "modify_sl_ok_old_cancel_failed"
    assert out["cancelled_old"] == []
    assert out["failed_cancel"] == [111]
    assert marker not in str(out)


@pytest.mark.asyncio
async def test_autonomous_modify_rechecks_arm_state_after_lock(tmp_path, store):
    """A disarm while the monitor waits on the lock cancels the stale move."""
    from app.db.repo import Database

    db = Database(str(tmp_path / "arm_recheck.db"))
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100_000.0,
        initial_sl_snap=98_000.0,
        r1=2_000.0,
        opened_at=1_700_000_000_000,
        invalidation_price=None,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})

    lock = asyncio.Lock()
    await lock.acquire()
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store, db=db, trade_lock=lock)
    pending = asyncio.create_task(
        svc.modify_stop_loss(
            symbol="BTC_USDT",
            side="long",
            new_sl=99_000.0,
            required_armed_rule="auto_be",
        )
    )
    await asyncio.sleep(0)
    await db.disarm_all()
    lock.release()

    with pytest.raises(OrderError, match="no longer armed"):
        await pending
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_autonomous_modify_rejects_reopened_position_after_lock(
    tmp_path, store
):
    """A queued old-epoch stop must never be applied to a reopened position."""
    from app.db.repo import Database

    old_epoch = 1_700_000_000_000
    new_epoch = old_epoch + 60_000
    db = Database(str(tmp_path / "reopen_recheck.db"))
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100_000.0,
        initial_sl_snap=98_000.0,
        r1=2_000.0,
        opened_at=old_epoch,
        invalidation_price=None,
        open_sig=old_epoch,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})

    entered = asyncio.Event()
    release = asyncio.Event()

    class GateLock:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    c = _modify_client()
    c.user_fills = AsyncMock(
        return_value=[
            {
                "symbol": "BTC",
                "dir": "Open Long",
                "start_position": 0.0,
                "time": new_epoch,
            }
        ]
    )
    svc = OrderService(c, _modify_settings(), store, db=db, trade_lock=GateLock())
    pending = asyncio.create_task(
        svc.modify_stop_loss(
            symbol="BTC_USDT",
            side="long",
            new_sl=99_000.0,
            required_armed_rule="auto_be",
            expected_position_signature=old_epoch,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
    finally:
        release.set()

    with pytest.raises(OrderError, match="position changed while waiting"):
        await pending
    c.user_fills.assert_awaited_once_with("BTC_USDT", fresh=True)
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_autonomous_modify_blocks_when_position_identity_is_unavailable(
    tmp_path, store
):
    from app.db.repo import Database

    db = Database(str(tmp_path / "missing_identity.db"))
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100_000.0,
        initial_sl_snap=98_000.0,
        r1=2_000.0,
        opened_at=1_700_000_000_000,
        invalidation_price=None,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store, db=db)

    with pytest.raises(OrderError, match="position identity is unavailable"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT",
            side="long",
            new_sl=99_000.0,
            required_armed_rule="auto_be",
            expected_position_signature=None,
        )

    c.positions.assert_not_awaited()
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_autonomous_modify_accepts_matching_fresh_position_identity(
    tmp_path, store
):
    from app.db.repo import Database

    epoch = 1_700_000_000_000
    db = Database(str(tmp_path / "matching_identity.db"))
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100_000.0,
        initial_sl_snap=98_000.0,
        r1=2_000.0,
        opened_at=epoch,
        invalidation_price=None,
        open_sig=epoch,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_be": True})
    c = _modify_client()
    c.user_fills = AsyncMock(
        return_value=[
            {
                "symbol": "BTC",
                "dir": "Open Long",
                "start_position": 0.0,
                "time": epoch,
            }
        ]
    )
    svc = OrderService(c, _modify_settings(), store, db=db)

    result = await svc.modify_stop_loss(
        symbol="BTC_USDT",
        side="long",
        new_sl=99_000.0,
        required_armed_rule="auto_be",
        expected_position_signature=epoch,
    )

    assert result["verified"] is True
    c.user_fills.assert_awaited_once_with("BTC_USDT", fresh=True)
    c.place_stop_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_waiting_auto_trail_rechecks_manual_override_after_lock(
    tmp_path, store
):
    """A queued trail cannot overwrite a manual stop move made ahead of it."""
    from app.db.repo import Database

    db = Database(str(tmp_path / "override_recheck.db"))
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100_000.0,
        initial_sl_snap=98_000.0,
        r1=2_000.0,
        opened_at=1_700_000_000_000,
        invalidation_price=None,
    )
    await db.set_armed_rules("BTC_USDT", "long", {"auto_trail": True})

    entered = asyncio.Event()
    release = asyncio.Event()

    class GateLock:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store, db=db, trade_lock=GateLock())
    pending = asyncio.create_task(
        svc.modify_stop_loss(
            symbol="BTC_USDT",
            side="long",
            new_sl=99_000.0,
            required_armed_rule="auto_trail",
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await db.update_high_water("BTC_USDT", "long", 110_000.0)
        await db.set_user_override_hw("BTC_USDT", "long")
    finally:
        release.set()

    with pytest.raises(OrderError, match="paused by a manual stop override"):
        await pending
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_auto_trail_blocks_invalid_manual_override_state(store):
    db = MagicMock()
    db.get_open_position_mgmt = AsyncMock(
        return_value={
            "armed_rules": {"auto_trail": True},
            "high_water": 110_000.0,
            "user_override_hw": float("nan"),
        }
    )
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store, db=db)

    with pytest.raises(OrderError, match="override state is invalid"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT",
            side="long",
            new_sl=99_000.0,
            required_armed_rule="auto_trail",
        )

    c.place_stop_order.assert_not_called()


def test_modify_sl_route_records_manual_override(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    svc = MagicMock()
    svc.modify_stop_loss = AsyncMock(
        return_value={"ok": True, "verified": True, "warnings": []}
    )
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(
        "/api/orders/modify-sl",
        json={"symbol": "BTC_USDT", "side": "long", "new_sl": 99_000.0},
    )

    assert response.status_code == 200
    svc.modify_stop_loss.assert_awaited_once_with(
        symbol="BTC_USDT",
        side="long",
        new_sl=99_000.0,
        record_user_override=True,
    )


def test_modify_sl_route_rejects_numeric_string_before_service(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    svc = MagicMock()
    svc.modify_stop_loss = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(
        "/api/orders/modify-sl",
        json={"symbol": "BTC_USDT", "side": "long", "new_sl": "99000.0"},
    )

    assert response.status_code == 422
    svc.modify_stop_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_small_step_verifies_by_oid_not_price(store):
    # New SL only $100 from the old one (< 0.15% => a price-only verify would
    # accept the OLD, unchanged stop as "the new SL"). OID verify must gate on
    # the concrete new oid, never on price alone.
    old = {"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 99_000.0}
    new = {"orderId": 556, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 99_100.0, "vol": 0.01}
    placed = {"orderId": 556, "error": None, "requestedTrigger": 99_100.0,
              "symbol": "BTC"}

    # (A) new oid rests alongside the price-close old -> old IS cancelled.
    c = _modify_client()
    c.open_stop_orders = AsyncMock(side_effect=[[old], [old, new], [old, new]])
    c.place_stop_order = AsyncMock(return_value=placed)
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_100.0)
    assert out["verified"] is True
    assert out["cancelled_old"] == [111]

    # (B) new oid does NOT rest — only the old, price-close stop is present.
    # Price-only verify would falsely pass; OID verify keeps the old stop.
    c2 = _modify_client()
    c2.open_stop_orders = AsyncMock(side_effect=[[old], [old], [old]])
    c2.place_stop_order = AsyncMock(return_value=placed)
    svc2 = OrderService(c2, _modify_settings(), store)
    out2 = await svc2.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_100.0)
    assert out2["verified"] is False
    assert out2["status"] == "modify_sl_unverified_old_kept"
    c2.cancel_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_tail",
    [
        {
            "orderId": 777,
            "oid": 888,
            "symbol": "BTC_USDT",
            "orderType": "Stop",
            "triggerPrice": 97_000.0,
        },
        {
            "symbol": "BTC_USDT",
            "orderType": "Stop",
            "triggerPrice": 97_000.0,
        },
        {
            "oid": "555",
            "symbol": "BTC_USDT",
            "orderType": "Stop",
            "triggerPrice": 99_000.0,
            "vol": 0.01,
        },
        {
            "orderId": 777,
            "symbol": "BTC_USDT",
            "orderType": "Stop",
            "triggerPrice": 97_000.0,
            "trigger_price": 96_000.0,
        },
        {
            "orderId": 777,
            "symbol": "ETH_USDT",
            "orderType": "Stop",
            "triggerPrice": 97_000.0,
        },
    ],
    ids=[
        "conflicting-aliases",
        "missing-id",
        "duplicate-id",
        "conflicting-trigger-aliases",
        "foreign-symbol",
    ],
)
async def test_modify_sl_verification_validates_entire_stop_snapshot(
    store, malformed_tail
):
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c = _modify_client()
    c.open_stop_orders = AsyncMock(side_effect=[[old], [new, malformed_tail]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unknown_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_same_oid_wrong_price_keeps_old_stop(store):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    wrong_new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 90_000.0,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [wrong_new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unverified_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_conflicting_trigger_aliases_keep_old_stop(store):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    conflicting_new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "trigger_price": 90_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [conflicting_new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unknown_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_same_oid_take_profit_keeps_old_stop(store):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    wrong_new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Take Profit",
        "triggerPrice": 99_000.0,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [wrong_new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unverified_old_kept"
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_vol", [None, 0.005])
async def test_modify_sl_same_oid_without_full_coverage_keeps_old_stop(
    store, reported_vol
):
    c = _modify_client()
    old = {
        "orderId": 111,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": reported_vol,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [new]])
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unverified_old_kept"
    assert any("coverage" in warning.lower() for warning in out["warnings"])
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_growth_during_verification_keeps_old_stop(store):
    position_001 = [
        {"symbol": "BTC", "side": "long", "hold_vol": 0.01,
         "open_type": "isolated"}
    ]
    position_002 = [
        {"symbol": "BTC", "side": "long", "hold_vol": 0.02,
         "open_type": "isolated"}
    ]
    c = _modify_client(
        positions=AsyncMock(
            side_effect=[position_001, position_001, position_002]
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["verified"] is False
    assert out["status"] == "modify_sl_unverified_old_kept"
    assert any("current position" in warning.lower() for warning in out["warnings"])
    assert c.positions.await_count == 3
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_rejects_duplicate_same_side_positions(store):
    duplicate_positions = [
        {"symbol": "BTC", "side": "long", "hold_vol": 0.01,
         "open_type": "isolated"},
        {"symbol": "BTC", "side": "long", "hold_vol": 0.02,
         "open_type": "isolated"},
    ]
    c = _modify_client(
        positions=AsyncMock(return_value=duplicate_positions)
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="positions lookup failed"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_geometry_long_rejected(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=101_000.0)
    assert "BELOW mark" in str(ei.value)
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mark",
    [float("nan"), float("inf"), float("-inf"), True, "100000.0", 10**400],
)
async def test_modify_sl_rejects_invalid_mark_before_order_reads(store, mark):
    c = _modify_client(
        ticker=AsyncMock(
            return_value=MagicMock(symbol="BTC_USDT", last_price=mark)
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="mark price unavailable"):
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)

    c.contract_meta.assert_not_awaited()
    c.open_stop_orders.assert_not_awaited()
    c.place_stop_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price_unit", [True, "0.1", 10**400])
async def test_modify_sl_rejects_invalid_price_unit_before_stop_reads(
    store, bad_price_unit
):
    contract = _contract()
    contract.price_unit = bad_price_unit
    c = _modify_client(contract_meta=AsyncMock(return_value=contract))
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="price unit"):
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)

    c.open_stop_orders.assert_not_awaited()
    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_sl", [float("nan"), float("inf"), float("-inf")])
async def test_modify_sl_rejects_nonfinite_before_exchange_reads(store, new_sl):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="finite"):
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=new_sl)

    c.positions.assert_not_awaited()
    c.ticker.assert_not_awaited()
    c.place_stop_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_blocks_when_finite_price_step_count_overflows(store):
    contract = _contract()
    contract.price_unit = 1e-308
    c = _modify_client(
        ticker=AsyncMock(
            return_value=Ticker(symbol="BTC_USDT", last_price=1e101)
        ),
        contract_meta=AsyncMock(return_value=contract),
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="rounded SL is invalid"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=1e100
        )

    c.open_stop_orders.assert_not_awaited()
    c.place_stop_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_sl", [True, "99000.0"])
async def test_modify_sl_rejects_untyped_price_before_exchange_reads(store, new_sl):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="numeric"):
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=new_sl)

    c.positions.assert_not_awaited()
    c.ticker.assert_not_awaited()
    c.place_stop_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("fraction", float("nan")),
        ("fraction", float("inf")),
        ("fraction", -0.1),
        ("fraction", 0.0),
        ("fraction", 1.1),
        ("fraction", True),
        ("fraction", "0.5"),
        ("vol", float("nan")),
        ("vol", float("inf")),
        ("vol", -1.0),
        ("vol", 0.0),
        ("vol", True),
        ("vol", "0.5"),
    ],
)
async def test_close_rejects_invalid_explicit_amount_before_exchange_reads(
    store, field, bad
):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError):
        await svc.close_position(symbol="BTC_USDT", side="long", **{field: bad})

    c.positions.assert_not_awaited()


@pytest.mark.parametrize("field", ["vol", "fraction"])
def test_close_route_rejects_numeric_string_amount_before_service(monkeypatch, field):
    import app.main as main
    from fastapi.testclient import TestClient

    svc = MagicMock()
    svc.close_position = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(main, "_order_service", lambda _request: svc)

    response = TestClient(main.app).post(
        "/api/orders/close",
        json={"symbol": "BTC_USDT", "side": "long", field: "0.5"},
    )

    assert response.status_code == 422
    svc.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_rejects_ambiguous_vol_and_fraction_before_exchange_reads(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="either vol or fraction"):
        await svc.close_position(
            symbol="BTC_USDT", side="long", vol=0.01, fraction=0.5
        )

    c.positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_hl_close_keeps_explicit_quote_symbols_distinct(client, store):
    client.exchange_id = "hyperliquid"
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC_USDC",
                "side": "long",
                "hold_vol": 1.0,
                "open_type": "isolated",
            }
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderError, match="no open long position on BTC_USDT"):
        await svc.close_position(
            symbol="BTC_USDT", side="long", fraction=0.5
        )

    client.contract_meta.assert_not_awaited()
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [None, [None]], ids=["null", "non-object-row"])
async def test_close_rejects_malformed_initial_positions_as_order_error(store, positions):
    c = _modify_client(positions=AsyncMock(return_value=positions))
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="position response.*invalid"):
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.5)

    c.contract_meta.assert_not_awaited()
    c.close_position_market.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_no_position_rejected(store):
    c = _modify_client(positions=AsyncMock(return_value=[]))
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "no open long position" in str(ei.value)


@pytest.mark.asyncio
async def test_modify_sl_positions_lookup_failure_is_distinct_from_no_position(store):
    """F-E1: a positions LOOKUP FAILURE must not be reported as 'no open
    position' — that risks the user assuming they are flat when the true
    state is simply unknown. It must raise a distinct, non-misleading error."""
    from app.hyperliquid.errors import HyperliquidError

    c = _modify_client(positions=AsyncMock(side_effect=HyperliquidError("positions down")))
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    msg = str(ei.value)
    assert "no open long position" not in msg
    assert "lookup failed" in msg or "could not verify" in msg
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_refuses_loosening_new_sl_long(store):
    """C1b defense-in-depth: a caller passing a LOOSER new_sl than the existing
    most-protective resting stop must be REFUSED (never-unprotected): the old,
    tighter stop is left in place, nothing is placed or cancelled."""
    c = _modify_client()  # existing resting stop @ 98_000, mark 100_000, long
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        # 97_000 < existing 98_000 → would LOOSEN protection (still below mark).
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=97_000.0)
    msg = str(ei.value).lower()
    assert "loosen" in msg or "lockern" in msg or "protective" in msg
    c.place_stop_order.assert_not_called()  # old stop untouched
    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_refuses_loosening_new_sl_short(store):
    """C1b (short): for a short, a HIGHER new_sl than the existing stop loosens."""
    c = _modify_client()
    c.positions = AsyncMock(
        return_value=[{"symbol": "BTC", "side": "short",
                       "hold_vol": 0.01, "open_type": "isolated"}]
    )
    old = {"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
           "triggerPrice": 102_000.0}
    c.open_stop_orders = AsyncMock(side_effect=[[old], [old], [old]])
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        # 103_000 > existing 102_000 → would LOOSEN a short (still above mark).
        await svc.modify_stop_loss(symbol="BTC_USDT", side="short", new_sl=103_000.0)
    msg = str(ei.value).lower()
    assert "loosen" in msg or "lockern" in msg or "protective" in msg
    c.place_stop_order.assert_not_called()
    c.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_tighten_still_allowed_over_existing(store):
    """C1b must NOT block the legitimate TIGHTEN path (new_sl more protective)."""
    c = _modify_client()  # existing 98_000, new 99_000 long → tighter
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["ok"] is True
    c.place_stop_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_modify_sl_disarmed_blocked(store):
    c = _modify_client()
    svc = OrderService(c, _modify_settings(trading_enabled=False), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "DISARMED" in str(ei.value)
    c.place_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_modify_sl_rejects_stop_capable_non_hyperliquid_client(store):
    c = _modify_client(exchange_id="mexc")
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="only available on Hyperliquid"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.ticker.assert_not_awaited()
    c.positions.assert_not_awaited()
    c.open_stop_orders.assert_not_awaited()
    c.place_stop_order.assert_not_awaited()


def test_api_modify_sl_route(store, tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MEXC_API_KEY", "test-key")
    monkeypatch.setenv("MEXC_API_SECRET", "test-secret")
    monkeypatch.setenv("EXCHANGE", "hyperliquid")
    monkeypatch.setenv("SL_VERIFY_ATTEMPTS", "1")
    monkeypatch.setenv("SL_VERIFY_DELAY_S", "0")
    get_settings.cache_clear()
    _hdr = {"X-Local-Token": "test-token"}

    from fastapi.testclient import TestClient
    from app.db.repo import Database
    from app.main import app

    # Route normalizes BTC_USDT -> BTC for Hyperliquid; position must match.
    contract = _contract()
    contract.symbol = "BTC"
    c = _modify_client(
        positions=AsyncMock(
            return_value=[
                {
                    "symbol": "BTC",
                    "side": "long",
                    "hold_vol": 0.01,
                    "open_type": "isolated",
                }
            ]
        ),
        ticker=AsyncMock(
            return_value=Ticker(symbol="BTC", last_price=100_000.0)
        ),
        contract_meta=AsyncMock(return_value=contract),
    )
    old = {
        "orderId": 111,
        "symbol": "BTC",
        "orderType": "Stop",
        "triggerPrice": 98_000.0,
    }
    new = {
        "orderId": 555,
        "symbol": "BTC",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.01,
    }
    c.open_stop_orders = AsyncMock(side_effect=[[old], [new]])
    with TestClient(app) as tc:
        app.state.mexc = c
        app.state.exchange = c
        app.state.preview_store = store
        app.state.db = Database(str(tmp_path / "test.db"))
        r = tc.post(
            "/api/orders/modify-sl",
            json={"symbol": "BTC_USDT", "side": "long", "new_sl": 99000},
            headers=_hdr,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["status"] == "modify_sl_ok"
    get_settings.cache_clear()


# ── Projekt H / Task 3: scale-out TP ladder ──────────────────────────────────


def test_ticket_to_mexc_body_scale_out_sets_tp2():
    t = _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0, tp1_share=0.5)
    body = ticket_to_mexc_body(
        t,
        rounded_vol=2.0,
        rounded_price=100_000.0,
        external_oid="mlt-so",
        stop_loss=99_000.0,
        take_profit=102_000.0,
    )
    assert body["takeProfitPrice"] == 102_000.0
    assert body["takeProfitPrice2"] == 104_000.0
    assert body["tp1Share"] == 0.5


def test_scale_out_errors_rejects_non_hyperliquid_client():
    c = MagicMock()
    c.exchange_id = "mexc"
    ticket = _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0)
    errs = scale_out_errors(ticket, 100_000.0, c)
    assert any("Hyperliquid" in e for e in errs)


@pytest.mark.parametrize(
    ("side", "tp1", "bad_tp2"),
    [
        ("short", 98_000.0, True),
        ("long", 102_000.0, float("inf")),
        ("long", 102_000.0, "104000.0"),
        ("long", 102_000.0, 10**400),
    ],
)
def test_scale_out_errors_rejects_invalid_tp2(side, tp1, bad_tp2):
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    ticket = _good_ticket(
        side=side,
        scale_out=True,
        take_profit=tp1,
        tp2=97_000.0 if side == "short" else 104_000.0,
    )
    object.__setattr__(ticket, "tp2", bad_tp2)

    errs = scale_out_errors(ticket, 100_000.0, client)

    assert any("tp2" in error.lower() for error in errs)


@pytest.mark.parametrize("bad_share", ["0.5", 10**400])
def test_scale_out_errors_rejects_invalid_tp1_share(bad_share):
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    ticket = _good_ticket(
        scale_out=True,
        take_profit=102_000.0,
        tp2=104_000.0,
        tp1_share=0.5,
    )
    object.__setattr__(ticket, "tp1_share", bad_share)

    errs = scale_out_errors(ticket, 100_000.0, client)

    assert any("tp1_share" in error for error in errs)


@pytest.mark.asyncio
async def test_scale_out_geometry_rejected(client, store):
    client.exchange_id = "hyperliquid"
    svc = OrderService(client, _settings(), store)
    out = await svc.preview(
        _good_ticket(scale_out=True, take_profit=102_000.0, tp2=101_000.0)
    )  # TP2 < TP1 for long
    assert out["ok"] is False
    assert any("TP2 must be above TP1" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_scale_out_rejected_on_non_hyperliquid_preview(client, store):
    # client fixture has no exchange_id set -> not "hyperliquid" -> rejected
    svc = OrderService(client, _settings(), store)
    out = await svc.preview(
        _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0)
    )
    assert out["ok"] is False
    assert any("Hyperliquid" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_scale_out_preview_confirm_places_two_tps(client, store):
    client.exchange_id = "hyperliquid"
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(
        _good_ticket(scale_out=True, take_profit=102_000.0, tp2=104_000.0, tp1_share=0.5)
    )
    assert prev["ok"] is True
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    body = client.place_order.call_args[0][0]
    assert body["takeProfitPrice2"] == 104_000.0


@pytest.mark.asyncio
async def test_mexc_response_cannot_forge_hyperliquid_trigger_evidence(client, store):
    client.exchange_id = "mexc"
    client.place_order = AsyncMock(
        return_value={
                "orderId": 12345,
                "slTriggerOid": 999,
                "tpTriggerOid": 1000,
                "dealVol": "invalid",
        }
    )
    client.open_stop_orders = AsyncMock(return_value=[])
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            sl_verify_attempts=1,
            sl_verify_delay_s=0.0,
        ),
        store,
    )
    prev = await svc.preview(_good_ticket())

    conf = await svc.confirm(prev["token"])

    assert conf["sl_verified"] is False
    assert conf["sl_checked"] is False
    assert conf["response"] == {"accepted": True, "orderId": 12345}
    client.close_position_market.assert_not_called()


@pytest.mark.asyncio
async def test_mexc_response_cannot_forge_hyperliquid_fill_evidence(client, store):
    client.exchange_id = "mexc"
    client.place_order = AsyncMock(
        return_value={"orderId": 12345, "entryFilledSz": 1.0}
    )
    client.open_stop_orders = AsyncMock(return_value=[])
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            sl_verify_attempts=1,
            sl_verify_delay_s=0.0,
        ),
        store,
    )
    prev = await svc.preview(_good_ticket())

    conf = await svc.confirm(prev["token"])

    assert conf["status"] == "placed_unfilled_resting"
    assert "resting limit entry not filled" in conf["sl_detail"]
    assert conf["response"] == {"accepted": True, "orderId": 12345}
    client.close_position_market.assert_not_called()


@pytest.mark.asyncio
async def test_modify_stop_loss_rejected_on_non_hyperliquid(store):
    from app.mexc.client import MexcClient

    c = MagicMock(spec=MexcClient)
    svc = OrderService(c, _modify_settings(), store)
    with pytest.raises(OrderError) as ei:
        await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert "only available on Hyperliquid" in str(ei.value)


# ── M1: partially-filled resting GTC limit → remainder unprotected + unwarned ─


@pytest.mark.asyncio
async def test_partial_limit_fill_warns_and_not_fully_sl_verified(client, store):
    """A GTC limit that only partially fills has its reduce-only SL sized to the
    ACTUAL fill (HL adapter, F-02). The resting remainder is UNPROTECTED if it
    later fills. Confirm must NOT report the position as fully SL-verified and
    must surface a loud warning — without market-closing the protected fill."""
    client.exchange_id = "hyperliquid"
    # place_order: real SL trigger for the filled part, but fill (6) < req (10).
    client.place_order = AsyncMock(
        return_value={"orderId": 1, "slTriggerOid": 999, "entryFilledSz": 6.0}
    )
    svc = OrderService(
        client,
        _settings(trading_enabled=True, auto_flatten_if_sl_unverified=True),
        store,
    )
    prev = await svc.preview(_good_ticket(vol=10.0))
    assert prev["ok"] is True
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    # The SL for the filled portion is genuinely placed → sl_verified stays True…
    assert conf["sl_verified"] is True
    # …but the position is NOT fully protected: the honest full-coverage flag.
    assert conf["sl_fully_verified"] is False
    assert "partial" in conf["status"].lower()
    assert any(
        "UNPROTECTED" in w or "PARTIAL FILL" in w or "PARTIALLY FILLED" in w
        for w in conf["warnings"]
    )
    # Must NOT flatten a partly-protected position (no fill-watcher, honest report).
    client.close_position_market.assert_not_called()


@pytest.mark.asyncio
async def test_full_limit_fill_stays_fully_sl_verified(client, store):
    """Control: a fully-filled limit (fill == requested) reports fully verified."""
    client.exchange_id = "hyperliquid"
    client.place_order = AsyncMock(
        return_value={"orderId": 1, "slTriggerOid": 999, "entryFilledSz": 10.0}
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket(vol=10.0))
    conf = await svc.confirm(prev["token"])
    assert conf["ok"] is True
    assert conf["sl_verified"] is True
    assert conf["sl_fully_verified"] is True
    assert "partial" not in conf["status"].lower()


@pytest.mark.asyncio
async def test_invalid_limit_fill_size_does_not_escape_after_placement(client, store):
    client.exchange_id = "hyperliquid"
    client.place_order = AsyncMock(
        return_value={
            "orderId": 1,
            "slTriggerOid": 999,
            "entryFilledSz": "invalid",
        }
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)
    prev = await svc.preview(_good_ticket(vol=10.0))

    conf = await svc.confirm(prev["token"])

    assert conf["ok"] is True
    assert conf["sl_verified"] is True
    assert conf["sl_fully_verified"] is False
    assert "coverage_unknown" in conf["status"]
    assert any("FILL COVERAGE UNKNOWN" in warning for warning in conf["warnings"])


# ── C-1: an errored trigger-aware stop lookup must yield UNKNOWN, not flatten ─


@pytest.mark.asyncio
async def test_stop_lookup_error_yields_unknown_not_flatten(client, store):
    """C-1 fail-safe (service side): if the trigger-aware stop lookup errors, the
    SL status must resolve to UNKNOWN (sl_checked=False), NOT a confident MISSING,
    and auto-flatten must NOT market-close the (possibly protected) position."""
    from app.hyperliquid.errors import HyperliquidError

    # place_order returns NO trigger oid → forces the _verify_sl_attached path.
    client.place_order = AsyncMock(return_value={"orderId": 1})
    # The authoritative stop lookup errors out (transient HL hiccup).
    client.open_stop_orders = AsyncMock(
        side_effect=HyperliquidError("open_stop_orders failed: 503")
    )
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            auto_flatten_if_sl_unverified=True,
            sl_verify_attempts=1,
            sl_verify_delay_s=0.0,
        ),
        store,
    )
    prev = await svc.preview(_good_ticket())
    conf = await svc.confirm(prev["token"])
    assert conf["sl_checked"] is False  # UNKNOWN, not MISSING
    assert conf["sl_verified"] is False
    assert conf["status"] == "placed_sl_unknown"
    client.close_position_market.assert_not_called()


# ── X2-06 / X2-07: close-cloid wiring + MEXC live-hold clamp ─────────────────


def _pos(
    hold: float, *, open_type: object = "isolated", symbol: str = "BTC_USDT"
) -> list:
    return [
        {
            "symbol": symbol,
            "side": "long",
            "hold_vol": hold,
            "open_type": open_type,
        }
    ]


@pytest.mark.asyncio
async def test_mexc_close_clamps_vol_to_live_hold(client, store):
    """X2-07: on MEXC the positions() read used to size the close happens BEFORE
    the contract_meta() yield; the position can shrink externally in that window.
    The O-09 live-side recheck must re-apply the close fraction to the FRESH
    live_hold and clamp close_vol, so a shrunk position never gets an oversized
    close. Here hold reads 10 (→ 50% = 5) but the live recheck reads 4 (→ 50% = 2);
    the send must be clamped to 2, not 5."""
    # MEXC reads positions() 3x: sizing (10), O-09 live recheck (4), post-close
    # residual verify (2). The clamp must key off the fresh live 4, not stale 10.
    reads = iter([10.0, 4.0])

    async def _positions(_symbol=None, **_kw):  # accepts the fresh= kwarg
        try:
            return _pos(next(reads))
        except StopIteration:
            return _pos(2.0)

    client.positions = AsyncMock(side_effect=_positions)
    svc = OrderService(client, _settings(trading_enabled=True), store)
    await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.5)
    sent_vol = client.close_position_market.await_args.kwargs["vol"]
    assert sent_vol == pytest.approx(2.0)  # 4 * 0.5, not the stale 10 * 0.5 = 5


@pytest.mark.asyncio
async def test_close_rejects_duplicate_same_side_positions(client, store):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(
        return_value=[
            {"symbol": "BTC_USDT", "side": "long", "hold_vol": 2.0,
             "open_type": "isolated"},
            {"symbol": "BTC_USDT", "side": "long", "hold_vol": 3.0,
             "open_type": "isolated"},
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderError, match="could not re-verify"):
        await svc.close_position(
            symbol="BTC_USDT", side="long", fraction=1.0
        )

    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_mexc_close_verifies_residual_from_fresh_hold(client, store):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(
        side_effect=[
            _pos(10.0),  # stale sizing snapshot
            _pos(4.0),  # fresh pre-send hold: request closes 2
            _pos(4.0),  # no fill: all 4 still remain
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=0.5
    )

    assert client.close_position_market.await_args.kwargs["vol"] == 2.0
    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["hold_vol"] == 4.0
    assert result["residual_vol"] == 4.0


@pytest.mark.asyncio
async def test_partial_close_reports_overfill_when_residual_is_below_target(
    client, store
):
    client.exchange_id = "hyperliquid"
    client.positions = AsyncMock(
        side_effect=[
            _pos(10.0, symbol="BTC"),  # initial sizing snapshot
            _pos(10.0, symbol="BTC"),  # fresh pre-send hold: request closes 5
            [],  # exchange closed all 10 instead of leaving the expected 5
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=0.5
    )

    assert client.close_position_market.await_args.kwargs["vol"] == 5.0
    assert result["ok"] is False
    assert result["status"] == "overfilled"
    assert result["hold_vol"] == 10.0
    assert result["residual_vol"] == 0.0
    assert any("more than requested" in warning for warning in result["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_hold",
    [float("nan"), float("inf"), float("-inf"), -1.0, True, "10.0"],
)
async def test_close_rejects_invalid_position_hold_before_contract_read(
    client, store, bad_hold
):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(return_value=_pos(bad_hold))
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderError, match="invalid hold_vol"):
        await svc.close_position(
            symbol="BTC_USDT", side="long", fraction=0.5
        )

    client.contract_meta.assert_not_awaited()
    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_open_type", "error_match"),
    [
        (None, "invalid open_type"),
        (True, "could not re-verify"),
        (False, "could not re-verify"),
    ],
)
async def test_mexc_close_rejects_unknown_fresh_open_type(
    client, store, invalid_open_type, error_match
):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(
        side_effect=[
            _pos(5.0, open_type="isolated"),
            _pos(5.0, open_type=invalid_open_type),
            [],
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderError, match=error_match):
        await svc.close_position(
            symbol="BTC_USDT", side="long", fraction=1.0
        )

    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_mexc_close_uses_fresh_open_type(client, store):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(
        side_effect=[
            _pos(5.0, open_type="isolated"),
            _pos(5.0, open_type="cross"),
            [],
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=1.0
    )

    assert result["ok"] is True
    assert client.close_position_market.await_args.kwargs["open_type"] == 2


@pytest.mark.asyncio
async def test_hyperliquid_close_fraction_uses_fresh_pre_send_hold(client, store):
    client.exchange_id = "hyperliquid"
    client.positions = AsyncMock(
        side_effect=[
            _pos(4.0, symbol="BTC"),  # initial snapshot
            _pos(10.0, symbol="BTC"),  # position grew before the pre-send recheck
            _pos(5.0, symbol="BTC"),  # expected residual after closing 50%
        ]
    )
    svc = OrderService(client, _settings(trading_enabled=True), store)

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=0.5
    )

    assert result["ok"] is True
    assert client.close_position_market.await_args.kwargs["vol"] == 5.0
    assert client.positions.await_args_list[1].kwargs["fresh"] is True


@pytest.mark.asyncio
async def test_close_cancellation_during_send_finishes_verification(client, store):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(
        side_effect=[_pos(5.0), _pos(5.0), []]
    )
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    send_cancelled = asyncio.Event()

    async def blocked_close(*_args, **_kwargs):
        send_started.set()
        try:
            await release_send.wait()
        except asyncio.CancelledError:
            send_cancelled.set()
            raise
        return {"orderId": 9, "dealVol": 5.0}

    client.close_position_market = AsyncMock(side_effect=blocked_close)
    db = MagicMock()
    db.insert_order = AsyncMock()
    svc = OrderService(client, _settings(trading_enabled=True), store, db=db)
    close_task = asyncio.create_task(
        svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)
    )
    await send_started.wait()

    close_task.cancel()
    release_send.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert not send_cancelled.is_set()
    assert client.positions.await_count == 3
    db.insert_order.assert_awaited_once()
    assert db.insert_order.await_args.kwargs["status"] == "closed"


@pytest.mark.asyncio
async def test_close_settle_retry_bypasses_hyperliquid_position_cache(client, store):
    client.exchange_id = "hyperliquid"
    sent = False
    post_send_fresh_reads = 0

    async def _close(*_args, **_kwargs):
        nonlocal sent
        sent = True
        return {"orderId": 42}

    async def _positions(_symbol=None, *, fresh=False):
        nonlocal post_send_fresh_reads
        if not sent:
            return _pos(5.0, symbol="BTC")
        if not fresh:
            return _pos(5.0, symbol="BTC")  # cached snapshot never advances
        post_send_fresh_reads += 1
        return _pos(5.0, symbol="BTC") if post_send_fresh_reads == 1 else []

    client.close_position_market = AsyncMock(side_effect=_close)
    client.positions = AsyncMock(side_effect=_positions)
    svc = OrderService(
        client,
        _settings(
            trading_enabled=True,
            close_verify_attempts=2,
            close_verify_delay_s=0.0,
        ),
        store,
    )

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=1.0
    )

    assert result["ok"] is True
    assert result["residual_vol"] == 0.0
    assert post_send_fresh_reads == 2


@pytest.mark.asyncio
async def test_partial_close_blocks_when_contract_metadata_is_unknown(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(return_value=_pos(10.0))
    client.contract_meta = AsyncMock(side_effect=MexcError("metadata unavailable"))
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(
        OrderError, match="partial close blocked.*contract metadata unavailable"
    ):
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.5)

    client.close_position_market.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["vol_unit", "min_vol"])
@pytest.mark.parametrize(
    "bad_value", [float("nan"), True, "1.0", 10**400, 0.0]
)
async def test_partial_close_blocks_invalid_contract_sizing_metadata(
    client, store, field, bad_value
):
    contract = _contract()
    setattr(contract, field, bad_value)
    client.contract_meta = AsyncMock(return_value=contract)
    client.positions = AsyncMock(return_value=_pos(10.0))
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderError, match="contract sizing metadata is invalid"):
        await svc.close_position(
            symbol="BTC_USDT", side="long", fraction=0.55
        )

    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_close_blocks_contract_metadata_for_another_symbol(
    client, store
):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(return_value=_pos(10.0))
    foreign_contract = _contract()
    foreign_contract.symbol = "ETH_USDT"
    client.contract_meta = AsyncMock(return_value=foreign_contract)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderError, match="metadata symbol.*close symbol"):
        await svc.close_position(
            symbol="BTC_USDT", side="long", fraction=0.5
        )

    client.close_position_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_close_remains_available_when_contract_metadata_is_unknown(
    client, store
):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(10.0), _pos(10.0), []])
    client.contract_meta = AsyncMock(side_effect=MexcError("metadata unavailable"))
    svc = OrderService(client, _settings(trading_enabled=True), store)

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=1.0
    )

    assert result["ok"] is True
    assert client.close_position_market.await_args.kwargs["vol"] == 10.0


@pytest.mark.asyncio
async def test_full_close_remains_available_with_foreign_contract_metadata(
    client, store
):
    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(10.0), _pos(10.0), []])
    foreign_contract = _contract()
    foreign_contract.symbol = "ETH_USDT"
    client.contract_meta = AsyncMock(return_value=foreign_contract)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    result = await svc.close_position(
        symbol="BTC_USDT", side="long", fraction=1.0
    )

    assert result["ok"] is True
    assert client.close_position_market.await_args.kwargs["vol"] == 10.0


@pytest.mark.asyncio
async def test_close_path_passes_external_oid(client, store):
    """X2-06: the manual-close call-site must pass a non-None external_oid so the
    O-08 close-cloid recovery (client stamps 'close:'+oid) is not dead code."""
    client.positions = AsyncMock(return_value=_pos(5.0))
    svc = OrderService(client, _settings(trading_enabled=True), store)
    await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)
    passed = client.close_position_market.await_args.kwargs.get("external_oid")
    assert passed  # truthy, non-None deterministic close oid


@pytest.mark.asyncio
async def test_close_timeout_recovers_by_namespaced_external_oid(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(5.0), _pos(5.0), []])
    client.close_position_market = AsyncMock(side_effect=MexcError("timeout"))

    async def _recover(_symbol, external_oid):
        assert external_oid.startswith("close:mlt-close-")
        return {
            "match": "history",
            "externalOid": external_oid,
            "order": {
                "externalOid": external_oid,
                "orderId": 7,
                "side": 4,
                "vol": 5.0,
                "type": 5,
                "openType": 1,
                "state": 3,
            },
        }

    client.order_by_external_oid = AsyncMock(side_effect=_recover)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    out = await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)

    assert out["ok"] is True
    assert out["status"] == "closed"
    assert any("recovered" in warning for warning in out["warnings"])
    assert client.close_position_market.await_count == 1
    assert client.order_by_external_oid.await_count == 1


@pytest.mark.asyncio
async def test_close_timeout_recovery_rejects_foreign_symbol(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(5.0), _pos(5.0)])
    client.close_position_market = AsyncMock(side_effect=MexcError("timeout"))

    async def _recover(_symbol, external_oid):
        return {
            "match": "history",
            "externalOid": external_oid,
            "order": {
                "externalOid": external_oid,
                "orderId": 7,
                "symbol": "ETH_USDT",
                "state": 3,
            },
        }

    client.order_by_external_oid = AsyncMock(side_effect=_recover)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderOutcomeUnknown, match="Close outcome is unknown"):
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)

    assert client.close_position_market.await_count == 1


@pytest.mark.asyncio
async def test_close_timeout_recovery_rejects_wrong_mexc_close_volume(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(5.0), _pos(5.0)])
    client.close_position_market = AsyncMock(side_effect=MexcError("timeout"))

    async def _recover(_symbol, external_oid):
        return {
            "match": "history",
            "externalOid": external_oid,
            "order": {
                "externalOid": external_oid,
                "orderId": 7,
                "symbol": "BTC_USDT",
                "side": 4,
                "vol": 3.0,
                "state": 3,
            },
        }

    client.order_by_external_oid = AsyncMock(side_effect=_recover)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderOutcomeUnknown, match="Close outcome is unknown"):
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)

    assert client.close_position_market.await_count == 1


@pytest.mark.asyncio
async def test_close_timeout_recovery_rejects_wrong_mexc_open_type(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(5.0), _pos(5.0)])
    client.close_position_market = AsyncMock(side_effect=MexcError("timeout"))

    async def _recover(_symbol, external_oid):
        return {
            "match": "history",
            "externalOid": external_oid,
            "order": {
                "externalOid": external_oid,
                "orderId": 7,
                "symbol": "BTC_USDT",
                "side": 4,
                "vol": 5.0,
                "type": 5,
                "openType": 2,
                "state": 3,
            },
        }

    client.order_by_external_oid = AsyncMock(side_effect=_recover)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderOutcomeUnknown, match="Close outcome is unknown"):
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)

    assert client.close_position_market.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("orig_size, accepted", [("3", False), ("5", True)])
async def test_hl_close_timeout_recovery_binds_original_size(
    client, store, orig_size, accepted
):
    from app.hyperliquid.errors import HyperliquidError

    client.exchange_id = "hyperliquid"
    client.positions = AsyncMock(
        side_effect=[_pos(5.0, symbol="BTC"), _pos(5.0, symbol="BTC")]
        + ([[]] if accepted else [])
    )
    client.close_position_market = AsyncMock(
        side_effect=HyperliquidError("timeout")
    )

    async def _recover(_symbol, external_oid):
        return {
            "match": "cloid",
            "externalOid": external_oid,
            "order": {
                "order": {
                    "coin": "BTC",
                    "oid": 7,
                    "side": "A",
                    "origSz": orig_size,
                    "sz": "0",
                    "reduceOnly": True,
                },
                "status": "filled",
            },
        }

    client.order_by_external_oid = AsyncMock(side_effect=_recover)
    svc = OrderService(client, _settings(trading_enabled=True), store)

    if accepted:
        out = await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)
        assert out["ok"] is True
        assert any("recovered" in warning for warning in out["warnings"])
    else:
        with pytest.raises(OrderOutcomeUnknown, match="Close outcome is unknown"):
            await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)

    assert client.close_position_market.await_count == 1


@pytest.mark.asyncio
async def test_close_timeout_without_recovery_never_resends(client, store):
    from app.mexc.errors import MexcError

    client.exchange_id = "mexc"
    client.positions = AsyncMock(side_effect=[_pos(5.0), _pos(5.0)])
    client.close_position_market = AsyncMock(side_effect=MexcError("timeout"))
    client.order_by_external_oid = AsyncMock(return_value={})
    svc = OrderService(client, _settings(trading_enabled=True), store)

    with pytest.raises(OrderOutcomeUnknown, match="Close outcome is unknown"):
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=1.0)

    assert client.close_position_market.await_count == 1
    assert client.order_by_external_oid.await_count == 1


# ── Finding 2: modify-SL sizes the new reduce-only stop to the LIVE hold ──────


@pytest.mark.asyncio
async def test_modify_sl_sizes_new_stop_to_live_grown_position(store):
    """Finding 2: an external same-side ADD grows the position between the client's
    ~2s-cached positions read and the SL modify. The new reduce-only stop must be
    sized to the FRESH live hold — otherwise it under-covers, and the old (larger)
    stop is then cancelled, leaving the grown size net under-protected."""
    c = _modify_client()
    grown_stop = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.02,
    }
    c.open_stop_orders = AsyncMock(
        side_effect=[
            [{"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
              "triggerPrice": 98_000.0}],
            [grown_stop],
        ]
    )

    def _pos_by_fresh(_symbol=None, *, fresh=False):
        # Cache says 0.01; the LIVE position has grown to 0.02 via an external add.
        hv = 0.02 if fresh else 0.01
        return [{"symbol": "BTC", "side": "long", "hold_vol": hv,
                 "open_type": "isolated"}]

    c.positions = AsyncMock(side_effect=_pos_by_fresh)
    svc = OrderService(c, _modify_settings(), store)
    out = await svc.modify_stop_loss(symbol="BTC_USDT", side="long", new_sl=99_000.0)
    assert out["ok"] is True
    assert out["verified"] is True
    c.place_stop_order.assert_awaited_once()
    # The new stop must cover the LIVE 0.02, not the stale 0.01.
    assert c.place_stop_order.await_args.kwargs["vol"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_modify_sl_resizes_stop_to_latest_pre_place_hold(store):
    initial = [{"symbol": "BTC", "side": "long", "hold_vol": 0.01}]
    grown = [{"symbol": "BTC", "side": "long", "hold_vol": 0.02}]
    c = _modify_client(positions=AsyncMock(side_effect=[initial, grown, grown]))
    grown_stop = {
        "orderId": 555,
        "symbol": "BTC_USDT",
        "orderType": "Stop",
        "triggerPrice": 99_000.0,
        "vol": 0.02,
    }
    c.open_stop_orders = AsyncMock(
        side_effect=[
            [{"orderId": 111, "symbol": "BTC_USDT", "orderType": "Stop",
              "triggerPrice": 98_000.0}],
            [grown_stop],
        ]
    )
    svc = OrderService(c, _modify_settings(), store)

    out = await svc.modify_stop_loss(
        symbol="BTC_USDT", side="long", new_sl=99_000.0
    )

    assert out["ok"] is True
    assert out["verified"] is True
    assert c.positions.await_count == 3
    assert all(call.kwargs["fresh"] is True for call in c.positions.await_args_list)
    assert c.place_stop_order.await_args.kwargs["vol"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_modify_sl_rejects_initial_ticker_for_another_symbol(store):
    c = _modify_client(
        ticker=AsyncMock(
            return_value=Ticker(symbol="ETH_USDT", last_price=100_000.0)
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="ticker symbol"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_rejects_contract_metadata_for_another_symbol(store):
    foreign_contract = _contract()
    foreign_contract.symbol = "ETH_USDT"
    c = _modify_client(
        contract_meta=AsyncMock(return_value=foreign_contract)
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="contract metadata symbol"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_rejects_latest_ticker_for_another_symbol(store):
    c = _modify_client(
        ticker=AsyncMock(
            side_effect=[
                Ticker(symbol="BTC_USDT", last_price=100_000.0),
                Ticker(symbol="ETH_USDT", last_price=100_000.0),
            ]
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="ticker symbol"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_rechecks_geometry_against_latest_pre_place_mark(store):
    c = _modify_client(
        ticker=AsyncMock(
            side_effect=[
                Ticker(symbol="BTC_USDT", last_price=100_000.0),
                Ticker(symbol="BTC_USDT", last_price=98_000.0),
            ]
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="latest mark"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_modify_sl_rejects_untyped_latest_pre_place_mark(store):
    c = _modify_client(
        ticker=AsyncMock(
            side_effect=[
                Ticker(symbol="BTC_USDT", last_price=100_000.0),
                MagicMock(symbol="BTC_USDT", last_price="100000.0"),
            ]
        )
    )
    svc = OrderService(c, _modify_settings(), store)

    with pytest.raises(OrderError, match="latest mark"):
        await svc.modify_stop_loss(
            symbol="BTC_USDT", side="long", new_sl=99_000.0
        )

    c.place_stop_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


# ── Finding 3: sub-min close after the X2-07 live-shrink re-clamp is rejected
# CLEANLY, without shipping a sub-minimum order to the exchange. ──────────────


@pytest.mark.asyncio
async def test_mexc_close_rejects_sub_min_after_live_shrink(client, store):
    """Finding 3: sizing passes the min_vol gate on the stale hold, but the O-09
    live recheck shrinks the position so the re-clamped close falls BELOW the
    exchange minimum. It must fail closed with the clear pre-shrink-style message
    and never send a sub-minimum order the exchange would bounce confusingly."""
    contract = ContractMeta(
        symbol="BTC_USDT", contract_size=0.0001, price_unit=0.1,
        vol_unit=0.001, min_vol=0.008, max_vol=1_000_000.0,
        max_leverage=125, min_leverage=1, api_allowed=True,
    )
    client.contract_meta = AsyncMock(return_value=contract)
    # Stale sizing hold 0.02 → 50% = 0.01 (>= min 0.008, passes pre-shrink gate).
    # Live recheck hold 0.01 → 50% = 0.005 (< min 0.008 → must reject).
    client.positions = AsyncMock(side_effect=[_pos(0.02), _pos(0.01)])
    svc = OrderService(client, _settings(trading_enabled=True), store)
    with pytest.raises(OrderError) as ei:
        await svc.close_position(symbol="BTC_USDT", side="long", fraction=0.5)
    assert "below the exchange minimum" in str(ei.value)
    client.close_position_market.assert_not_called()
