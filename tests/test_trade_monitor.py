"""Task 5: server-side trade monitor — one-cycle behaviour (money-executing).

Drives `_run_one_cycle` directly (never the infinite loop) with a fake exchange
client + an in-memory DB. The auto-BE write path (`svc.modify_stop_loss`) is a
spy injected by monkeypatching `_make_order_service`, so no real order code runs.

Covered: armed HL at >=+1R executes auto-BE (+ be_done + feed) ; unarmed only
alerts ; disarmed never auto-acts ; a failing position doesn't abort the cycle ;
a non-HL position never auto-BEs.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repo import Database
from app.orders import monitor

NOW_MS = 1_700_000_000_000


class FakeClient:
    """Minimal exchange client. `is_hl` toggles the place_stop_order attribute
    that both the monitor and modify_stop_loss use as the HL-only gate."""

    def __init__(self, positions, mark=102.5, stops=None, is_hl=True):
        self._positions = positions
        self._mark = mark
        self._stops = stops if stops is not None else [
            {"triggerPrice": 98.0, "orderType": "Stop"}
        ]
        if is_hl:
            # Presence is all that matters (the real write goes via the spy).
            self.place_stop_order = lambda *a, **k: None

    async def account_snapshot(self, *, fresh=False):
        return {"positions": self._positions}

    async def ticker(self, symbol):
        return SimpleNamespace(last_price=self._mark)

    async def open_stop_orders(self, symbol):
        return list(self._stops)


def _pos(symbol="BTC_USDT", side="long", entry=100.0, hold=1.0):
    return {"symbol": symbol, "side": side, "entry_price": entry, "hold_vol": hold}


def _make_app(db, client):
    app = SimpleNamespace(state=SimpleNamespace())
    app.state.db = db
    app.state.mexc = client
    app.state.exchange = client
    app.state.preview_store = None
    app.state.trade_lock = None
    return app


def _install_spy(monkeypatch, result=None):
    """Replace the service seam with a spy exposing an AsyncMock modify_stop_loss.

    Default return mirrors a CONFIRMED-resting modify (``verified=True``) — the
    only return the monitor may treat as success (C2). Pass ``result`` to inject
    an unverified/soft-failure return.
    """
    if result is None:
        result = {"status": "modify_sl_ok", "verified": True}
    svc = SimpleNamespace(modify_stop_loss=AsyncMock(return_value=result))
    monkeypatch.setattr(monitor, "_make_order_service", lambda app, client, settings: svc)
    return svc


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "monitor.db")


async def _seed_open(db, *, armed, rules=None, opened_at=NOW_MS):
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "long",
        entry_snap=100.0,
        initial_sl_snap=98.0,
        r1=2.0,
        opened_at=opened_at,
        invalidation_price=None,
    )
    if armed:
        await db.set_armed_rules(
            "BTC_USDT", "long", rules if rules is not None else {"auto_be": True}
        )


@pytest.mark.asyncio
async def test_opened_at_skips_malformed_fill_rows_without_losing_valid_time():
    opened_at = NOW_MS - 5_000

    result = await monitor._best_effort_opened_at(
        SimpleNamespace(),
        None,
        "BTC_USDT",
        "long",
        NOW_MS,
        fills=["not-a-fill", {"dir": "Open Long", "time": opened_at}],
    )

    assert result == opened_at


@pytest.mark.asyncio
async def test_opened_at_rejects_boolean_fill_time():
    result = await monitor._best_effort_opened_at(
        SimpleNamespace(),
        None,
        "BTC_USDT",
        "long",
        NOW_MS,
        fills=[{"dir": "Open Long", "time": True}],
    )

    assert result == NOW_MS


@pytest.mark.asyncio
async def test_opened_at_rejects_non_string_fill_direction():
    result = await monitor._best_effort_opened_at(
        SimpleNamespace(),
        None,
        "BTC_USDT",
        "long",
        NOW_MS,
        fills=[{"dir": {"open": "long"}, "time": NOW_MS - 5_000}],
    )

    assert result == NOW_MS


@pytest.mark.asyncio
async def test_opened_at_requires_exact_open_fill_direction():
    result = await monitor._best_effort_opened_at(
        SimpleNamespace(),
        None,
        "BTC_USDT",
        "long",
        NOW_MS,
        fills=[{"dir": "Not Open Long", "time": NOW_MS - 5_000}],
    )

    assert result == NOW_MS


@pytest.mark.asyncio
async def test_mexc_baseline_does_not_fetch_unlinked_fill_history(db_path):
    db = Database(db_path)
    await db.init()
    client = FakeClient([], is_hl=False)
    client.user_fills = AsyncMock(return_value=[])

    await monitor.ensure_baseline(
        db,
        client,
        "BTC_USDT",
        "long",
        100.0,
        NOW_MS,
        pos={**_pos(), "position_id": 42},
        current_sl=98.0,
    )

    client.user_fills.assert_not_awaited()


@pytest.mark.asyncio
async def test_mexc_baseline_ignores_unlinked_historical_fill_age(db_path):
    db = Database(db_path)
    await db.init()

    await monitor.ensure_baseline(
        db,
        FakeClient([], is_hl=False),
        "BTC_USDT",
        "long",
        100.0,
        NOW_MS,
        pos={**_pos(), "position_id": 42},
        current_sl=98.0,
        fills=[{"dir": "Open Long", "time": NOW_MS - 86_400_000}],
    )

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["opened_at"] == NOW_MS


@pytest.mark.asyncio
async def test_current_sl_ignores_explicitly_foreign_mexc_stop():
    client = FakeClient(
        [],
        stops=[
            {
                "symbol": "ETH_USDT",
                "triggerPrice": 99.0,
                "orderType": "Stop",
            }
        ],
        is_hl=False,
    )
    client.exchange_id = "mexc"

    assert await monitor._current_sl(client, "BTC_USDT", "long", 100.0) == (
        None,
        True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_stops", [None, {}, ""])
async def test_current_sl_rejects_malformed_stop_collections(malformed_stops):
    client = FakeClient([])
    client.open_stop_orders = AsyncMock(return_value=malformed_stops)

    assert await monitor._current_sl(client, "BTC_USDT", "long", 100.0) == (
        None,
        False,
    )


@pytest.mark.asyncio
async def test_invalidation_uses_entry_proposal_not_latest_symbol_proposal():
    db = SimpleNamespace(
        proposal_for_open_position=AsyncMock(
            return_value={"proposal": {"invalidation_price": 98.0}}
        ),
        latest_proposal_for_symbol=AsyncMock(
            return_value={"proposal": {"invalidation_price": 105.0}}
        ),
    )

    result = await monitor._best_effort_invalidation(
        db,
        "BTC_USDT",
        "long",
        observed_entry=100.0,
        observed_open_sig=222,
        observed_opened_at=2_000,
        reopen_opened_at=2_000,
    )

    assert result == 98.0
    db.proposal_for_open_position.assert_awaited_once_with(
        "BTC_USDT",
        "long",
        observed_entry=100.0,
        observed_open_sig=222,
        observed_opened_at=2_000,
        reopen_opened_at=2_000,
    )
    db.latest_proposal_for_symbol.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "invalidation"),
    [
        ("long", 100.0),
        ("long", 105.0),
        ("short", 100.0),
        ("short", 95.0),
        ("long", 0.0),
        ("long", -1.0),
        ("long", float("nan")),
        ("long", float("inf")),
    ],
)
async def test_invalidation_rejects_invalid_or_wrong_side_level(side, invalidation):
    db = SimpleNamespace(
        proposal_for_open_position=AsyncMock(
            return_value={"proposal": {"invalidation_price": invalidation}}
        )
    )

    result = await monitor._best_effort_invalidation(
        db,
        "BTC_USDT",
        side,
        observed_entry=100.0,
        observed_opened_at=2_000,
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "invalidation"), [("long", 98.0), ("short", 102.0)])
async def test_invalidation_accepts_finite_loss_side_level(side, invalidation):
    db = SimpleNamespace(
        proposal_for_open_position=AsyncMock(
            return_value={"proposal": {"invalidation_price": invalidation}}
        )
    )

    result = await monitor._best_effort_invalidation(
        db,
        "BTC_USDT",
        side,
        observed_entry=100.0,
        observed_opened_at=2_000,
    )

    assert result == invalidation


@pytest.mark.parametrize(
    "position_id",
    [True, False, 0, -1, 1.5, "1.5", float("inf"), "9" * 5000],
)
def test_position_id_signature_rejects_non_positive_integer_ids(position_id):
    assert monitor._position_id_signature({"position_id": position_id}) is None


@pytest.mark.asyncio
async def test_hl_epoch_signature_ignores_malformed_fills():
    assert (
        await monitor._hl_epoch_signature(
            SimpleNamespace(), "BTC_USDT", "long", fills=object()
        )
        is None
    )
    fills = [
        "not-a-fill",
        {"dir": "Open Long", "start_position": 0, "time": "bad"},
        {"dir": "Open Long", "start_position": 0, "time": 1_000},
        {"dir": "Open Long", "start_position": 0, "time": 2_000},
    ]

    assert (
        await monitor._hl_epoch_signature(
            SimpleNamespace(), "BTC_USDT", "long", fills=fills
        )
        == 2_000
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_fill",
    [
        {"dir": "Open Long", "start_position": False, "time": 2_000},
        {"dir": "Open Long", "start_position": 0, "time": True},
    ],
)
async def test_hl_epoch_signature_rejects_boolean_epoch_fields(bad_fill):
    assert (
        await monitor._hl_epoch_signature(
            SimpleNamespace(), "BTC_USDT", "long", fills=[bad_fill]
        )
        is None
    )


@pytest.mark.asyncio
async def test_hl_epoch_signature_rejects_non_string_fill_direction():
    assert (
        await monitor._hl_epoch_signature(
            SimpleNamespace(),
            "BTC_USDT",
            "long",
            fills=[
                {
                    "dir": {"open": "long"},
                    "start_position": 0,
                    "time": 2_000,
                }
            ],
        )
        is None
    )


@pytest.mark.asyncio
async def test_hl_epoch_signature_requires_exact_open_fill_direction():
    assert (
        await monitor._hl_epoch_signature(
            SimpleNamespace(),
            "BTC_USDT",
            "long",
            fills=[
                {
                    "dir": "Not Open Long",
                    "start_position": 0,
                    "time": 2_000,
                }
            ],
        )
        is None
    )


@pytest.mark.asyncio
async def test_hl_epoch_signature_requires_exactly_flat_start_position():
    assert (
        await monitor._hl_epoch_signature(
            SimpleNamespace(),
            "BTC_USDT",
            "long",
            fills=[
                {
                    "dir": "Open Long",
                    "start_position": 1e-13,
                    "time": 2_000,
                }
            ],
        )
        is None
    )


def _flat_candles(n=30, close=100.0, tr=1.0):
    """Candles with a CONSTANT true range ``tr`` → Wilder ATR == ``tr``.

    (All closes equal ``close`` so each bar's TR = max(high-low, |high-pc|,
    |low-pc|) = high-low = tr.) compute_atr only reads .high/.low/.close.
    """
    half = tr / 2.0
    return [
        SimpleNamespace(high=close + half, low=close - half, close=close)
        for _ in range(n)
    ]


@pytest.mark.asyncio
async def test_nonboolean_auto_trail_rule_skips_atr_and_money_path(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_trail": "false"})
    service = _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=120.0, is_hl=True)
    client.klines = AsyncMock(return_value=_flat_candles())

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    client.klines.assert_not_awaited()
    service.modify_stop_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_armed_hl_at_1r_moves_sl_to_be(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(monkeypatch)
    # entry 100, SL 98 -> r1=2 ; mark 102.5 -> +1.25R, above the +1R trigger.
    client = FakeClient([_pos()], mark=102.5, is_hl=True)
    client.account_snapshot = AsyncMock(wraps=client.account_snapshot)
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    client.account_snapshot.assert_awaited_once_with(fresh=True)
    svc.modify_stop_loss.assert_awaited_once()
    kwargs = svc.modify_stop_loss.await_args.kwargs
    assert kwargs["symbol"] == "BTC_USDT"
    assert kwargs["side"] == "long"
    # BE = entry * (1 + fee_rt=0.0006) = 100.06.
    assert kwargs["new_sl"] == pytest.approx(100.06)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1
    assert "auto_be" in row["last_alert_state"]


@pytest.mark.asyncio
async def test_malformed_persisted_alert_state_does_not_block_auto_be(
    monkeypatch, db_path
):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    await db.set_alert_state("BTC_USDT", "long", "corrupt")
    service = _install_spy(monkeypatch)

    await monitor._run_one_cycle(
        _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True)), NOW_MS
    )

    service.modify_stop_loss.assert_awaited_once()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["last_alert_state"]["auto_be"]["active"] is True


@pytest.mark.asyncio
async def test_invalid_persisted_be_done_blocks_auto_stop_move(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    async with db._acquire() as conn:
        await conn.execute(
            """
            UPDATE position_management SET be_done = ''
            WHERE symbol = ? AND side = ? AND status = 'OPEN'
            """,
            ("BTC_USDT", "long"),
        )
        await conn.commit()
    service = _install_spy(monkeypatch)

    await monitor._run_one_cycle(
        _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True)), NOW_MS
    )

    service.modify_stop_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_unarmed_only_alerts_no_modify(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    opened_at = int(time.time() * 1000)
    original_id = await db.insert_proposal(
        symbol="BTC_USDT", proposal_json={"invalidation_price": 99.0}
    )
    await db.insert_order(
        symbol="BTC_USDT",
        side="long",
        request_json={"_proposal_id": original_id},
        response_json={"orderId": 1},
        status="placed",
        error=None,
    )
    await _seed_open(db, armed=False, opened_at=opened_at)
    # A newer unrelated analysis must not replace the entry's thesis. At mark
    # 98.5 the original invalidation (99) crosses, while the newer one (80) does not.
    await db.insert_proposal(
        symbol="BTC_USDT", proposal_json={"invalidation_price": 80.0}
    )
    svc = _install_spy(monkeypatch)
    app = _make_app(db, FakeClient([_pos()], mark=98.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert "thesis" in row["last_alert_state"]
    assert "auto_be" not in row["last_alert_state"]


@pytest.mark.asyncio
async def test_disarmed_position_no_auto_action(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=False)  # would be +1R but armed_rules empty
    svc = _install_spy(monkeypatch)
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0
    assert "auto_be" not in row["last_alert_state"]


@pytest.mark.asyncio
async def test_unidentified_position_row_blocks_all_auto_actions(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(monkeypatch)
    # An unidentified row could be a malformed duplicate of the valid position.
    # The whole snapshot is therefore unreliable for automatic money actions.
    app = _make_app(db, FakeClient(["not-a-dict", _pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_entry", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_entry_is_rejected_before_position_reads(
    monkeypatch, db_path, bad_entry
):
    db = Database(db_path)
    await db.init()
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos(entry=bad_entry)], mark=102.5, is_hl=True)
    client.open_stop_orders = AsyncMock(wraps=client.open_stop_orders)
    client.ticker = AsyncMock(wraps=client.ticker)
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    client.open_stop_orders.assert_not_awaited()
    client.ticker.assert_not_awaited()
    svc.modify_stop_loss.assert_not_awaited()
    assert await db.list_open_position_mgmt() == []


@pytest.mark.asyncio
async def test_boolean_entry_is_rejected_before_position_reads(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos(entry=True)], mark=2.0, stops=[{"triggerPrice": 0.5}])
    client.open_stop_orders = AsyncMock(wraps=client.open_stop_orders)
    client.ticker = AsyncMock(wraps=client.ticker)

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    client.open_stop_orders.assert_not_awaited()
    client.ticker.assert_not_awaited()
    svc.modify_stop_loss.assert_not_awaited()
    assert await db.list_open_position_mgmt() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mark", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_mark_is_rejected_without_state_or_order_change(
    monkeypatch, db_path, bad_mark
):
    db = Database(db_path)
    await db.init()
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=bad_mark, is_hl=True)
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    assert await db.list_open_position_mgmt() == []


@pytest.mark.asyncio
async def test_boolean_mark_is_rejected_before_auto_stop_move(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await db.upsert_position_mgmt(
        "BTC_USDT",
        "short",
        entry_snap=100.0,
        initial_sl_snap=102.0,
        r1=2.0,
        opened_at=NOW_MS,
        invalidation_price=None,
    )
    await db.set_armed_rules("BTC_USDT", "short", {"auto_be": True})
    svc = _install_spy(monkeypatch)
    client = FakeClient(
        [_pos(side="short")],
        mark=True,
        stops=[{"triggerPrice": 102.0, "orderType": "Stop"}],
        is_hl=True,
    )

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_hl_position_never_auto_bes(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(monkeypatch)
    # No place_stop_order attribute -> the HL-only gate blocks the write.
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=False))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0
    assert "auto_be_unavailable" in row["last_alert_state"]


@pytest.mark.asyncio
async def test_persistent_modify_failure_is_bounded(monkeypatch, db_path):
    """I-1: a persistently failing auto-BE must stop hammering after
    _BE_MAX_ATTEMPTS and go into a sticky halted state, not retry forever."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = SimpleNamespace(modify_stop_loss=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(monitor, "_make_order_service", lambda app, client, settings: svc)
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    # Run more cycles than the cap; every cycle the position is still live/armed/+1R.
    for _ in range(monitor._BE_MAX_ATTEMPTS + 3):
        await monitor._run_one_cycle(app, NOW_MS)

    # Exactly _BE_MAX_ATTEMPTS real modify attempts, then it stops calling.
    assert svc.modify_stop_loss.await_count == monitor._BE_MAX_ATTEMPTS
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0  # never succeeded → stays un-done
    assert row["last_alert_state"]["auto_be_error"]["halted"] is True


