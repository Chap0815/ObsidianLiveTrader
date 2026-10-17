"""Regression tests for the 2026-10 production audit findings."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.db.repo import Database
from app.models import ContractMeta, OrderTicket, Ticker
from app.orders.service import OrderService
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
        observed_opened_at=int(datetime.now(timezone.utc).timestamp() * 1000),
    )

    assert resolved is not None
    assert resolved["id"] == proposal_id
