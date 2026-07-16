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
from app.orders.service import OrderService
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