@pytest.mark.asyncio
async def test_transient_absence_does_not_disarm_before_grace(monkeypatch, db_path):
    """I-2: a single empty/partial snapshot must NOT close the mgmt record and
    wipe arming; only _CLOSE_GRACE_CYCLES consecutive absences close it."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=102.5, is_hl=True)
    app = _make_app(db, client)

    # Empty snapshot cycles (transient) — reuse the same app so absence counts persist.
    client._positions = []
    for i in range(monitor._CLOSE_GRACE_CYCLES - 1):  # one short of the grace
        await monitor._run_one_cycle(app, NOW_MS)
        row = await db.get_open_position_mgmt("BTC_USDT", "long")
        assert row is not None, "record closed too early on a transient glitch"
        assert row["armed_rules"] == {"auto_be": True}  # arming preserved

    # Reaching the grace threshold closes it.
    await monitor._run_one_cycle(app, NOW_MS)
    assert await db.get_open_position_mgmt("BTC_USDT", "long") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {},
        {"positions": None},
        {"positions": {}},
        {"positions": [None]},
        {"positions": [{}]},
        {"positions": [{"symbol": True, "side": "long"}]},
        {"positions": [{"symbol": "BTC_USDT", "side": "sideways"}]},
    ],
    ids=[
        "not-object",
        "missing",
        "null",
        "not-list",
        "invalid-row",
        "missing-identity",
        "invalid-symbol",
        "invalid-side",
    ],
)
async def test_invalid_snapshot_never_proves_position_absence(
    monkeypatch, db_path, snapshot
):
    """An unrecognized account shape is unknown, not evidence of a flat account."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    _install_spy(monkeypatch)
    client = FakeClient([], is_hl=True)
    client.account_snapshot = AsyncMock(return_value=snapshot)
    app = _make_app(db, client)

    for cycle in range(monitor._CLOSE_GRACE_CYCLES):
        await monitor._run_one_cycle(app, NOW_MS + cycle * 20_000)

    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row is not None
    assert row["armed_rules"] == {"auto_be": True}


