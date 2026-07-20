"""Task 3 (O-03): MEXC price fields must serialize as fixed decimal strings,
never Python's scientific `e` notation, since the signed POST body is the
raw JSON text sent to the exchange (json.dumps of a small float like
0.00002 produces "2e-05", which MEXC's exchange-side parser can reject or
misinterpret for low-price coins such as SHIB/PEPE)."""

import re

import httpx
import pytest

from app.mexc.client import MexcClient, _fmt_price, _opt_float, _opt_int, map_position
from app.mexc.errors import MexcError

# Scientific notation looks like "2e-05" / "1.2E+10" — a digit directly
# followed by e/E and an exponent. Field names like "leverage" also contain
# a bare "e", so the check must be exponent-shaped, not a blanket substring.
_SCI_NOTATION = re.compile(r"\d[eE][+-]?\d")


def _mock_client(capture: dict) -> MexcClient:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["content"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"success": True, "data": {"orderId": 1}})

    c = MexcClient("https://contract.mexc.com", "k", "s")
    c._client = httpx.AsyncClient(
        base_url=c.base_url, transport=httpx.MockTransport(handler)
    )
    return c


@pytest.mark.asyncio
async def test_mexc_price_no_scientific_notation():
    # Helper itself: sub-cent price must never come out as "2e-05".
    assert _fmt_price(0.00002) == "0.00002"

    capture: dict = {}
    c = _mock_client(capture)
    body = {
        "symbol": "SHIB_USDT",
        "vol": 1000,
        "side": 1,
        "type": 1,
        "openType": 1,
        "leverage": 10,
        "price": 0.00002,
        "stopLossPrice": 0.000019,
        "takeProfitPrice": 0.0000215,
    }
    await c.place_order(body)

    sent = capture["content"]
    assert not _SCI_NOTATION.search(sent)
    assert '"price":"0.00002"' in sent
    assert '"stopLossPrice":"0.000019"' in sent
    assert '"takeProfitPrice":"0.0000215"' in sent
    # Non-price fields stay untouched (still bare JSON numbers).
    assert '"vol":1000' in sent


def test_fmt_price_rejects_non_finite():
    # Infinity/NaN must not reach Decimal.quantize (raises InvalidOperation,
    # which is not a MexcError and would bypass order-error handling).
    with pytest.raises(MexcError):
        _fmt_price(float("inf"))
    with pytest.raises(MexcError):
        _fmt_price(float("nan"))


@pytest.mark.asyncio
async def test_mexc_price_roundtrip_normal_coin():
    """A normal BTC-sized price must not lose precision or gain noise digits."""
    capture: dict = {}
    c = _mock_client(capture)
    body = {
        "symbol": "BTC_USDT",
        "vol": 1,
        "side": 1,
        "type": 1,
        "openType": 1,
        "leverage": 10,
        "price": 65432.15,
        "stopLossPrice": 64000,
    }
    await c.place_order(body)

    sent = capture["content"]
    assert not _SCI_NOTATION.search(sent)
    assert '"price":"65432.15"' in sent
    assert '"stopLossPrice":"64000"' in sent


# --- Task 4 (O-05/O-09/O-04): recovery window, match-marker, path cache ---


def _client_with_handler(handler) -> MexcClient:
    c = MexcClient("https://contract.mexc.com", "k", "s")
    c._client = httpx.AsyncClient(
        base_url=c.base_url, transport=httpx.MockTransport(handler)
    )
    return c


@pytest.mark.asyncio
async def test_mexc_order_by_external_oid_marks_history_match():
    """History contains the order (only reachable via the widened window,
    i.e. beyond the old page_size=20) -> returns match="history" + externalOid,
    never a fabricated/empty match."""
    target_oid = "cli-abc-123"
    # 25 unrelated rows (would NOT fit in the old page_size=20 window) followed
    # by the real match — proves the window was actually widened, not just the
    # marker added.
    rows = [{"externalOid": f"other-{i}", "orderId": i} for i in range(25)]
    rows.append({"externalOid": target_oid, "orderId": 999, "state": "filled"})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if path == "/api/v1/private/order/list/history_orders":
            page_size = int(request.url.params.get("page_size", "0"))
            page_num = int(request.url.params.get("page_num", "1"))
            assert page_size >= 100, "history window must be widened past 20"
            if page_num == 1:
                return httpx.Response(200, json={"resultList": rows})
            return httpx.Response(200, json={"resultList": []})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    result = await c.order_by_external_oid("BTC_USDT", target_oid)

    assert result["match"] == "history"
    assert result["externalOid"] == target_oid
    assert result["order"]["orderId"] == 999


