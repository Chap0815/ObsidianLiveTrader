"""Task 3 (O-03): MEXC price fields must serialize as fixed decimal strings,
never Python's scientific `e` notation, since the signed POST body is the
raw JSON text sent to the exchange (json.dumps of a small float like
0.00002 produces "2e-05", which MEXC's exchange-side parser can reject or
misinterpret for low-price coins such as SHIB/PEPE)."""

import re

import httpx
import pytest

from app.mexc.client import MexcClient, _fmt_price
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