@pytest.mark.asyncio
async def test_non_hl_unavailable_alert_is_debounced(monkeypatch, db_path):
    """A non-HL armed position surfaces 'auto_be_unavailable' ONCE; the ts must
    NOT be rewritten every cycle (that would re-toast the client each poll)."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=102.5, is_hl=False)  # no place_stop_order
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)
    row1 = await db.get_open_position_mgmt("BTC_USDT", "long")
    ts1 = row1["last_alert_state"]["auto_be_unavailable"]["ts"]
    assert ts1 == NOW_MS

    # Second cycle at a LATER time — the alert must stay at the original ts.
    await monitor._run_one_cycle(app, NOW_MS + 60_000)
    row2 = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row2["last_alert_state"]["auto_be_unavailable"]["ts"] == ts1


@pytest.mark.asyncio
async def test_sl_read_failure_never_moves_the_stop(monkeypatch, db_path):
    """F1: if the current-SL read RAISES (not 'no stop', a lookup hiccup), the
    monitor must NOT move the stop — moving on an unknown stop could loosen a
    well-trailed stop down to break-even. The frozen baseline keeps r1>0 so
    evaluate_rules would emit a move; the read-failure guard must drop it."""
    from unittest.mock import AsyncMock as _AM

    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)  # r1=2 baseline, armed
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=102.5, is_hl=True)  # +1.25R, would move
    client.open_stop_orders = _AM(side_effect=RuntimeError("hiccup"))  # read FAILS

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()  # never move on a failed SL read
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0  # not latched


# ── TML v2 (Task V4): Auto-Trailing wired into the monitor ────────────────────


@pytest.mark.asyncio
async def test_armed_trail_moves_sl_and_refires_no_latch(monkeypatch, db_path):
    """armed auto_trail HL, +>activation_r, HW set, ATR ok → modify_stop_loss with
    the Chandelier trail SL. Trailing has NO be_done latch: a rising high-water
    fires a fresh (tighter) trail move every cycle."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_trail": True})
    svc = _install_spy(monkeypatch)
    # entry 100, r1=2. atr=1, mult=2 → offset 2. mark 110 → hw 110 → trail 108.
    client = FakeClient([_pos()], mark=110.0, is_hl=True)
    client.klines = AsyncMock(return_value=_flat_candles(tr=1.0))
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    assert svc.modify_stop_loss.await_count == 1
    assert svc.modify_stop_loss.await_args.kwargs["new_sl"] == pytest.approx(108.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0  # trailing never latches be_done
    assert "auto_trail" in row["last_alert_state"]
    assert row["last_alert_state"]["auto_trail"]["message"] == (
        "App tightened the SL (trailing stop)."
    )
    assert row["high_water"] == pytest.approx(110.0)

    # Second cycle, higher mark → hw 112 → trail 110: a fresh, tighter move.
    client._mark = 112.0
    await monitor._run_one_cycle(app, NOW_MS + 20_000)
    assert svc.modify_stop_loss.await_count == 2
    assert svc.modify_stop_loss.await_args.kwargs["new_sl"] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_duplicate_position_row_cannot_repeat_trail_move(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_trail": True})
    svc = _install_spy(monkeypatch)
    position = _pos()
    client = FakeClient([position, dict(position)], mark=110.0, is_hl=True)
    client.klines = AsyncMock(return_value=_flat_candles(tr=1.0))

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()


@pytest.mark.asyncio
async def test_conflicting_duplicate_position_rows_block_auto_move(monkeypatch, db_path):
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_trail": True})
    svc = _install_spy(monkeypatch)
    position = _pos()
    conflicting = dict(position, entry_price=101.0)
    client = FakeClient([position, conflicting], mark=110.0, is_hl=True)
    client.klines = AsyncMock(return_value=_flat_candles(tr=1.0))

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    svc.modify_stop_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_atr_fetch_error_no_trail_move(monkeypatch, db_path):
    """Fail-safe: a klines/ATR fetch error → atr None → evaluate_rules emits no
    trail move → modify_stop_loss never called (never trail on an unknown ATR)."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_trail": True})
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=110.0, is_hl=True)
    client.klines = AsyncMock(side_effect=RuntimeError("klines down"))
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    client.klines.assert_awaited()  # it tried (armed trail)…
    svc.modify_stop_loss.assert_not_awaited()  # …but no ATR → no move


@pytest.mark.asyncio
async def test_non_trail_position_does_not_fetch_klines(monkeypatch, db_path):
    """Cost guard: a position WITHOUT auto_trail must never trigger a klines
    fetch — the ATR path is entered only for auto_trail-armed positions."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_be": True})  # BE only, no trail
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=102.5, is_hl=True)  # +1.25R → auto-BE fires
    client.klines = AsyncMock(return_value=_flat_candles())
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    client.klines.assert_not_awaited()  # NO klines for a non-trail position
    svc.modify_stop_loss.assert_awaited_once()  # auto-BE still ran