@pytest.mark.asyncio
async def test_mexc_order_by_external_oid_checks_open_orders():
    """Not in history at all, but present in open_orders -> found with
    match="open" (fail-closed: only returns a match when the oid actually
    appears in the data)."""
    target_oid = "cli-xyz-789"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if path == "/api/v1/private/order/list/history_orders":
            return httpx.Response(200, json={"resultList": []})
        if path == "/api/v1/private/order/list/open_orders":
            return httpx.Response(
                200,
                json={
                    "resultList": [
                        {"externalOid": target_oid, "orderId": 42, "state": "new"}
                    ]
                },
            )
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    result = await c.order_by_external_oid("BTC_USDT", target_oid)

    assert result["match"] == "open"
    assert result["externalOid"] == target_oid
    assert result["order"]["orderId"] == 42


@pytest.mark.asyncio
async def test_mexc_recovery_retries_before_giving_up():
    """X2-03: a just-filled order can be absent from BOTH history and open during
    MEXC index-lag. order_by_external_oid must SETTLE-RETRY history+open a few
    times (short delay) before returning {} — so the fill is found once the index
    catches up, instead of a premature {} → hard error → user re-preview →
    double position."""
    target_oid = "cli-retry-1"
    calls = {"open": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if path == "/api/v1/private/order/list/history_orders":
            return httpx.Response(200, json={"resultList": []})
        if path == "/api/v1/private/order/list/open_orders":
            calls["open"] += 1
            # Absent on the first two polls (index-lag), appears on the third.
            if calls["open"] >= 3:
                return httpx.Response(
                    200,
                    json={
                        "resultList": [
                            {"externalOid": target_oid, "orderId": 7, "state": 2}
                        ]
                    },
                )
            return httpx.Response(200, json={"resultList": []})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    c._RECOVERY_RETRY_DELAY_S = 0.0  # keep the test fast — no real 1s sleeps
    result = await c.order_by_external_oid("BTC_USDT", target_oid)

    assert calls["open"] == 3, "must retry history+open before giving up"
    assert result["match"] == "open"
    assert result["externalOid"] == target_oid
    assert result["order"]["orderId"] == 7


@pytest.mark.asyncio
async def test_mexc_recovery_rejects_cancelled_state():
    """X2-04: a match whose MEXC state is cancelled(4)/invalid(5) is NOT live and
    must NOT be reported as recovered → order_by_external_oid returns {} (so the
    caller fail-closes instead of claiming a cancelled order is live)."""
    target_oid = "cli-cancel-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if path == "/api/v1/private/order/list/history_orders":
            # Our oid IS present, but the order was cancelled (state 4).
            return httpx.Response(
                200,
                json={
                    "resultList": [
                        {"externalOid": target_oid, "orderId": 9, "state": 4}
                    ]
                },
            )
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    c._RECOVERY_RETRY_DELAY_S = 0.0
    result = await c.order_by_external_oid("BTC_USDT", target_oid)

    assert result == {}, "cancelled order must never be reported recovered/live"


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_caches_working_path():
    """After the first successful call establishes which stop-order-list path
    actually works, the second call must try that cached path FIRST (fewer
    calls / different call order), instead of re-probing every candidate."""
    working_path = MexcClient._STOP_ORDER_PATHS[2]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == working_path:
            return httpx.Response(200, json={"resultList": []})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)

    # First call: no cache yet, must probe candidates in default order until
    # it reaches the working one (index 2 -> 3 calls: 0, 1, 2).
    await c.open_stop_orders("BTC_USDT")
    assert calls == list(MexcClient._STOP_ORDER_PATHS[:3])
    assert c._stop_path_cache == working_path

    # Second call: cached path must be tried FIRST -> exactly one call, and
    # it's the working path (not the original index-0 candidate).
    calls.clear()
    await c.open_stop_orders("BTC_USDT")
    assert calls == [working_path]


# --- Task 32 (C3-01): user_fills() — MEXC fill-marker parity ---


