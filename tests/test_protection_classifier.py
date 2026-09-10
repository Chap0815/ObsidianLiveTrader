"""Q-05: the backend SL/TP classifier is a single source of truth.

Both money-path call sites — the advisory reevaluate extractor
(`app.main._extract_position_sl_tp` → `classify_protection`) and the
auto-flatten verifier (`OrderService._verify_sl_attached`) — must answer
"is this trigger an SL?" the same way for the same input.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.orders.protection import classify_protection
from app.orders.service import OrderService, _sl_matches
from app.orders.tokens import PreviewStore

from tests.test_orders_flow import _settings


def test_classify_prefers_explicit_field():
    """An explicit stopLossPrice wins over the side+entry price heuristic.

    The row also carries a triggerPrice (101_000) that the geometry heuristic
    would classify as a TAKE-PROFIT for a long at entry 100_000 — the explicit
    stopLossPrice field must still make it an SL and the stray trigger ignored.
    """
    sl, tp = classify_protection(
        [{"stopLossPrice": 95_000.0, "triggerPrice": 101_000.0}],
        side="long",
        entry=100_000.0,
    )
    assert sl == 95_000.0
    assert tp is None


def test_classify_combined_explicit_sl_tp_returns_both():
    sl, tp = classify_protection(
        [{"stopLossPrice": 95_000.0, "takeProfitPrice": 110_000.0}],
        side="long",
        entry=100_000.0,
    )

    assert sl == 95_000.0
    assert tp == 110_000.0


def test_classify_rejects_conflicting_position_side_aliases():
    sl, tp = classify_protection(
        [
            {
                "positionType": 1,
                "position_type": 2,
                "stopLossPrice": 95_000.0,
            }
        ],
        side="long",
        entry=100_000.0,
    )

    assert sl is None
    assert tp is None


def test_classify_label_then_price():
    """An explicit orderType label beats the side+entry heuristic.

    - "Stop" trigger ABOVE a long entry (heuristic would say TP) → SL.
    - "Take Profit" trigger BELOW a long entry (heuristic would say SL) → TP.
    """
    sl, tp = classify_protection(
        [{"triggerPrice": 101_000.0, "orderType": "Stop"}],
        side="long",
        entry=100_000.0,
    )
    assert sl == 101_000.0
    assert tp is None

    sl2, tp2 = classify_protection(
        [{"triggerPrice": 99_000.0, "orderType": "Take Profit"}],
        side="long",
        entry=100_000.0,
    )
    assert tp2 == 99_000.0
    assert sl2 is None


@pytest.mark.asyncio
async def test_reevaluate_and_verify_agree():
    """Same stop order → both call sites agree the SL is present at 95_000.

    The reevaluate extractor classifies the labeled stop as SL == 95_000, and
    the auto-flatten verifier confirms an SL near 95_000 is attached. They can
    no longer diverge into "protected" vs "SL missing → flatten".
    """
    stops = [{"symbol": "BTC_USDT", "orderType": "Stop", "triggerPrice": 95_000.0}]

    # (a) Reevaluate extractor
    sl, tp = classify_protection(stops, side="long", entry=100_000.0)
    assert sl == 95_000.0
    assert tp is None

    # (b) Auto-flatten verifier — same stop list from the exchange
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=stops)
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )
    verified, detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )
    assert verified is True
    assert checked is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "expected_verified"),
    [
        (
            {
                "symbol": "BTC_USDT",
                "orderType": "Stop",
                "stopLossPrice": 90_000.0,
                "triggerPrice": 95_000.0,
            },
            False,
        ),
        (
            {
                "symbol": "BTC_USDT",
                "orderType": "Take Profit",
                "stopLossPrice": 95_000.0,
                "triggerPrice": 105_000.0,
            },
            True,
        ),
    ],
    ids=["explicit-sl-mismatch-wins", "explicit-sl-match-wins"],
)
async def test_entry_sl_verify_honors_explicit_sl_field_priority(
    stop, expected_verified
):
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=[stop])
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is expected_verified
    assert checked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_symbol", [None, "ETH_USDT", "BTC_USDC"])
async def test_entry_sl_verify_requires_matching_stop_symbol(reported_symbol):
    stop = {"orderType": "Stop", "triggerPrice": 95_000.0}
    if reported_symbol is not None:
        stop["symbol"] = reported_symbol
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=[stop])
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is False
    assert checked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_stops", [None, {}, "", [None]])
async def test_entry_sl_verify_treats_malformed_stop_collection_as_unknown(
    malformed_stops,
):
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=malformed_stops)
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is False
    assert checked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_positions", [None, [None]])
async def test_entry_sl_verify_treats_malformed_position_fallback_as_unknown(
    malformed_positions,
):
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=None)
    client.positions = AsyncMock(return_value=malformed_positions)
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is False
    assert checked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [None, True, "garbage", "NaN", 0, -1])
async def test_mexc_verify_does_not_count_invalid_stop_price_as_checked(bad_price):
    client = MagicMock()
    client.exchange_id = "mexc"
    client.open_stop_orders = AsyncMock(
        return_value=[{"symbol": "BTC_USDT", "stopLossPrice": bad_price}]
    )
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is False
    assert checked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [True, "garbage", "NaN", 0, -1])
async def test_mexc_verify_does_not_count_invalid_position_sl_as_checked(bad_price):
    client = MagicMock()
    client.exchange_id = "mexc"
    client.open_stop_orders = AsyncMock(return_value=[])
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "hold_vol": 1.0,
                "stopLossPrice": bad_price,
            }
        ]
    )
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is False
    assert checked is False


@pytest.mark.asyncio
async def test_mexc_verify_rejects_conflicting_position_sl_aliases():
    client = MagicMock()
    client.exchange_id = "mexc"
    client.open_stop_orders = AsyncMock(return_value=[])
    client.positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTC_USDT",
                "side": "long",
                "hold_vol": 1.0,
                "stopLossPrice": 95_000.0,
                "stop_loss": 90_000.0,
            }
        ]
    )
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )

    assert verified is False
    assert checked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_stops", [None, {}, "", [None]])
async def test_modify_sl_oid_verify_treats_malformed_stop_collection_as_unknown(
    malformed_stops,
):
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=malformed_stops)
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )

    verified, _detail, checked = await svc._verify_sl_oid(
        "BTC_USDT", 555, 95_000.0, 1.0, side="long"
    )

    assert verified is False
    assert checked is False


def test_classify_multiple_sl_long_picks_most_protective_highest():
    """C1: with MULTIPLE resting SL orders, classify_protection must report the
    MOST-protective one, never last-wins.

    modify_stop_loss can legitimately leave TWO stops resting
    (modify_sl_ok_old_cancel_failed / modify_sl_unverified_old_kept). For a
    LONG the higher stop is the tighter/real protection. If the exchange returns
    the LOOSE old stop LAST, last-wins would under-report current_sl → the
    auto-trail monitor could compute a trail between the two and then cancel
    BOTH → live protection drops (118 → 105). The classifier must pick 118.
    """
    stops = [
        {"orderType": "Stop", "triggerPrice": 118.0},  # tight/real
        {"orderType": "Stop", "triggerPrice": 105.0},  # loose old — LAST
    ]
    sl, tp = classify_protection(stops, side="long", entry=120.0)
    assert sl == 118.0
    assert tp is None


def test_classify_multiple_sl_short_picks_most_protective_lowest():
    """C1 (short): for a SHORT the LOWER stop is the tighter/real protection.

    Loose old stop (130) returned LAST would win under last-wins → under-report.
    Must pick the lowest (112).
    """
    stops = [
        {"orderType": "Stop", "triggerPrice": 112.0},  # tight/real
        {"orderType": "Stop", "triggerPrice": 130.0},  # loose old — LAST
    ]
    sl, tp = classify_protection(stops, side="short", entry=100.0)
    assert sl == 112.0
    assert tp is None


def test_classify_multiple_sl_explicit_field_most_protective():
    """C1: the most-protective rule also holds for explicit stopLossPrice fields."""
    stops = [
        {"stopLossPrice": 118.0},
        {"stopLossPrice": 105.0},  # loose — LAST
    ]
    sl, _tp = classify_protection(stops, side="long", entry=120.0)
    assert sl == 118.0


def test_classify_protection_ignores_nonfinite_prices():
    stops = [
        {"stopLossPrice": "NaN"},
        {"takeProfitPrice": "Infinity"},
        {"orderType": "Stop", "triggerPrice": "-Infinity"},
        {"orderType": "Stop", "triggerPrice": 95.0},
    ]
    sl, tp = classify_protection(stops, side="long", entry=100.0)
    assert sl == 95.0
    assert tp is None


def test_classify_protection_filters_opposite_hedge_side():
    stops = [
        {"positionType": 1, "stopLossPrice": 98.0},
        {"positionType": 2, "stopLossPrice": 102.0},
    ]

    assert classify_protection(stops, side="long", entry=100.0) == (98.0, None)
    assert classify_protection(stops, side="short", entry=100.0) == (102.0, None)


@pytest.mark.parametrize(
    "row",
    [
        {"stopLossPrice": True},
        {"takeProfitPrice": True},
        {"orderType": "Stop", "triggerPrice": True},
    ],
)
def test_classify_protection_ignores_boolean_prices(row):
    sl, tp = classify_protection([row], side="long", entry=100.0)

    assert sl is None
    assert tp is None


def test_invalid_explicit_protection_does_not_fall_back_to_trigger():
    sl, tp = classify_protection(
        [{"stopLossPrice": True, "orderType": "Stop", "triggerPrice": 95.0}],
        side="long",
        entry=100.0,
    )

    assert sl is None
    assert tp is None


def test_classify_protection_skips_malformed_rows():
    sl, tp = classify_protection(
        [None, {"orderType": "Stop", "triggerPrice": 95.0}],
        side="long",
        entry=100.0,
    )

    assert sl == 95.0
    assert tp is None


def test_classify_protection_rejects_non_string_order_label():
    sl, tp = classify_protection(
        [{"orderType": True, "triggerPrice": 95.0}],
        side="long",
        entry=100.0,
    )

    assert sl is None
    assert tp is None


def test_unlabeled_trigger_rejects_nonfinite_entry():
    sl, tp = classify_protection(
        [{"triggerPrice": 95.0}], side="long", entry=float("nan")
    )
    assert sl is None
    assert tp is None


def test_unlabeled_trigger_rejects_boolean_entry():
    sl, tp = classify_protection(
        [{"triggerPrice": 95.0}], side="long", entry=True
    )

    assert sl is None
    assert tp is None


@pytest.mark.parametrize(
    ("expected", "candidate"),
    [
        (95.0, "garbage"),
        (95.0, "NaN"),
        (95.0, "Infinity"),
        (float("nan"), 95.0),
        (float("inf"), 95.0),
    ],
)
def test_sl_matcher_rejects_invalid_or_nonfinite_prices(expected, candidate):
    assert _sl_matches(expected, candidate) is False


@pytest.mark.parametrize(("expected", "candidate"), [(True, 1.0), (1.0, True)])
def test_sl_matcher_rejects_boolean_prices(expected, candidate):
    assert _sl_matches(expected, candidate) is False


@pytest.mark.asyncio
async def test_mexc_tpsl_combined_order_is_sl_not_tp():
    """Regression-Pin fuer DIE Divergenz, die Task 19 motiviert hat (Q-05):

    MEXC "tpsl" (kombinierte Order mit echtem Stop) MUSS als SL klassifiziert
    werden. Die alte Reevaluate-Regel (startswith("tp")) haette sie als TP
    fehlklassifiziert -> Verify haette FAELSCHLICH "SL fehlt" gemeldet ->
    Auto-Flatten einer geschuetzten Position. Wer classify_order_label je
    wieder auf ein startswith("tp")-Muster "vereinfacht", macht diesen Test rot.
    """
    stops = [{"symbol": "BTC_USDT", "orderType": "tpsl", "triggerPrice": 95_000.0}]

    # (a) Klassifizierer direkt
    sl, tp = classify_protection(stops, side="long", entry=100_000.0)
    assert sl == 95_000.0
    assert tp is None

    # (b) Verify-Pfad: tpsl-Stop wird als SL erkannt -> verified
    client = MagicMock()
    client.exchange_id = "hyperliquid"
    client.open_stop_orders = AsyncMock(return_value=stops)
    client.positions = AsyncMock(return_value=[])
    svc = OrderService(
        client, _settings(sl_verify_attempts=1, sl_verify_delay_s=0.0), PreviewStore()
    )
    verified, detail, checked = await svc._verify_sl_attached(
        symbol="BTC_USDT", expected_sl=95_000.0, side="long"
    )
    assert verified is True
    assert checked is True