@pytest.mark.asyncio
async def test_high_water_updated_each_cycle_monotonic(monkeypatch, db_path):
    """High-water advances every cycle before rule evaluation, monotonically
    (long: never decreases)."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=False)
    _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=105.0, is_hl=True)
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))[
        "high_water"
    ] == pytest.approx(105.0)

    client._mark = 110.0
    await monitor._run_one_cycle(app, NOW_MS + 20_000)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))[
        "high_water"
    ] == pytest.approx(110.0)

    client._mark = 108.0  # pullback: high-water must NOT decrease
    await monitor._run_one_cycle(app, NOW_MS + 40_000)
    assert (await db.get_open_position_mgmt("BTC_USDT", "long"))[
        "high_water"
    ] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_multi_move_applies_only_most_protective(monkeypatch, db_path):
    """Nice-2: when BOTH Auto-BE and Auto-Trail emit a move in one cycle, only the
    MOST protective (long: highest new_sl) is applied via a SINGLE modify_stop_loss
    — never both sequentially (which could net-loosen the live stop). be_done is
    latched because a BE move was eligible."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_be": True, "auto_trail": True})
    svc = _install_spy(monkeypatch)
    # mark 110: BE ≈ 100.06, trail = hw(110) - 2*atr(1) = 108. Trail is tighter.
    client = FakeClient([_pos()], mark=110.0, is_hl=True)
    client.klines = AsyncMock(return_value=_flat_candles(tr=1.0))
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()  # exactly ONE move this cycle
    assert svc.modify_stop_loss.await_args.kwargs["new_sl"] == pytest.approx(108.0)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1  # BE was eligible → latched even though trail won
    assert "auto_trail" in row["last_alert_state"]