@pytest.mark.asyncio
async def test_mexc_user_fills_normalizes_shape_like_hyperliquid():
    """MEXC's order_deals rows (numeric side code, `profit` per deal) must
    normalize to the EXACT same shape Hyperliquid's user_fills produces
    (symbol, px, sz, side, time(ms), dir, closed_pnl, oid, fee) — the shared
    frontend marker pipeline (classifyFillDir etc.) has no MEXC special-case
    and only works if the fields line up field-for-field."""
    raw_rows = [
        {
            "id": "9001",
            "symbol": "BTC_USDT",
            "side": 1,  # open long -> buy
            "vol": 0.5,
            "price": 65000.5,
            "fee": 0.02,
            "feeCurrency": "USDT",
            "timestamp": 1710000005000,
            "profit": 0,
            "orderId": "555",
        },
        {
            "id": "9002",
            "symbol": "BTC_USDT",
            "side": 4,  # close long -> sell
            "vol": 0.5,
            "price": 66000.0,
            "fee": 0.03,
            "timestamp": 1710000009000,
            "profit": 500.25,
            "orderId": "556",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/private/order/list/order_deals"
        return httpx.Response(200, json={"resultList": raw_rows})

    c = _client_with_handler(handler)
    out = await c.user_fills(symbol="BTC_USDT", limit=100)

    assert len(out) == 2
    # Newest first (time descending), same ordering contract as HL.
    assert out[0]["time"] == 1710000009000
    assert out[0]["symbol"] == "BTC_USDT"
    assert out[0]["px"] == 66000.0
    assert out[0]["sz"] == 0.5
    assert out[0]["side"] == "sell"
    assert "close" in out[0]["dir"].lower()
    assert out[0]["closed_pnl"] == 500.25
    assert out[0]["oid"] == "556"
    assert out[0]["fee"] == 0.03

    opened = out[1]
    assert opened["side"] == "buy"
    assert "open" in opened["dir"].lower()
    assert opened["closed_pnl"] == 0.0


@pytest.mark.asyncio
async def test_mexc_user_fills_skips_unparseable_rows():
    """A row missing price/vol must be dropped, not raise/crash the whole call
    — graceful degrade, never a fabricated fill (same page/handler as above,
    kept as one Task-32 test group covering shape normalization end to end)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resultList": [
                    {"symbol": "BTC_USDT", "side": 1, "timestamp": 1},  # no price/vol
                    {
                        "symbol": "BTC_USDT",
                        "side": 1,
                        "vol": 1.0,
                        "price": 100.0,
                        # realistischer ms-Timestamp: _to_ms laesst ms unveraendert
                        # (idempotent), konvertiert nur Sekunden-Payloads hoch.
                        "timestamp": 1_700_000_000_000,
                    },
                ]
            },
        )

    c = _client_with_handler(handler)
    out = await c.user_fills(symbol="BTC_USDT")
    assert len(out) == 1
    assert out[0]["time"] == 1_700_000_000_000


# --- Defect D: _opt_float/_opt_int must guard "" like Hyperliquid's _opt_f,
# not just None — MEXC blanks optional numeric fields as "" (e.g. fairPrice,
# liquidatePrice, collectCycle), and float("")/int("") raise ValueError, which
# is not a MexcError so it escapes the ExchangeError handlers and crashes the
# whole poll cycle instead of degrading that one field to None. ---


def test_opt_float_empty_string_returns_none():
    assert _opt_float("") is None


def test_opt_float_garbage_string_returns_none():
    assert _opt_float("abc") is None


def test_opt_float_valid_numeric_string_still_parses():
    assert _opt_float("1.5") == 1.5


def test_opt_float_none_returns_none():
    assert _opt_float(None) is None


def test_opt_int_empty_string_returns_none():
    assert _opt_int("") is None


def test_opt_int_garbage_string_returns_none():
    assert _opt_int("abc") is None


def test_opt_int_valid_numeric_string_still_parses():
    assert _opt_int("42") == 42


def test_opt_int_none_returns_none():
    assert _opt_int(None) is None


@pytest.mark.asyncio
async def test_ticker_empty_string_optional_fields_degrade_to_none():
    """fairPrice:"" (real MEXC blank-field pattern) must not crash ticker() —
    it must map to fair_price=None instead of raising ValueError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "symbol": "BTC_USDT",
                    "lastPrice": 65000.0,
                    "bid1": "",
                    "ask1": "",
                    "fairPrice": "",
                    "indexPrice": "",
                    "volume24": "",
                    "amount24": "",
                    "fundingRate": "",
                    "timestamp": "",
                },
            },
        )

    c = _client_with_handler(handler)
    t = await c.ticker("BTC_USDT")
    assert t.last_price == 65000.0
    assert t.bid1 is None
    assert t.ask1 is None
    assert t.fair_price is None
    assert t.index_price is None
    assert t.volume24 is None
    assert t.amount24 is None
    assert t.funding_rate is None
    assert t.timestamp is None


@pytest.mark.asyncio
async def test_funding_rate_empty_string_optional_fields_degrade_to_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "symbol": "BTC_USDT",
                    "fundingRate": 0.0001,
                    "maxFundingRate": "",
                    "minFundingRate": "",
                    "collectCycle": "",
                    "nextSettleTime": "",
                    "timestamp": "",
                },
            },
        )

    c = _client_with_handler(handler)
    fr = await c.funding_rate("BTC_USDT")
    assert fr.funding_rate == 0.0001
    assert fr.max_funding_rate is None
    assert fr.min_funding_rate is None
    assert fr.collect_cycle is None
    assert fr.next_settle_time is None
    assert fr.timestamp is None


def test_map_position_empty_string_optional_fields_degrade_to_none():
    row = {
        "positionId": 1,
        "symbol": "BTC_USDT",
        "positionType": 1,
        "holdVol": 1.0,
        "holdAvgPrice": 65000.0,
        "leverage": 10,
        "openType": 1,
        "unRealizedPnl": 0.0,
        "realised": 0.0,
        "liquidatePrice": "",
        "im": "",
        "marginRatio": "",
        "state": 1,
    }
    out = map_position(row)
    assert out["liquidate_price"] is None
    assert out["im"] is None
    assert out["margin_ratio"] is None
