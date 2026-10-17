"""Regression tests for the 2026-10 production audit findings."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock

from app.config import Settings
from app.db.repo import Database
from app.models import ContractMeta, OrderTicket, Ticker
from app.orders.service import OrderError, OrderService, execution_context_id
from app.orders.tokens import PreviewStore


class _LaggingMexc:
    exchange_id = "mexc"

    def __init__(self) -> None:
        self.place_calls = 0
        self.place_started = asyncio.Event()
        self.release_place = asyncio.Event()

    async def contract_meta(self, symbol):
        return ContractMeta(
            symbol=symbol,
            contract_size=0.0001,
            price_unit=0.1,
            vol_unit=1.0,
            min_vol=1.0,
            max_vol=1_000_000.0,
            min_leverage=1,
            max_leverage=125,
            api_allowed=True,
            state=0,
        )

    async def ticker(self, symbol):
        return Ticker(symbol=symbol, last_price=100_000.0)

    async def account_state(self, symbol=None, *, fresh=False):
        return (
            [
                {
                    "currency": "USDT",
                    "equity": 10_000.0,
                    "availableBalance": 9_000.0,
                }
            ],
            [],
        )

    async def open_orders(self, symbol=None):
        return []

    async def positions(self, symbol=None, *, fresh=False):
        return []

    async def set_leverage(self, *args, **kwargs):
        return {"success": True}

    async def place_order(self, body):
        self.place_calls += 1
        self.place_started.set()
        await self.release_place.wait()
        return {"orderId": self.place_calls}


def _settings() -> Settings:
    return Settings(
        exchange="mexc",
        trading_enabled=True,
        allow_manual_trigger=True,
        max_leverage=20,
        max_risk_pct=1.0,
        min_rrr=2.0,
        strict_rrr=True,
        risk_slippage_pct=0.05,
        allow_unprotected_entry=False,
        max_notional_usdt=5_000.0,
        preview_token_ttl_seconds=60,
        mexc_api_key="synthetic-key",
        mexc_api_secret="synthetic-secret",
        local_api_token="synthetic-token",
    )


def _ticket() -> OrderTicket:
    return OrderTicket(
        symbol="BTC_USDT",
        side="long",
        order_type="market",
        vol=1.0,
        leverage=5,
        entry=100_000.0,
        stop_loss=99_000.0,
        take_profit=103_000.0,
        open_type=1,
        trigger_mode="manual",
    )


@pytest.mark.asyncio
async def test_mexc_entry_reservation_blocks_preview_during_exchange_lag(tmp_path):
    db = Database(str(tmp_path / "orders.db"))
    await db.init()
    await db.open()
    client = _LaggingMexc()
    service = OrderService(client, _settings(), PreviewStore(), db=db)
    try:
        first = await service.preview(_ticket())
        confirming = asyncio.create_task(service.confirm(first["token"]))
        await client.place_started.wait()

        second = await service.preview(_ticket())
        assert second["ok"] is False
        assert "still converging" in second["errors"][0]

        client.release_place.set()
        assert (await confirming)["ok"] is True

        third = await service.preview(_ticket())
        assert third["ok"] is False
        assert client.place_calls == 1
    finally:
        client.release_place.set()
        await db.close()


@pytest.mark.asyncio
async def test_cancelled_shared_write_is_rolled_back_before_next_writer(tmp_path):
    db = Database(str(tmp_path / "cancel.db"))
    await db.init()
    await db.open()
    connection = db._shared
    assert connection is not None
    original_commit = connection.commit
    reached_commit = asyncio.Event()

    async def cancellable_commit() -> None:
        reached_commit.set()
        await asyncio.Future()

    try:
        connection.commit = cancellable_commit
        task = asyncio.create_task(
            db.insert_proposal(
                symbol="BTC_USDT", proposal_json={"source": "cancelled"}
            )
        )
        await reached_commit.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        connection.commit = original_commit
        await db.insert_proposal(
            symbol="ETH_USDT", proposal_json={"source": "later"}
        )
        symbols = {row["symbol"] for row in await db.recent_proposals()}
        assert symbols == {"ETH_USDT"}
    finally:
        connection.commit = original_commit
        await db.close()


@pytest.mark.asyncio
async def test_resting_limit_keeps_explicit_proposal_after_fifteen_minutes(tmp_path):
    db = Database(str(tmp_path / "provenance.db"))
    await db.init()
    proposal_id = await db.insert_proposal(
        symbol="SOL_USDT", proposal_json={"action": "BUY", "thesis": "source"}
    )
    order_id = await db.insert_order(
        symbol="SOL_USDT",
        side="long",
        request_json={"_proposal_id": proposal_id},
        response_json={"orderId": 1},
        status="placed_unfilled_resting",
        error=None,
    )
    placed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    async with db._acquire() as conn:
        await conn.execute(
            "UPDATE orders SET created_at = ? WHERE id = ?",
            (placed_at.isoformat(), order_id),
        )
        await conn.commit()

    resolved = await db.proposal_for_open_position(
        "SOL_USDT",
        "long",
        observed_open_sig=77,
    )

    assert resolved is None  # Placement time alone is not a fill/position link.
    await db.bind_entry_proposal(
        "SOL_USDT", "long", open_sig=77, proposal_id=proposal_id,
        external_oid="mlt-explicit-limit",
    )
    resolved = await db.proposal_for_open_position(
        "SOL_USDT", "long", observed_open_sig=77,
    )

    assert resolved is not None
    assert resolved["id"] == proposal_id


def _reservation_request():
    return {
        "symbol": "BTC_USDT", "side": 1, "vol": 1.0, "type": 1,
        "openType": 1, "leverage": 5, "price": 100_000.0,
    }


def _position(hold=1.0):
    return {
        "symbol": "BTC_USDT", "side": "long", "hold_vol": hold,
        "entry_price": 100_000.0, "open_type": "isolated", "position_id": 77,
        "liquidate_price": 90_000.0,
    }


def _recovered(*, state=3, deal=1.0, **changes):
    order = {
        **_reservation_request(), "orderId": 123, "positionId": 77,
        "externalOid": "mlt-reservation", "dealVol": deal, "state": state,
        **changes,
    }
    return {"match": "direct", "externalOid": "mlt-reservation", "order": order}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "hold", "blocked"),
    [
        (_recovered(state=2, deal=0.5), 0.5, True),
        (_recovered(state=2, deal=1.0), 1.0, True),
        (_recovered(), 0.5, True),
        (_recovered(), 1.0, False),
        (_recovered(state=4, deal=0.5), 0.5, False),
        (_recovered(state=4, deal=0.0), 0.0, False),
        (_recovered(state=3, deal=1.0, orderState=2), 1.0, True),
        (_recovered(orderId=999), 1.0, True),
        (_recovered(vol=2.0), 1.0, True),
        (_recovered(side=3), 1.0, True),
        ({}, 1.0, True),
    ],
)
async def test_reservation_requires_terminal_order_and_visible_exposure(
    tmp_path, evidence, hold, blocked
):
    db = Database(str(tmp_path / "reconcile.db"))
    await db.init()
    client = _LaggingMexc()
    client.order_by_external_oid = AsyncMock(return_value=evidence)
    service = OrderService(client, _settings(), PreviewStore(), db=db)
    assert await service._claim_entry_reservation(
        "BTC_USDT", "long", "mlt-reservation", 0.0,
        _reservation_request(), None,
    )
    await db.set_entry_reservation_order_id(
        exchange_id="mexc", symbol="BTC_USDT", side="long",
        external_oid="mlt-reservation", order_id=123,
    )
    # Simulate elapsed time and restart without sleeps. Old expiry is diagnostic.
    async with db._acquire() as conn:
        await conn.execute("UPDATE entry_reservations SET expires_at = 0")
        await conn.commit()
    db = Database(str(tmp_path / "reconcile.db"))
    await db.init()
    service.db = db
    positions = [_position(hold)] if hold else []
    assert await service._has_entry_reservation(
        "BTC_USDT", "long", positions=positions
    ) is blocked
    client.order_by_external_oid.assert_awaited_once_with(
        "BTC_USDT", "mlt-reservation", include_terminal=True
    )


@pytest.mark.asyncio
async def test_partial_fill_blocks_a_second_real_preview(tmp_path):
    db = Database(str(tmp_path / "partial.db"))
    await db.init()
    client = _LaggingMexc()
    client.order_by_external_oid = AsyncMock(return_value=_recovered(state=2, deal=0.5))
    client.account_state = AsyncMock(return_value=(
        [{"currency": "USDT", "equity": 10_000.0, "availableBalance": 9_000.0}],
        [_position(0.5)],
    ))
    service = OrderService(client, _settings(), PreviewStore(), db=db)
    await service._claim_entry_reservation(
        "BTC_USDT", "long", "mlt-reservation", 0.0,
        _reservation_request(), None,
    )
    preview = await service.preview(_ticket())
    assert preview["ok"] is False
    assert "still converging" in preview["errors"][0]
    assert client.place_calls == 0


@pytest.mark.asyncio
async def test_cancel_reconciles_terminal_zero_fill_guard(tmp_path):
    db = Database(str(tmp_path / "cancel-guard.db"))
    await db.init()
    client = _LaggingMexc()
    client.open_orders = AsyncMock(return_value=[_recovered(state=2, deal=0)["order"]])
    client.cancel_order = AsyncMock(return_value=[{"orderId": 123, "errorCode": 0}])
    client.order_by_external_oid = AsyncMock(return_value=_recovered(state=4, deal=0))
    service = OrderService(client, _settings(), PreviewStore(), db=db)
    await service._claim_entry_reservation(
        "BTC_USDT", "long", "mlt-reservation", 0.0,
        _reservation_request(), None,
    )
    assert (await service.cancel(order_id=123, symbol="BTC_USDT"))["ok"] is True
    assert await db.active_entry_reservation(
        exchange_id="mexc", symbol="BTC_USDT", side="long"
    ) is None


@pytest.mark.asyncio
async def test_exact_order_position_link_binds_entry_and_survives_addon(tmp_path):
    db = Database(str(tmp_path / "entry-link.db"))
    await db.init()
    proposal_id = await db.insert_proposal(symbol="BTC_USDT", proposal_json={"thesis": "entry"})
    addon = await db.insert_proposal(symbol="BTC_USDT", proposal_json={"thesis": "addon"})
    client = _LaggingMexc()
    client.order_by_external_oid = AsyncMock(return_value=_recovered())
    service = OrderService(client, _settings(), PreviewStore(), db=db)
    await service._claim_entry_reservation(
        "BTC_USDT", "long", "mlt-reservation", 0.0,
        _reservation_request(), proposal_id,
    )
    assert not await service._has_entry_reservation("BTC_USDT", "long", positions=[_position()])
    await db.bind_entry_proposal(
        "BTC_USDT", "long", open_sig=77, proposal_id=addon, external_oid="mlt-addon"
    )
    result = await db.proposal_for_open_position("BTC_USDT", "long", observed_open_sig=77)
    assert result["id"] == proposal_id
    assert await db.proposal_for_open_position("BTC_USDT", "long", observed_open_sig=78) is None


@pytest.mark.asyncio
async def test_timestamp_proximity_never_binds_unrelated_orders(tmp_path):
    db = Database(str(tmp_path / "unbound.db"))
    await db.init()
    for age in (timedelta(days=30), timedelta(seconds=-30)):
        proposal_id = await db.insert_proposal(symbol="BTC_USDT", proposal_json={"action": "BUY"})
        oid = await db.insert_order(
            symbol="BTC_USDT", side="long", request_json={"_proposal_id": proposal_id},
            response_json={"orderId": 123}, status="placed", error=None,
        )
        async with db._acquire() as conn:
            await conn.execute("UPDATE orders SET created_at = ? WHERE id = ?",
                               ((datetime.now(timezone.utc) - age).isoformat(), oid))
            await conn.commit()
    assert await db.proposal_for_open_position(
        "BTC_USDT", "long", observed_open_sig=77,
        observed_opened_at=int(datetime.now(timezone.utc).timestamp() * 1000),
    ) is None


@pytest.mark.asyncio
async def test_server_rejects_token_bound_to_another_network():
    client = _LaggingMexc()
    store = PreviewStore()
    service = OrderService(client, _settings(), store)
    preview = await service.preview(_ticket())
    changed = _settings().model_copy(update={"exchange": "hyperliquid"})
    with pytest.raises(OrderError, match="Execution network changed"):
        await OrderService(client, changed, store).confirm(preview["token"])
    assert client.place_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [{}, {"positionId": 999}, {"positionId": 77, "position_id": 999}])
async def test_terminal_evidence_requires_exact_position_identity(tmp_path, identity):
    db = Database(str(tmp_path / "missing-position-link.db"))
    await db.init()
    proposal_id = await db.insert_proposal(symbol="BTC_USDT", proposal_json={"thesis": "entry"})
    evidence = _recovered()
    evidence["order"].pop("positionId")
    evidence["order"].update(identity)
    client = _LaggingMexc()
    client.order_by_external_oid = AsyncMock(return_value=evidence)
    service = OrderService(client, _settings(), PreviewStore(), db=db)
    await service._claim_entry_reservation(
        "BTC_USDT", "long", "mlt-reservation", 0.0,
        _reservation_request(), proposal_id,
    )
    assert await service._has_entry_reservation("BTC_USDT", "long", positions=[_position()])
    assert await db.proposal_for_open_position("BTC_USDT", "long", observed_open_sig=77) is None


@pytest.mark.asyncio
async def test_existing_reservations_migrate_without_silently_clearing(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE entry_reservations (exchange_id TEXT, symbol TEXT, side TEXT, "
            "external_oid TEXT, pre_hold REAL, expires_at INTEGER, "
            "PRIMARY KEY(exchange_id,symbol,side))"
        )
        conn.execute(
            "INSERT INTO entry_reservations VALUES ('mexc','BTC_USDT','long','legacy',0,0)"
        )
    db = Database(str(path))
    await db.init()
    await db.init()  # Idempotent additive migration.
    row = await db.active_entry_reservation(exchange_id="mexc", symbol="BTC_USDT", side="long")
    assert row["external_oid"] == "legacy"
    assert row["requested_vol"] is None
    assert not await db.claim_entry_reservation(
        exchange_id="mexc", symbol="BTC_USDT", side="long", external_oid="new",
        pre_hold=0, ttl_seconds=120,
    )


def test_fallback_reservation_never_expires_with_preview_clock(monkeypatch):
    store = PreviewStore()
    assert store.claim_entry_reservation("mexc", "BTC_USDT", "long", "own", 0, 1)
    monkeypatch.setattr("app.orders.tokens.time.monotonic", lambda: 10**20)
    assert store.active_entry_reservation("mexc", "BTC_USDT", "long") is not None
    assert not store.claim_entry_reservation("mexc", "BTC_USDT", "long", "other", 0, 1)


@pytest.mark.asyncio
async def test_crash_before_submit_retires_only_unsubmitted_claims(tmp_path):
    db = Database(str(tmp_path / "phases.db"))
    await db.init()
    for symbol in ("BTC_USDT", "ETH_USDT"):
        await db.claim_entry_reservation(
            exchange_id="mexc", symbol=symbol, side="long", external_oid=symbol,
            pre_hold=0, ttl_seconds=120,
        )
    await db.mark_entry_submission_started(
        exchange_id="mexc", symbol="ETH_USDT", side="long", external_oid="ETH_USDT"
    )
    async with db._acquire() as conn:
        await conn.execute("UPDATE entry_reservations SET owner_id = 'previous-process'")
        await conn.commit()
    await db.init()
    assert await db.active_entry_reservation(exchange_id="mexc", symbol="BTC_USDT", side="long") is None
    assert await db.active_entry_reservation(exchange_id="mexc", symbol="ETH_USDT", side="long") is not None


@pytest.mark.asyncio
async def test_provenance_is_scoped_to_network_and_account(tmp_path):
    from types import SimpleNamespace

    db = Database(str(tmp_path / "contexts.db"))
    await db.init()
    contexts = [
        execution_context_id(SimpleNamespace(exchange_id="mexc", api_key="account-A")),
        execution_context_id(SimpleNamespace(exchange_id="mexc", api_key="account-a")),
        execution_context_id(SimpleNamespace(exchange_id="hyperliquid", testnet=True, account_address="0xabc")),
        execution_context_id(SimpleNamespace(exchange_id="hyperliquid", testnet=False, account_address="0xabc")),
    ]
    assert len(set(contexts)) == 4
    for context in contexts:
        proposal_id = await db.insert_proposal(symbol="BTC_USDT", proposal_json={"context": context})
        await db.bind_entry_proposal(
            "BTC_USDT", "long", open_sig=77, proposal_id=proposal_id,
            external_oid="own", execution_context=context,
        )
        result = await db.proposal_for_open_position(
            "BTC_USDT", "long", observed_open_sig=77, execution_context=context
        )
        assert result["id"] == proposal_id
    assert await db.proposal_for_open_position("BTC_USDT", "long", observed_open_sig=77) is None


@pytest.mark.asyncio
async def test_reservation_acknowledgement_cannot_replace_order_identity(tmp_path):
    db = Database(str(tmp_path / "ack.db"))
    await db.init()
    await db.claim_entry_reservation(
        exchange_id="mexc", symbol="BTC_USDT", side="long", external_oid="own",
        pre_hold=0, ttl_seconds=120,
    )
    kwargs = dict(exchange_id="mexc", symbol="BTC_USDT", side="long", external_oid="own")
    await db.set_entry_reservation_order_id(**kwargs, order_id=7)
    await db.set_entry_reservation_order_id(**kwargs, order_id=7)
    with pytest.raises(ValueError, match="conflict"):
        await db.set_entry_reservation_order_id(**kwargs, order_id=8)
    with pytest.raises(ValueError, match="invalid"):
        await db.set_entry_reservation_order_id(**kwargs, order_id=True)


@pytest.mark.asyncio
async def test_account_change_cannot_reuse_preview_authority():
    client = _LaggingMexc()
    client.api_key = "synthetic-account-A"
    store = PreviewStore()
    service = OrderService(client, _settings(), store)
    preview = await service.preview(_ticket())
    client.api_key = "synthetic-account-B"
    with pytest.raises(OrderError, match="Execution network changed"):
        await service.confirm(preview["token"])
    assert client.place_calls == 0