@pytest.mark.asyncio
async def test_atr_cache_dedups_klines_within_cycle(monkeypatch, db_path):
    """Cost guard: two auto_trail-armed positions on the SAME symbol fetch klines
    only ONCE per cycle — the (symbol, tf) ATR cache dedups the fetch."""
    from unittest.mock import AsyncMock as _AM

    db = Database(db_path)
    await db.init()
    for side, isl in (("long", 98.0), ("short", 102.0)):
        await db.upsert_position_mgmt(
            "BTC_USDT", side, entry_snap=100.0, initial_sl_snap=isl, r1=2.0,
            opened_at=NOW_MS, invalidation_price=None,
        )
        await db.set_armed_rules("BTC_USDT", side, {"auto_trail": True})
    _install_spy(monkeypatch)
    client = FakeClient([_pos(side="long"), _pos(side="short")], mark=102.5, is_hl=True)
    client.klines = _AM(return_value=_flat_candles(tr=1.0))

    await monitor._run_one_cycle(_make_app(db, client), NOW_MS)

    # Both positions are auto_trail-armed on the same symbol/tf → exactly ONE fetch.
    assert client.klines.await_count == 1


@pytest.mark.asyncio
async def test_atr_cache_ttl_uses_monotonic_time(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    settings = SimpleNamespace(
        tm_trail_atr_tf="15m",
        tm_trail_atr_period=14,
        tm_monitor_interval_s=20,
    )
    client = FakeClient([])
    client.klines = AsyncMock(
        side_effect=[_flat_candles(tr=1.0), _flat_candles(tr=2.0)]
    )
    ticks = iter([1_000.0, 1_000.0, 1_021.0, 1_021.0])
    fake_time = SimpleNamespace(monotonic=lambda: next(ticks))
    monkeypatch.setattr(monitor, "time", fake_time)

    first = await monitor._atr_for(app, client, settings, "BTC_USDT", 1_000_000)
    second = await monitor._atr_for(app, client, settings, "BTC_USDT", 100_000)

    assert first == pytest.approx(1.0)
    assert second == pytest.approx(2.0)
    assert client.klines.await_count == 2


@pytest.mark.asyncio
async def test_atr_cache_ttl_starts_after_slow_refresh(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    settings = SimpleNamespace(
        tm_trail_atr_tf="15m",
        tm_trail_atr_period=14,
        tm_monitor_interval_s=20,
    )
    client = FakeClient([])
    client.klines = AsyncMock(return_value=_flat_candles(tr=1.0))
    ticks = iter([1_000.0, 1_021.0, 1_021.0, 1_021.0])
    fake_time = SimpleNamespace(monotonic=lambda: next(ticks))
    monkeypatch.setattr(monitor, "time", fake_time)

    first = await monitor._atr_for(app, client, settings, "BTC_USDT", 1_000_000)
    second = await monitor._atr_for(app, client, settings, "BTC_USDT", 1_000_000)

    assert first == pytest.approx(1.0)
    assert second == pytest.approx(1.0)
    assert client.klines.await_count == 1


# ── C2: modify_stop_loss SOFT-failure return must not latch a false be_done ───


@pytest.mark.asyncio
async def test_unverified_modify_does_not_latch_be(monkeypatch, db_path):
    """C2: a NON-exception but UNVERIFIED modify return
    (``verified=False`` / "modify_sl_unverified_old_kept") means the NEW stop is
    NOT confirmed resting and the OLD looser stop is still held. The monitor must
    NOT latch be_done, must NOT write the "App moved the SL" feed, must
    write an honest non-"done" feed, and auto-BE must stay ELIGIBLE next cycle
    (re-attempting) — counting toward the attempt cap so it can't hammer forever."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)  # entry 100, r1=2, auto_be
    svc = _install_spy(
        monkeypatch,
        result={"verified": False, "status": "modify_sl_unverified_old_kept"},
    )
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))  # +1.25R

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()  # it TRIED to move
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 0  # NOT latched on an unverified move
    assert "auto_be" not in row["last_alert_state"]  # no false "moved" feed
    assert "auto_be_error" in row["last_alert_state"]  # honest error feed instead
    assert row["last_alert_state"]["auto_be_error"]["halted"] is False
    message = row["last_alert_state"]["auto_be_error"]["message"]
    assert "SL move not confirmed" in message
    assert "NICHT bestaetigt" not in message

    # Still eligible next cycle → it re-attempts (auto-BE not stuck done).
    await monitor._run_one_cycle(app, NOW_MS + 20_000)
    assert svc.modify_stop_loss.await_count == 2
    row2 = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row2["be_done"] == 0


@pytest.mark.asyncio
async def test_verified_modify_latches_be(monkeypatch, db_path):
    """C2 companion: a VERIFIED modify return (``verified=True``) DOES latch
    be_done and writes the "moved" feed — the confirmed-success path."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    svc = _install_spy(
        monkeypatch, result={"verified": True, "status": "modify_sl_ok"}
    )
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["be_done"] == 1  # latched on a CONFIRMED move
    assert "auto_be" in row["last_alert_state"]
    assert "App moved the SL to break-even" in row["last_alert_state"]["auto_be"][
        "message"
    ]
    assert "auto_be_error" not in row["last_alert_state"]


@pytest.mark.asyncio
async def test_verified_be_with_latch_failure_does_not_repeat_mutation(
    monkeypatch, db_path
):
    """A verified stop plus failed DB latch must halt, not send it every cycle."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True)
    db.mark_be_done = AsyncMock(side_effect=RuntimeError("sqlite unavailable"))
    svc = _install_spy(
        monkeypatch, result={"verified": True, "status": "modify_sl_ok"}
    )
    # The stop endpoint deliberately remains one cycle behind at the old SL.
    app = _make_app(db, FakeClient([_pos()], mark=102.5, is_hl=True))

    await monitor._run_one_cycle(app, NOW_MS)
    await monitor._run_one_cycle(app, NOW_MS + 20_000)

    assert svc.modify_stop_loss.await_count == 1
    assert app.state.tm_be_attempts[("BTC_USDT", "long")] == monitor._BE_MAX_ATTEMPTS
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row["last_alert_state"]["auto_be_error"]["halted"] is True


@pytest.mark.asyncio
async def test_unverified_trail_move_writes_no_done_feed(monkeypatch, db_path):
    """C2 for trailing: an unverified trail return must NOT write the
    "App tightened the SL" feed (a false "done") — honest error only."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_trail": True})
    svc = _install_spy(
        monkeypatch,
        result={"verified": False, "status": "modify_sl_unverified_old_kept"},
    )
    client = FakeClient([_pos()], mark=110.0, is_hl=True)  # hw 110 → trail 108
    client.klines = AsyncMock(return_value=_flat_candles(tr=1.0))
    app = _make_app(db, client)

    await monitor._run_one_cycle(app, NOW_MS)

    svc.modify_stop_loss.assert_awaited_once()
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert "auto_trail" not in row["last_alert_state"]  # no false "moved" feed
    assert "auto_be_error" in row["last_alert_state"]
    assert row["be_done"] == 0  # trailing never latches anyway


