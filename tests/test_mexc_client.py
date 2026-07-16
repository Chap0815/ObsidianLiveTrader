"""Task 3 (O-03): MEXC price fields must serialize as fixed decimal strings,
never Python's scientific `e` notation, since the signed POST body is the
raw JSON text sent to the exchange (json.dumps of a small float like
0.00002 produces "2e-05", which MEXC's exchange-side parser can reject or
misinterpret for low-price coins such as SHIB/PEPE)."""

import re

import httpx
import pytest

from app.mexc.client import MexcClient, _fmt_price

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