# ── C3: a same-price reopen within the absence grace gets a fresh baseline ─────


@pytest.mark.asyncio
async def test_reopen_after_absence_resets_be_and_high_water(monkeypatch, db_path):
    """C3: when a position vanishes for < grace cycles (record NOT closed) and
    then REAPPEARS at ~the same entry, the fresh position must NOT inherit the old
    be_done latch or a stale high_water. Reappearance-after-absence resets both."""
    db = Database(db_path)
    await db.init()
    await _seed_open(db, armed=True, rules={"auto_be": True})
    # Simulate the PRIOR trade's finished state on the still-open record.
    await db.mark_be_done("BTC_USDT", "long")
    await db.update_high_water("BTC_USDT", "long", 130.0)  # stale extreme
    svc = _install_spy(monkeypatch)
    client = FakeClient([_pos()], mark=101.0, is_hl=True)  # +0.5R, below +1R
    app = _make_app(db, client)

    # One ABSENT cycle (below grace=2 → record survives, state untouched).
    client._positions = []
    await monitor._run_one_cycle(app, NOW_MS)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row is not None  # not closed yet (transient tolerance)
    assert row["be_done"] == 1  # still stale — absence alone doesn't reset

    # REAPPEARS at the same entry → reopen → baseline reset BEFORE processing.
    client._positions = [_pos()]
    await monitor._run_one_cycle(app, NOW_MS + 20_000)
    row = await db.get_open_position_mgmt("BTC_USDT", "long")
    assert row is not None
    assert row["be_done"] == 0  # stale BE latch cleared → auto-BE eligible again
    # high_water re-seeded to entry (100) then advanced to the live mark (101),
    # NOT the stale 130 from the prior trade.
    assert row["high_water"] == pytest.approx(101.0)
    svc.modify_stop_loss.assert_not_awaited()  # +0.5R < +1R → no move yet
