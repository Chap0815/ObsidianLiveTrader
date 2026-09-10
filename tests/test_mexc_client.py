"""Task 3 (O-03): MEXC price fields must serialize as fixed decimal strings,
never Python's scientific `e` notation, since the signed POST body is the
raw JSON text sent to the exchange (json.dumps of a small float like
0.00002 produces "2e-05", which MEXC's exchange-side parser can reject or
misinterpret for low-price coins such as SHIB/PEPE)."""

import asyncio
import re

import httpx
import pytest

from app.mexc.client import (
    MexcClient,
    _fmt_price,
    _opt_float,
    _opt_int,
    map_position,
    parse_contract_meta,
    sign_payload,
)
from app.mexc.errors import MexcError

# Scientific notation looks like "2e-05" / "1.2E+10" — a digit directly
# followed by e/E and an exponent. Field names like "leverage" also contain
# a bare "e", so the check must be exponent-shaped, not a blanket substring.
_SCI_NOTATION = re.compile(r"\d[eE][+-]?\d")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "api_secret"),
    [(" \t ", "synthetic-secret"), ("synthetic-key", " \t ")],
)
async def test_private_request_rejects_whitespace_credentials_before_transport(
    api_key, api_secret
):
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("private transport must not run without usable credentials")

    client = MexcClient("https://contract.mexc.com", api_key, api_secret)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MexcError, match="API keys not configured"):
            await client.assets()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_private_get_sends_exact_canonical_signed_query():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode("ascii")
        request_time = request.headers["Request-Time"]
        captured["query"] = query
        captured["signature"] = request.headers["Signature"]
        captured["expected_signature"] = sign_payload("k", "s", request_time, query)
        return httpx.Response(200, json={"success": True, "data": []})

    client = _client_with_handler(handler)
    try:
        await client._request(
            "GET",
            "/api/v1/private/account/assets",
            params={"z": "a b", "skip": None, "a": "1&2"},
            private=True,
        )
    finally:
        await client.aclose()

    assert captured["query"] == "a=1%262&z=a%20b"
    assert captured["signature"] == captured["expected_signature"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_time", [True, 1.5, "1.5", 0, -1, None, {}])
async def test_ping_rejects_invalid_server_time(invalid_time):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "data": invalid_time},
        )

    with pytest.raises(MexcError, match="server time"):
        await _client_with_handler(handler).ping()


def _valid_order_body(**overrides):
    body = {
        "symbol": "BTC_USDT",
        "vol": 1,
        "side": 1,
        "type": 5,
        "openType": 1,
        "leverage": 5,
    }
    body.update(overrides)
    return body


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
    with pytest.raises(MexcError, match="Non-finite price value"):
        _fmt_price(float("inf"))
    with pytest.raises(MexcError, match="Non-finite price value"):
        _fmt_price(float("nan"))


@pytest.mark.parametrize("value", [True, "not-a-price", [], {}])
def test_fmt_price_rejects_invalid_type_with_mexc_error(value):
    with pytest.raises(MexcError):
        _fmt_price(value)


def test_fmt_price_rejects_unquantizable_finite_value_with_mexc_error():
    with pytest.raises(MexcError):
        _fmt_price(1e308)


# --- FINDING 3 (LOW/latent, silent SL loss): a positive price/SL smaller than
# the quantization step ROUND_DOWNs to "0" on the wire while the service still
# believes body_had_sl=True → a "protected" report with NO real stop. The
# quantizer must HARD REJECT a positive value that collapses to 0, independent
# of any (unavailable-here) contract priceScale. ---


def test_fmt_price_rejects_subquantum_collapse():
    # 5e-9 < 1e-8 (default 8dp step) → ROUND_DOWN would yield "0"; must reject.
    with pytest.raises(MexcError):
        _fmt_price(5e-9)


def test_fmt_price_rejects_subquantum_collapse_with_explicit_scale():
    # The reject must key off the ACTUAL quantized result, not the fixed 8dp
    # fallback: a value fine at 8dp still collapses at a coarse contract scale.
    with pytest.raises(MexcError):
        _fmt_price(0.0009, scale=2)  # 2dp ROUND_DOWN → 0.00 → "0"


def test_fmt_price_zero_is_still_allowed():
    # A legitimate 0 (market-order price field) is NOT a collapse — never reject.
    assert _fmt_price(0) == "0"
    assert _fmt_price(0.0) == "0"


@pytest.mark.asyncio
async def test_place_order_hard_rejects_collapsing_sl():
    """FINDING 3 end-to-end: a sub-quantum stopLossPrice must raise BEFORE the
    body is signed/sent — never ship "0" as the SL while the caller thinks the
    position is protected."""
    capture: dict = {}
    c = _mock_client(capture)
    with pytest.raises(MexcError):
        await c.place_order(
            {
                "symbol": "SHIB_USDT",
                "vol": 1000,
                "side": 1,
                "type": 1,
                "openType": 1,
                "leverage": 10,
                "price": 0.00002,
                "stopLossPrice": 5e-9,
            }
        )
    # Nothing reached the transport — the reject fires while formatting the body.
    assert "content" not in capture


@pytest.mark.asyncio
async def test_place_order_hard_rejects_negative_stop_before_send():
    capture: dict = {}
    c = _mock_client(capture)

    with pytest.raises(MexcError, match="stopLossPrice"):
        await c.place_order(
            {
                "symbol": "BTC_USDT",
                "vol": 1,
                "side": 1,
                "type": 5,
                "openType": 1,
                "leverage": 5,
                "price": 0,
                "stopLossPrice": -1,
            }
        )

    assert "content" not in capture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in (
            "stopLossPrice",
            "takeProfitPrice",
            "takeProfitPrice2",
            "triggerPrice",
        )
        for value in (0, None)
    ],
)
async def test_place_order_rejects_empty_protective_price_before_send(field, value):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    client = _client_with_handler(handler)
    body = {
        "symbol": "BTC_USDT",
        "vol": 1,
        "side": 1,
        "type": 5,
        "openType": 1,
        "leverage": 5,
        "price": 0,
        field: value,
    }

    with pytest.raises(MexcError, match=field):
        await client.place_order(body)

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_symbol", [None, "", "   ", True, 77, [], {}])
async def test_mexc_place_order_rejects_invalid_symbol_before_send(invalid_symbol):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    client = _client_with_handler(handler)

    with pytest.raises(MexcError, match="symbol"):
        await client.place_order(
            {
                "symbol": invalid_symbol,
                "vol": 1,
                "side": 1,
                "type": 5,
                "openType": 1,
                "leverage": 5,
            }
        )

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_volume", [None, True, 0, -1, "1"])
async def test_mexc_place_order_rejects_invalid_volume_before_send(invalid_volume):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="order volume"):
        await client.place_order(
            {
                "symbol": "BTC_USDT",
                "vol": invalid_volume,
                "side": 1,
                "type": 5,
                "openType": 1,
                "leverage": 5,
            }
        )

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("side", None),
        ("side", True),
        ("side", 0),
        ("side", "1"),
        ("type", None),
        ("type", True),
        ("type", 0),
        ("type", "5"),
        ("openType", None),
        ("openType", True),
        ("openType", 3),
        ("openType", "1"),
        ("leverage", None),
        ("leverage", True),
        ("leverage", 0),
        ("leverage", 1.5),
        ("leverage", "5"),
    ],
)
async def test_mexc_place_order_rejects_invalid_routing_before_send(
    field, invalid_value
):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    body = {
        "symbol": "BTC_USDT",
        "vol": 1,
        "side": 1,
        "type": 5,
        "openType": 1,
        "leverage": 5,
    }
    body[field] = invalid_value

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match=field):
        await client.place_order(body)

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "price_fields",
    [{}, {"price": None}, {"price": 0}, {"price": "100"}],
)
async def test_mexc_place_order_rejects_invalid_limit_price_before_send(price_fields):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    body = _valid_order_body(type=1)
    body.update(price_fields)

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="limit order price"):
        await client.place_order(body)

    assert requests == 0


# --- FINDING 2 (MEDIUM, error mistaken for success): MEXC can answer HTTP 200
# with {"code": <nonzero>, "message": ...} and NO "success" field
# (gateway/maintenance/rate-limit variants). _request only checked
# `success is False`, so such an error was passed through as a successful
# result — fatal on place_order. ---


@pytest.mark.asyncio
async def test_mexc_request_rejects_nonzero_code_without_success_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 2011, "message": "system busy"})

    c = _client_with_handler(handler)
    with pytest.raises(MexcError) as ei:
        await c.place_order(
            {
                "symbol": "BTC_USDT",
                "vol": 1,
                "side": 1,
                "type": 5,
                "openType": 1,
                "leverage": 5,
            }
        )
    msg = str(ei.value).lower()
    assert "2011" in msg or "busy" in msg


@pytest.mark.asyncio
async def test_private_request_uses_documented_default_receive_window():
    captured_headers: httpx.Headers | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = request.headers
        return httpx.Response(
            200,
            json={"success": True, "code": 0, "data": []},
        )

    assert await _client_with_handler(handler).assets() == []
    assert captured_headers is not None
    assert "ApiKey" in captured_headers
    assert "Request-Time" in captured_headers
    assert "Signature" in captured_headers
    assert "Recv-Window" not in captured_headers


@pytest.mark.asyncio
async def test_mexc_request_allows_zero_code_and_missing_code():
    """Control: code=0 and responses with NO code field are legitimate success
    answers and must NOT be rejected by the new nonzero-code check."""

    def handler_zero(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"orderId": 7}})

    out = await _client_with_handler(handler_zero).place_order(
        {"symbol": "BTC_USDT", "vol": 1, "side": 1, "type": 5,
         "openType": 1, "leverage": 5}
    )
    assert out == {"orderId": 7}

    def handler_nocode(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"orderId": 8}})

    out2 = await _client_with_handler(handler_nocode).place_order(
        {"symbol": "BTC_USDT", "vol": 1, "side": 1, "type": 5,
         "openType": 1, "leverage": 5}
    )
    assert out2 == {"orderId": 8}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [],
        0,
        False,
        "",
        "accepted",
        {"message": "accepted"},
        {"externalOid": "client-only"},
    ],
)
async def test_mexc_place_order_rejects_ambiguous_success_payload(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="uncertain order-create response"):
        await client.place_order(_valid_order_body())


@pytest.mark.asyncio
async def test_mexc_place_order_accepts_positive_scalar_order_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": 7})

    result = await _client_with_handler(handler).place_order(_valid_order_body())
    assert result == {"orderId": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("102057569836905984", {"orderId": "102057569836905984"}),
        ({"orderId": "102057569836905984"}, {"orderId": "102057569836905984"}),
        ({"oid": 7}, {"oid": 7}),
        ({"orderId": 7, "errorCode": 0}, {"orderId": 7, "errorCode": 0}),
        ({"orderId": 7, "success": True}, {"orderId": 7, "success": True}),
        ({"orderId": 7, "oid": "7"}, {"orderId": 7, "oid": "7"}),
    ],
)
async def test_mexc_place_order_accepts_positive_documented_order_id(payload, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    result = await _client_with_handler(handler).place_order(_valid_order_body())
    assert result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_fields",
    [
        {"errorCode": 3001},
        {"errorCode": None},
        {"errorCode": True},
        {"errorCode": 0.0},
        {"code": 2011},
        {"code": 0.0},
        {"success": False},
        {"success": "true"},
    ],
)
async def test_mexc_place_order_rejects_nested_failure_with_order_id(failure_fields):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"orderId": 7, **failure_fields}
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="uncertain order-create response"):
        await _client_with_handler(handler).place_order(_valid_order_body())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"orderId": 7, "oid": 8},
        {"orderId": "7", "order_id": "8"},
        {"orderId": 7, "oid": True},
    ],
)
async def test_mexc_place_order_rejects_conflicting_order_id_aliases(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="uncertain order-create response"):
        await _client_with_handler(handler).place_order(_valid_order_body())


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["errorCode", "error_code"])
async def test_mexc_place_order_rejects_outer_failure_marker(marker):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                marker: 3001,
                "data": {"orderId": 7},
            },
        )

    with pytest.raises(MexcError, match="MEXC error"):
        await _client_with_handler(handler).place_order(_valid_order_body())


@pytest.mark.asyncio
async def test_mexc_place_order_rejects_oversized_digit_order_id_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "data": "9" * 5000}
        )

    with pytest.raises(MexcError, match="uncertain order-create response"):
        await _client_with_handler(handler).place_order(_valid_order_body())


@pytest.mark.asyncio
async def test_mexc_place_order_rejects_nonfinite_json_before_send():
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="JSON"):
        await client.place_order(_valid_order_body(metadata=float("nan")))

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "vol"),
    [
        ("sideways", 1.0),
        ("long", float("nan")),
        ("long", -1.0),
        ("long", 0.0),
    ],
)
async def test_mexc_close_rejects_invalid_side_or_volume_before_send(side, vol):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    client = _client_with_handler(handler)
    with pytest.raises(MexcError):
        await client.close_position_market("BTC_USDT", side=side, vol=vol)

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("open_type", [True, 0, 3, 1.5, "1"])
async def test_mexc_close_rejects_invalid_open_type_before_send(open_type):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    with pytest.raises(MexcError, match="openType"):
        await _client_with_handler(handler).close_position_market(
            "BTC_USDT", side="long", vol=1.0, open_type=open_type
        )

    assert requests == 0


@pytest.mark.asyncio
async def test_mexc_close_rejects_empty_symbol_before_send():
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"success": True, "data": {"orderId": 7}})

    with pytest.raises(MexcError, match="symbol"):
        await _client_with_handler(handler).close_position_market(
            "  ", side="long", vol=1.0
        )

    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "mexc_side"), [(" LONG ", 4), ("short", 2)])
async def test_mexc_close_maps_valid_side_after_validation(side, mexc_side):
    capture: dict = {}
    client = _mock_client(capture)

    await client.close_position_market(
        "BTC_USDT", side=side, vol=1.5, external_oid="close-test"
    )

    assert f'"side":{mexc_side}' in capture["content"]
    assert '"vol":1.5' in capture["content"]
    assert '"externalOid":"close:close-test"' in capture["content"]


@pytest.mark.asyncio
async def test_mexc_set_leverage_accepts_documented_public_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "code": 0})

    result = await _client_with_handler(handler).set_leverage(
        "BTC_USDT", 5, 1, position_type=1
    )
    assert result == {"success": True, "code": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"success": True, "code": 0},
        {"success": True, "errorCode": "0"},
    ],
)
async def test_mexc_set_leverage_accepts_unambiguous_nested_success(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    result = await _client_with_handler(handler).set_leverage(
        "BTC_USDT", 5, 1, position_type=1
    )
    assert result == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "code": 2011},
        {"success": True, "code": None},
        {"success": True, "code": True},
        {"success": True, "code": 0.0},
        {"success": True, "errorCode": 3001},
        {"success": True, "errorCode": None},
        {"success": True, "errorCode": False},
        {"success": True, "errorCode": 0.0},
    ],
)
async def test_mexc_set_leverage_rejects_nested_failure_marker(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="uncertain set-leverage response"):
        await _client_with_handler(handler).set_leverage(
            "BTC_USDT", 5, 1, position_type=1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, [], 0, True, "", {"code": 0}])
async def test_mexc_set_leverage_rejects_ambiguous_success_payload(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="uncertain set-leverage response"):
        await _client_with_handler(handler).set_leverage(
            "BTC_USDT", 5, 1, position_type=1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leverage", "open_type", "position_type", "position_id"),
    [
        (True, 1, 1, None),
        (1.5, 1, 1, None),
        (0, 1, 1, None),
        (-1, 1, 1, None),
        (5, True, 1, None),
        (5, 0, 1, None),
        (5, 3, 1, None),
        (5, 1, True, None),
        (5, 1, 0, None),
        (5, 1, 3, None),
        (5, 1, None, None),
        (5, 1, None, True),
        (5, 1, None, 0),
        (5, 1, None, -1),
        (5, 1, None, 1.5),
    ],
)
async def test_mexc_set_leverage_rejects_invalid_inputs_before_request(
    leverage, open_type, position_type, position_id
):
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json={"success": True})

    with pytest.raises(MexcError):
        await _client_with_handler(handler).set_leverage(
            "BTC_USDT",
            leverage,
            open_type,
            position_type=position_type,
            position_id=position_id,
        )

    assert sent is False


@pytest.mark.asyncio
async def test_mexc_set_leverage_rejects_empty_symbol_without_position_id():
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json={"success": True})

    with pytest.raises(MexcError, match="symbol"):
        await _client_with_handler(handler).set_leverage(
            "  ", 5, 1, position_type=1
        )

    assert sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, {}, [], True, [{"orderId": 7}], [{"orderId": 7, "errorCode": 0}, "bad"]],
)
async def test_mexc_cancel_rejects_ambiguous_or_malformed_success_payload(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="cancel response"):
        await client.cancel_order([7])


@pytest.mark.asyncio
async def test_mexc_cancel_rejects_response_for_different_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"orderId": 8, "errorCode": 0, "errorMsg": "success"}],
            },
        )

    with pytest.raises(MexcError, match="requested order"):
        await _client_with_handler(handler).cancel_order([7])


@pytest.mark.asyncio
async def test_mexc_cancel_rejects_conflicting_response_id_aliases():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "orderId": 7,
                        "oid": 8,
                        "errorCode": 0,
                        "errorMsg": "success",
                    }
                ],
            },
        )

    with pytest.raises(MexcError, match="requested order"):
        await _client_with_handler(handler).cancel_order([7])


@pytest.mark.asyncio
async def test_mexc_cancel_rejects_response_with_unrequested_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"orderId": 7, "errorCode": 0, "errorMsg": "success"},
                    {"orderId": 8, "errorCode": 0, "errorMsg": "success"},
                ],
            },
        )

    with pytest.raises(MexcError, match="requested order"):
        await _client_with_handler(handler).cancel_order([7])


@pytest.mark.asyncio
async def test_mexc_cancel_rejects_duplicate_order_ids_before_send():
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"orderId": 7, "errorCode": 0, "errorMsg": "success"}],
            },
        )

    with pytest.raises(MexcError, match="duplicate"):
        await _client_with_handler(handler).cancel_order([7, "7"])

    assert sent is False


@pytest.mark.asyncio
async def test_mexc_cancel_rejects_conflicting_id_aliases_before_send():
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"orderId": 7, "errorCode": 0, "errorMsg": "success"}],
            },
        )

    with pytest.raises(MexcError, match="exactly one"):
        await _client_with_handler(handler).cancel_order([{"orderId": 7, "oid": 8}])

    assert sent is False


@pytest.mark.asyncio
async def test_mexc_cancel_accepts_matching_result_row():
    payload = [
        {"orderId": 7, "oid": "7", "errorCode": 0, "errorMsg": "success"}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    assert await _client_with_handler(handler).cancel_order([7]) == payload


@pytest.mark.asyncio
async def test_mexc_cancel_raises_on_matching_error_result_row():
    payload = [{"orderId": 7, "errorCode": 3001, "errorMsg": "busy"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="cancel rejected"):
        await _client_with_handler(handler).cancel_order([7])


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [False, 0.0])
async def test_mexc_cancel_rejects_invalid_error_code_type(error_code):
    payload = [{"orderId": 7, "errorCode": error_code, "errorMsg": "unknown"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="cancel rejected"):
        await _client_with_handler(handler).cancel_order([7])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_fields",
    [
        {"success": False},
        {"success": "true"},
        {"code": 3001},
        {"error_code": None},
    ],
)
async def test_mexc_cancel_rejects_contradictory_nested_marker(failure_fields):
    payload = [
        {"orderId": 7, "errorCode": 0, "errorMsg": "success", **failure_fields}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="cancel rejected"):
        await _client_with_handler(handler).cancel_order([7])


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", [True, 0, -1, 1.5, "abc"])
async def test_mexc_cancel_rejects_invalid_request_before_send(order_id):
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"orderId": order_id, "errorCode": 0}],
            },
        )

    with pytest.raises(MexcError):
        await _client_with_handler(handler).cancel_order([order_id])

    assert sent is False


@pytest.mark.asyncio
async def test_mexc_cancel_rejects_empty_request_before_send():
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json={"success": True, "data": []})

    with pytest.raises(MexcError):
        await _client_with_handler(handler).cancel_order([])

    assert sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", [2011, 500, 200, "200", True, False, 0.0, "", None]
)
async def test_mexc_request_rejects_invalid_code_even_with_success_true(code):
    """The official common response requires code=0 for success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "code": code, "data": {"orderId": 9}}
        )

    with pytest.raises(MexcError, match="MEXC error"):
        await _client_with_handler(handler).place_order(
            {
                "symbol": "BTC_USDT",
                "vol": 1,
                "side": 1,
                "type": 5,
                "openType": 1,
                "leverage": 5,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [0, 1, None, "true", "false"])
async def test_mexc_place_order_rejects_nonboolean_success_marker(success):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": success, "data": {"orderId": 9}}
        )

    with pytest.raises(MexcError, match="success marker"):
        await _client_with_handler(handler).place_order(
            {
                "symbol": "BTC_USDT",
                "vol": 1,
                "side": 1,
                "type": 5,
                "openType": 1,
                "leverage": 5,
            }
        )


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
                        {
                            "externalOid": target_oid,
                            "orderId": 9,
                            "state": 4,
                            "dealVol": 0,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    c._RECOVERY_RETRY_DELAY_S = 0.0
    result = await c.order_by_external_oid("BTC_USDT", target_oid)

    assert result == {}, "cancelled order must never be reported recovered/live"


@pytest.mark.asyncio
@pytest.mark.parametrize("deal_vol", ["0.4", 1, "NaN", None])
async def test_mexc_recovery_keeps_cancelled_order_when_zero_fill_not_proven(
    deal_vol,
):
    target_oid = "cli-partial-cancel"
    row = {
        "externalOid": target_oid,
        "orderId": 9,
        "symbol": "BTC_USDT",
        "state": 4,
    }
    if deal_vol is not None:
        row["dealVol"] = deal_vol

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(target_oid):
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={"resultList": []})

    result = await _client_with_handler(handler).order_by_external_oid(
        "BTC_USDT", target_oid
    )
    assert result["match"] == "direct"


@pytest.mark.asyncio
async def test_mexc_recovery_keeps_order_when_terminal_state_aliases_conflict():
    target_oid = "cli-conflicting-state"
    row = {
        "externalOid": target_oid,
        "orderId": 9,
        "symbol": "BTC_USDT",
        "state": 4,
        "orderState": 3,
        "dealVol": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(target_oid):
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={"resultList": []})

    result = await _client_with_handler(handler).order_by_external_oid(
        "BTC_USDT", target_oid
    )
    assert result["match"] == "direct"


@pytest.mark.asyncio
async def test_mexc_recovery_keeps_order_when_zero_fill_aliases_conflict():
    target_oid = "cli-conflicting-fill"
    row = {
        "externalOid": target_oid,
        "orderId": 9,
        "symbol": "BTC_USDT",
        "state": 4,
        "dealVol": 0,
        "deal_vol": "0.4",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(target_oid):
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={"resultList": []})

    result = await _client_with_handler(handler).order_by_external_oid(
        "BTC_USDT", target_oid
    )
    assert result["match"] == "direct"


@pytest.mark.asyncio
async def test_mexc_recovery_keeps_order_for_any_positive_reported_fill():
    target_oid = "cli-tiny-positive-fill"
    row = {
        "externalOid": target_oid,
        "orderId": 9,
        "symbol": "BTC_USDT",
        "state": 4,
        "dealVol": "0.0000000000005",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(target_oid):
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={"resultList": []})

    result = await _client_with_handler(handler).order_by_external_oid(
        "BTC_USDT", target_oid
    )
    assert result["match"] == "direct"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {
            "externalOid": "cli-identity",
            "orderId": 7,
            "symbol": "ETH_USDT",
            "state": 2,
        },
        {
            "externalOid": "cli-identity",
            "symbol": "BTC_USDT",
            "state": 2,
        },
        {
            "externalOid": "cli-identity",
            "orderId": 7,
            "oid": 8,
            "symbol": "BTC_USDT",
            "state": 2,
        },
        {
            "externalOid": "cli-identity",
            "orderId": 7,
            "oid": True,
            "symbol": "BTC_USDT",
            "state": 2,
        },
        {
            "externalOid": "cli-identity",
            "external_oid": "cli-other",
            "orderId": 7,
            "symbol": "BTC_USDT",
            "state": 2,
        },
        {
            "externalOid": "",
            "external_oid": "cli-identity",
            "orderId": 7,
            "symbol": "BTC_USDT",
            "state": 2,
        },
    ],
)
async def test_mexc_recovery_rejects_wrong_symbol_or_missing_order_id(row):
    target_oid = "cli-identity"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(target_oid):
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={"resultList": []})

    client = _client_with_handler(handler)
    client._RECOVERY_ATTEMPTS = 1
    assert await client.order_by_external_oid("BTC_USDT", target_oid) == {}


@pytest.mark.asyncio
async def test_mexc_recovery_rejects_oid_only_in_diagnostic_text():
    target_oid = "cli-not-found-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(200, json={"message": f"{target_oid} not found"})
        if path in {
            "/api/v1/private/order/list/history_orders",
            "/api/v1/private/order/list/open_orders",
        }:
            return httpx.Response(200, json={"resultList": []})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    c._RECOVERY_ATTEMPTS = 1

    assert await c.order_by_external_oid("BTC_USDT", target_oid) == {}


@pytest.mark.asyncio
async def test_mexc_recovery_accepts_exact_direct_oid_field():
    target_oid = "cli-direct-1"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"externalOid": target_oid, "orderId": 7, "state": 2},
        )

    c = _client_with_handler(handler)

    result = await c.order_by_external_oid("BTC_USDT", target_oid)
    assert result["match"] == "direct"
    assert result["externalOid"] == target_oid
    assert result["order"]["orderId"] == 7


@pytest.mark.asyncio
async def test_mexc_recovery_malformed_history_shape_falls_back_to_open_orders():
    target_oid = "cli-open-fallback-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if path == "/api/v1/private/order/list/history_orders":
            return httpx.Response(200, json={"resultList": 7})
        if path == "/api/v1/private/order/list/open_orders":
            return httpx.Response(
                200,
                json={"resultList": [{"externalOid": target_oid, "orderId": 8}]},
            )
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)

    result = await c.order_by_external_oid("BTC_USDT", target_oid)
    assert result["match"] == "open"
    assert result["order"]["orderId"] == 8


@pytest.mark.asyncio
async def test_mexc_recovery_unknown_history_dict_cannot_fabricate_match():
    target_oid = "cli-not-history-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/private/order/external/" + target_oid:
            return httpx.Response(404, json={"code": 404, "msg": "not found"})
        if path == "/api/v1/private/order/list/history_orders":
            return httpx.Response(
                200,
                json={"externalOid": target_oid, "message": "not a history list"},
            )
        if path == "/api/v1/private/order/list/open_orders":
            return httpx.Response(200, json={"resultList": []})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)
    c._RECOVERY_ATTEMPTS = 1

    assert await c.order_by_external_oid("BTC_USDT", target_oid) == {}


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_uses_current_official_endpoint():
    calls: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "data": [{"id": 7, "symbol": "BTC_USDT", "state": 1}],
            },
        )

    rows = await _client_with_handler(handler).open_stop_orders("BTC_USDT")

    assert rows == [{"id": 7, "symbol": "BTC_USDT", "state": 1}]
    assert calls == [
        ("/api/v1/private/stoporder/open_orders", {"symbol": "BTC_USDT"})
    ]


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_filters_terminal_states():
    rows = [
        {"id": state, "symbol": "BTC_USDT", "state": state}
        for state in (1, 2, 3, 4, 5)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "code": 0, "data": rows},
        )

    assert await _client_with_handler(handler).open_stop_orders("BTC_USDT") == [
        rows[0]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "", True, False, 0, 6, 1.5, "bad"])
async def test_mexc_open_stop_orders_rejects_invalid_state(state):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "data": [{"id": 7, "symbol": "BTC_USDT", "state": state}],
            },
        )

    with pytest.raises(MexcError, match="stop-order state"):
        await _client_with_handler(handler).open_stop_orders("BTC_USDT")


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_retries_current_endpoint_after_fallback():
    """A transient current-endpoint failure must not permanently promote the
    less-authoritative history fallback ahead of it on later protection reads."""
    current_path, fallback_path = MexcClient._STOP_ORDER_PATHS
    calls: list[str] = []
    current_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_attempts
        path = request.url.path
        calls.append(path)
        if path == current_path:
            current_attempts += 1
            if current_attempts == 1:
                return httpx.Response(404, json={"code": 404, "msg": "not found"})
            return httpx.Response(200, json=[{"orderId": 8, "state": 1}])
        assert path == fallback_path
        return httpx.Response(200, json={"resultList": [{"orderId": 7, "state": 1}]})

    c = _client_with_handler(handler)

    assert await c.open_stop_orders("BTC_USDT") == [{"orderId": 7, "state": 1}]
    assert await c.open_stop_orders("BTC_USDT") == [{"orderId": 8, "state": 1}]
    assert calls == [current_path, fallback_path, current_path]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": []},
        {"resultList": "not-a-list"},
        [{"orderId": 1}, "not-an-order"],
        None,
    ],
)
async def test_mexc_open_orders_rejects_unrecognized_or_malformed_rows(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    c = _client_with_handler(handler)

    with pytest.raises(MexcError, match="open-orders response"):
        await c.open_orders("BTC_USDT")


@pytest.mark.asyncio
async def test_mexc_open_orders_accepts_recognized_empty_result_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": []})

    c = _client_with_handler(handler)

    assert await c.open_orders("BTC_USDT") == []


@pytest.mark.asyncio
async def test_mexc_open_orders_filters_explicitly_foreign_symbols():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTC_USDT"
        return httpx.Response(
            200,
            json={
                "resultList": [
                    {"orderId": 1, "symbol": "BTC_USDT"},
                    {"orderId": 2, "symbol": "ETH_USDT"},
                    {"orderId": 3},
                ]
            },
        )

    rows = await _client_with_handler(handler).open_orders("BTC_USDT")

    assert rows == [
        {"orderId": 1, "symbol": "BTC_USDT"},
        {"orderId": 3},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_symbol", [True, 123])
async def test_mexc_open_orders_rejects_invalid_symbol_identity(invalid_symbol):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"resultList": [{"orderId": 1, "symbol": invalid_symbol}]},
        )

    with pytest.raises(MexcError, match="open-orders.*symbol"):
        await _client_with_handler(handler).open_orders("BTC_USDT")


@pytest.mark.asyncio
async def test_mexc_open_orders_fetches_second_page():
    first_page = [{"orderId": order_id} for order_id in range(1, 101)]
    second_page = [{"orderId": 101}]
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_num = int(request.url.params["page_num"])
        requested_pages.append(page_num)
        rows = first_page if page_num == 1 else second_page
        return httpx.Response(200, json={"resultList": rows})

    c = _client_with_handler(handler)

    rows = await c.open_orders("BTC_USDT")

    assert requested_pages == [1, 2]
    assert rows == [*first_page, *second_page]


@pytest.mark.asyncio
async def test_mexc_open_orders_fails_closed_at_page_cap():
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(int(request.url.params["page_num"]))
        return httpx.Response(
            200,
            json={"resultList": [{"orderId": order_id} for order_id in range(100)]},
        )

    c = _client_with_handler(handler)

    with pytest.raises(MexcError, match="page cap"):
        await c.open_orders("BTC_USDT")

    assert requested_pages == list(range(1, c._OPEN_ORDERS_MAX_PAGES + 1))


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_skips_malformed_candidate_rows():
    bad_path, good_path = MexcClient._STOP_ORDER_PATHS[:2]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == bad_path:
            return httpx.Response(200, json={"resultList": ["not-an-order"]})
        if path == good_path:
            return httpx.Response(
                200,
                json={"resultList": [{"orderId": 7, "state": 1}]},
            )
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    c = _client_with_handler(handler)

    assert await c.open_stop_orders("BTC_USDT") == [{"orderId": 7, "state": 1}]
    assert calls == [bad_path, good_path]


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_filters_explicitly_foreign_symbols():
    path = MexcClient._STOP_ORDER_PATHS[0]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        assert request.url.params["symbol"] == "BTC_USDT"
        return httpx.Response(
            200,
            json={
                "resultList": [
                    {"orderId": 1, "symbol": "BTC_USDT", "state": 1},
                    {"orderId": 2, "symbol": "ETH_USDT", "state": 1},
                    {"orderId": 3, "state": 1},
                ]
            },
        )

    rows = await _client_with_handler(handler).open_stop_orders("BTC_USDT")

    assert rows == [
        {"orderId": 1, "symbol": "BTC_USDT", "state": 1},
        {"orderId": 3, "state": 1},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_symbol", [True, 123])
async def test_mexc_open_stop_orders_rejects_invalid_symbol_identity(invalid_symbol):
    path = MexcClient._STOP_ORDER_PATHS[0]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        return httpx.Response(
            200,
            json={
                "resultList": [
                    {"orderId": 1, "symbol": invalid_symbol, "state": 1}
                ]
            },
        )

    with pytest.raises(MexcError, match="stop-order.*symbol"):
        await _client_with_handler(handler).open_stop_orders("BTC_USDT")


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_fetches_second_page():
    current_path, path = MexcClient._STOP_ORDER_PATHS
    first_page = [{"id": order_id, "state": 1} for order_id in range(1, 101)]
    second_page = [{"id": 101, "state": 1}]
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == current_path:
            return httpx.Response(404, json={"code": 404, "message": "missing"})
        assert request.url.path == path
        assert request.url.params["is_finished"] == "0"
        page_num = int(request.url.params["page_num"])
        requested_pages.append(page_num)
        rows = first_page if page_num == 1 else second_page
        return httpx.Response(200, json={"resultList": rows})

    c = _client_with_handler(handler)

    rows = await c.open_stop_orders("BTC_USDT")

    assert requested_pages == [1, 2]
    assert rows == [*first_page, *second_page]


@pytest.mark.asyncio
async def test_mexc_open_stop_orders_does_not_fallback_after_partial_page_read():
    current_path, selected_path = MexcClient._STOP_ORDER_PATHS
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == current_path:
            return httpx.Response(404, json={"code": 404, "message": "missing"})
        assert request.url.path == selected_path
        assert request.url.params["is_finished"] == "0"
        if request.url.params["page_num"] == "1":
            return httpx.Response(
                200,
                json={"resultList": [{"id": order_id} for order_id in range(100)]},
            )
        return httpx.Response(503, json={"message": "temporary read failure"})

    c = _client_with_handler(handler)

    with pytest.raises(MexcError):
        await c.open_stop_orders("BTC_USDT")

    assert requested_paths == [current_path, selected_path, selected_path]


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
async def test_mexc_user_fills_filters_explicitly_foreign_symbols():
    raw_rows = [
        {
            "symbol": "BTC_USDT",
            "side": 1,
            "vol": 1.0,
            "price": 100.0,
            "timestamp": 1_700_000_000_000,
        },
        {
            "symbol": "ETH_USDT",
            "side": 1,
            "vol": 2.0,
            "price": 200.0,
            "timestamp": 1_700_000_001_000,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTC_USDT"
        return httpx.Response(200, json={"resultList": raw_rows})

    out = await _client_with_handler(handler).user_fills(symbol="BTC_USDT")

    assert [row["symbol"] for row in out] == ["BTC_USDT"]


@pytest.mark.asyncio
async def test_mexc_user_fills_pages_until_requested_matching_rows():
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page_num"])
        requested_pages.append(page)
        symbol = "ETH_USDT" if page == 1 else "BTC_USDT"
        return httpx.Response(
            200,
            json={
                "resultList": [
                    {
                        "symbol": symbol,
                        "side": 1,
                        "vol": 1.0,
                        "price": 100.0,
                        "timestamp": 1_700_000_000_000 + page,
                    }
                ]
            },
        )

    out = await _client_with_handler(handler).user_fills(
        symbol="BTC_USDT", limit=1
    )

    assert requested_pages == [1, 2]
    assert [row["symbol"] for row in out] == ["BTC_USDT"]


@pytest.mark.asyncio
async def test_mexc_user_fills_fails_closed_at_page_cap_after_filtered_rows():
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page_num"])
        requested_pages.append(page)
        return httpx.Response(
            200,
            json={
                "resultList": [
                    {
                        "symbol": "ETH_USDT",
                        "side": 1,
                        "vol": 1.0,
                        "price": 100.0,
                        "timestamp": 1_700_000_000_000 + page,
                    }
                ]
            },
        )

    client = _client_with_handler(handler)

    with pytest.raises(MexcError, match="user-fills pagination exceeded safe page cap"):
        await client.user_fills(symbol="BTC_USDT", limit=1)

    assert requested_pages == list(range(1, client._FILLS_MAX_PAGES + 1))


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"unexpected": []}, {"resultList": "not-a-list"}, None, "not-a-list"],
)
async def test_mexc_user_fills_rejects_unrecognized_page_shape(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    c = _client_with_handler(handler)

    with pytest.raises(MexcError, match="user-fills response"):
        await c.user_fills(symbol="BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", ""),
        ("symbol", True),
        ("symbol", 123),
        ("side", 5),
        ("price", "NaN"),
        ("price", 0),
        ("vol", "Infinity"),
        ("vol", 0),
        ("timestamp", 1),
        ("timestamp", 0),
        ("side", True),
        ("side", 1.5),
        ("price", True),
        ("price", 10**400),
        ("vol", True),
        ("timestamp", 1.5),
        ("timestamp", True),
    ],
)
async def test_mexc_user_fills_skips_invalid_required_values(field, value):
    row = {
        "symbol": "BTC_USDT",
        "side": 1,
        "vol": 1.0,
        "price": 100.0,
        "timestamp": 1_700_000_000_000,
    }
    row[field] = value

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": [row]})

    c = _client_with_handler(handler)

    assert await c.user_fills() == []


@pytest.mark.asyncio
async def test_mexc_user_fills_skips_implausibly_future_timestamp(monkeypatch):
    now_s = 1_700_000_000.0
    monkeypatch.setattr("app.mexc.client.time.time", lambda: now_s)
    rows = [
        {
            "symbol": "BTC_USDT",
            "side": 1,
            "vol": 1.0,
            "price": 100.0,
            "timestamp": int(now_s * 1000) + offset,
        }
        for offset in (300_001, 300_000)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": rows})

    normalized = await _client_with_handler(handler).user_fills("BTC_USDT")

    assert [row["time"] for row in normalized] == [int(now_s * 1000) + 300_000]


@pytest.mark.asyncio
async def test_mexc_user_fills_degrades_nonfinite_optional_values_to_none():
    row = {
        "symbol": "BTC_USDT",
        "side": 2,
        "vol": 1.0,
        "price": 100.0,
        "timestamp": 1_700_000_000_000,
        "profit": "NaN",
        "fee": "Infinity",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": [row]})

    c = _client_with_handler(handler)
    out = await c.user_fills(symbol="BTC_USDT")

    assert out[0]["side"] == "buy"
    assert out[0]["dir"] == "Close Short"
    assert out[0]["closed_pnl"] is None
    assert out[0]["fee"] is None


@pytest.mark.asyncio
async def test_mexc_user_fills_degrades_boolean_optional_values_to_none():
    row = {
        "symbol": "BTC_USDT",
        "side": 2,
        "vol": 1.0,
        "price": 100.0,
        "timestamp": 1_700_000_000_000,
        "profit": True,
        "fee": True,
        "orderId": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": [row]})

    out = await _client_with_handler(handler).user_fills(symbol="BTC_USDT")

    assert out[0]["closed_pnl"] is None
    assert out[0]["fee"] is None
    assert out[0]["oid"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_oid",
    [0, -1, 1.5, "1.5", {}, pytest.param("9" * 5_000, id="oversized-digits")],
)
async def test_mexc_user_fills_degrades_invalid_oid_values_to_none(bad_oid):
    row = {
        "symbol": "BTC_USDT",
        "side": 2,
        "vol": 1.0,
        "price": 100.0,
        "timestamp": 1_700_000_000_000,
        "orderId": bad_oid,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultList": [row]})

    out = await _client_with_handler(handler).user_fills(symbol="BTC_USDT")

    assert out[0]["oid"] is None


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


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_opt_float_nonfinite_returns_none(value):
    assert _opt_float(value) is None


@pytest.mark.parametrize("value", [True, False])
def test_opt_float_boolean_returns_none(value):
    assert _opt_float(value) is None


def test_opt_int_empty_string_returns_none():
    assert _opt_int("") is None


def test_opt_int_garbage_string_returns_none():
    assert _opt_int("abc") is None


def test_opt_int_valid_numeric_string_still_parses():
    assert _opt_int("42") == 42


def test_opt_int_none_returns_none():
    assert _opt_int(None) is None


@pytest.mark.parametrize("value", [True, False])
def test_opt_int_boolean_returns_none(value):
    assert _opt_int(value) is None


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_opt_int_infinite_float_returns_none(value):
    assert _opt_int(value) is None


@pytest.mark.parametrize("value", [1.5, -1.5])
def test_opt_int_fractional_float_returns_none(value):
    assert _opt_int(value) is None


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
@pytest.mark.parametrize("last_price", ["NaN", "Infinity", "garbage", 0, -1])
async def test_ticker_rejects_invalid_required_last_price(last_price):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"symbol": "BTC_USDT", "lastPrice": last_price},
            },
        )

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="ticker lastPrice"):
        await client.ticker("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_symbol", [False, 0])
async def test_ticker_rejects_falsy_nonstring_symbol_identity(invalid_symbol):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"symbol": invalid_symbol, "lastPrice": 100.0},
            },
        )

    with pytest.raises(MexcError, match="Ticker symbol"):
        await _client_with_handler(handler).ticker("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("high", 9), ("low", 11)])
async def test_klines_reject_impossible_ohlc_geometry(field, value):
    payload = {
        "time": [1_700_000_000],
        "open": [10],
        "high": [11],
        "low": [9],
        "close": [10],
        "vol": [1],
        "amount": [10],
    }
    payload[field] = [value]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="kline.*geometry"):
        await _client_with_handler(handler).klines("BTC_USDT", "15m")


@pytest.mark.asyncio
async def test_klines_reject_fractional_timestamp():
    payload = {
        "time": [1_700_000_000.5],
        "open": [10],
        "high": [10],
        "low": [10],
        "close": [10],
        "vol": [1],
        "amount": [10],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="kline time"):
        await _client_with_handler(handler).klines("BTC_USDT", "15m")


@pytest.mark.asyncio
async def test_klines_reject_far_future_timestamp(monkeypatch):
    monkeypatch.setattr("app.mexc.client.time.time", lambda: 1_700_000_000.0)
    payload = {
        "time": [1_700_000_301],
        "open": [10],
        "high": [10],
        "low": [10],
        "close": [10],
        "vol": [1],
        "amount": [10],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="kline time"):
        await _client_with_handler(handler).klines("BTC_USDT", "15m")


@pytest.mark.asyncio
async def test_klines_reject_implausibly_old_timestamp():
    payload = {
        "time": [999_999_999],
        "open": [10],
        "high": [10],
        "low": [10],
        "close": [10],
        "vol": [1],
        "amount": [10],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="kline time"):
        await _client_with_handler(handler).klines("BTC_USDT", "15m")


@pytest.mark.asyncio
async def test_klines_normalize_provider_rows_to_chronological_order():
    payload = {
        "time": [1_700_000_900, 1_700_000_000],
        "open": [12, 11],
        "high": [12, 11],
        "low": [12, 11],
        "close": [12, 11],
        "vol": [1, 1],
        "amount": [12, 11],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    candles = await _client_with_handler(handler).klines("BTC_USDT", "15m")

    assert [c.time for c in candles] == [1_700_000_000_000, 1_700_000_900_000]
    assert candles[-1].close == 12


@pytest.mark.asyncio
async def test_klines_reject_duplicate_timestamps():
    payload = {
        "time": [1_700_000_001, 1_700_000_001],
        "open": [11, 12],
        "high": [11, 12],
        "low": [11, 12],
        "close": [11, 12],
        "vol": [1, 1],
        "amount": [11, 12],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="duplicate kline timestamp"):
        await _client_with_handler(handler).klines("BTC_USDT", "15m")


@pytest.mark.asyncio
async def test_klines_reject_duplicate_interval_bucket():
    payload = {
        "time": [1_700_000_001, 1_700_000_002],
        "open": [11, 12],
        "high": [11, 12],
        "low": [11, 12],
        "close": [11, 12],
        "vol": [1, 1],
        "amount": [11, 12],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="duplicate kline interval"):
        await _client_with_handler(handler).klines("BTC_USDT", "15m")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [7, "not-an-object", [{}, "bad"]])
async def test_contract_detail_rejects_unrecognized_or_mixed_shapes(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="contract-detail response"):
        await _client_with_handler(handler).contract_detail("BTC_USDT")


@pytest.mark.asyncio
async def test_contract_meta_selects_exact_requested_symbol():
    def row(symbol: str, contract_size: float) -> dict:
        return {
            "symbol": symbol,
            "contractSize": contract_size,
            "priceUnit": 0.1,
            "volUnit": 1,
            "minVol": 1,
            "maxVol": 1000,
            "maxLeverage": 50,
            "minLeverage": 1,
            "apiAllowed": True,
            "state": 0,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "data": [row("ETH_USDT", 0.01), row("BTC_USDT", 0.001)]},
        )

    meta = await _client_with_handler(handler).contract_meta("BTC_USDT")
    assert meta.symbol == "BTC_USDT"
    assert meta.contract_size == 0.001


@pytest.mark.asyncio
async def test_contract_meta_rejects_single_other_symbol():
    payload = {
        "symbol": "ETH_USDT",
        "contractSize": 0.01,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="Contract symbol mismatch"):
        await _client_with_handler(handler).contract_meta("BTC_USDT")


@pytest.mark.parametrize(
    "field",
    [
        "contractSize",
        "priceUnit",
        "volUnit",
        "minVol",
        "maxVol",
        "maxLeverage",
        "minLeverage",
    ],
)
def test_contract_meta_rejects_boolean_numeric_limits(field):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "apiAllowed": True,
        "state": 0,
    }
    row[field] = True

    with pytest.raises(MexcError, match=field):
        parse_contract_meta(row)


def test_numeric_overflow_keeps_mexc_adapter_semantics():
    huge = 10**400
    row = {
        "symbol": "BTC_USDT",
        "contractSize": huge,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "apiAllowed": True,
        "state": 0,
    }

    with pytest.raises(MexcError, match="contractSize"):
        parse_contract_meta(row)
    assert _opt_float(huge) is None


@pytest.mark.parametrize("field", ["maxLeverage", "minLeverage"])
def test_contract_meta_rejects_fractional_integer_limits(field):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "apiAllowed": True,
        "state": 0,
    }
    row[field] = 1.5

    with pytest.raises(MexcError, match=field):
        parse_contract_meta(row)


def test_contract_meta_wraps_oversized_integer_string():
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": "9" * 5000,
        "minLeverage": 1,
        "apiAllowed": True,
        "state": 0,
    }

    with pytest.raises(MexcError, match="maxLeverage"):
        parse_contract_meta(row)


@pytest.mark.parametrize("field", ["maxLeverage", "minLeverage"])
def test_contract_meta_rejects_missing_leverage_bound(field):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "apiAllowed": True,
        "state": 0,
    }
    row.pop(field)

    with pytest.raises(MexcError, match=field):
        parse_contract_meta(row)


def test_contract_meta_applies_country_max_leverage():
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "countryConfigContractMaxLeverage": 20,
        "apiAllowed": True,
        "state": 0,
    }

    assert parse_contract_meta(row).max_leverage == 20


@pytest.mark.parametrize("value", [None, "", True, -1, 1.5, "garbage"])
def test_contract_meta_rejects_invalid_country_max_leverage(value):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "countryConfigContractMaxLeverage": value,
        "apiAllowed": True,
        "state": 0,
    }

    with pytest.raises(MexcError, match="countryConfigContractMaxLeverage"):
        parse_contract_meta(row)


def test_contract_meta_zero_country_cap_keeps_global_max():
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "countryConfigContractMaxLeverage": 0,
        "apiAllowed": True,
        "state": 0,
    }

    assert parse_contract_meta(row).max_leverage == 50


@pytest.mark.parametrize("value", [None, "", True, False, 1.5, "garbage"])
def test_contract_meta_rejects_invalid_state(value):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "apiAllowed": True,
        "state": value,
    }

    with pytest.raises(MexcError, match="state"):
        parse_contract_meta(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [7, "not-an-object", [{}, "bad"]])
async def test_ticker_rejects_unrecognized_or_mixed_shapes(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="ticker response"):
        await _client_with_handler(handler).ticker("BTC_USDT")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_rate", ["NaN", None, ""])
async def test_funding_rate_rejects_invalid_required_value(invalid_rate):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"symbol": "BTC_USDT", "fundingRate": invalid_rate},
            },
        )

    client = _client_with_handler(handler)
    with pytest.raises(MexcError, match="fundingRate"):
        await client.funding_rate("BTC_USDT")


@pytest.mark.asyncio
async def test_market_overview_sanitizes_invalid_ranking_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "symbol": "GOOD_USDT",
                        "amount24": "1000",
                        "fundingRate": "0.001",
                        "lastPrice": "10",
                        "riseFallRate": "0.05",
                        "holdVol": "50",
                    },
                    {
                        "symbol": "BAD_USDT",
                        "amount24": "NaN",
                        "fundingRate": "Infinity",
                        "lastPrice": "-1",
                        "riseFallRate": "NaN",
                        "holdVol": "-2",
                    },
                ],
            },
        )

    rows = await _client_with_handler(handler).market_overview()

    assert rows[0]["symbol"] == "GOOD_USDT"
    bad = rows[1]
    assert bad["volume24"] == 0.0
    assert bad["funding"] is None
    assert bad["last"] is None
    assert bad["price_change_pct"] is None
    assert bad["open_interest"] is None


@pytest.mark.asyncio
async def test_market_overview_degrades_overflowed_price_change_to_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "symbol": "BTC_USDT",
                        "amount24": "1000",
                        "lastPrice": "10",
                        "riseFallRate": "1e308",
                    }
                ],
            },
        )

    rows = await _client_with_handler(handler).market_overview()

    assert rows[0]["price_change_pct"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [7, "not-an-object", [{}, "bad"]])
async def test_market_overview_rejects_unrecognized_or_mixed_shapes(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="market-overview response"):
        await _client_with_handler(handler).market_overview()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, 7, "not-an-object", []])
async def test_funding_rate_rejects_non_object_shapes(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="funding-rate response"):
        await _client_with_handler(handler).funding_rate("BTC_USDT")


@pytest.mark.asyncio
async def test_funding_rate_rejects_explicit_other_symbol():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"symbol": "ETH_USDT", "fundingRate": 0.0001},
            },
        )

    with pytest.raises(MexcError, match="Funding symbol mismatch"):
        await _client_with_handler(handler).funding_rate("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_symbol", [False, 0])
async def test_funding_rate_rejects_falsy_nonstring_symbol_identity(invalid_symbol):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"symbol": invalid_symbol, "fundingRate": 0.0001},
            },
        )

    with pytest.raises(MexcError, match="Funding symbol"):
        await _client_with_handler(handler).funding_rate("BTC_USDT")


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


def test_map_position_nonfinite_financial_fields_are_unknown():
    row = {
        "symbol": "BTC_USDT",
        "positionType": 1,
        "holdVol": "NaN",
        "holdAvgPrice": "Infinity",
        "unRealizedPnl": "NaN",
        "realised": "Infinity",
    }

    out = map_position(row, contract_size=float("nan"))

    assert out["hold_vol"] is None
    assert out["entry_price"] is None
    assert out["unrealized_pnl"] is None
    assert out["realised"] is None
    assert out["contract_size"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["holdVol", "holdAvgPrice"])
async def test_positions_reject_nonfinite_required_financial_fields(field):
    def handler(request: httpx.Request) -> httpx.Response:
        row = {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "openType": 1,
            "holdVol": 1,
            "holdAvgPrice": 100,
        }
        row[field] = "NaN"
        return httpx.Response(200, json={"success": True, "data": [row]})

    with pytest.raises(MexcError, match="open position"):
        await _client_with_handler(handler).positions("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["positionType", "openType"])
async def test_positions_reject_boolean_identity_fields(field):
    def handler(request: httpx.Request) -> httpx.Response:
        row = {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "openType": 1,
            "holdVol": 1,
            "holdAvgPrice": 100,
        }
        row[field] = True
        return httpx.Response(200, json={"success": True, "data": [row]})

    with pytest.raises(MexcError, match="open position"):
        await _client_with_handler(handler).positions("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", [None, "", " ", True, 123])
async def test_positions_reject_invalid_symbol_identity(symbol):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "symbol": symbol,
                        "positionType": 1,
                        "openType": 1,
                        "holdVol": 1,
                        "holdAvgPrice": 100,
                    }
                ],
            },
        )

    with pytest.raises(MexcError, match="open position identity"):
        await _client_with_handler(handler).positions("BTC_USDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["holdVol", "holdAvgPrice"])
async def test_positions_reject_boolean_required_financial_fields(field):
    def handler(request: httpx.Request) -> httpx.Response:
        row = {
            "symbol": "BTC_USDT",
            "positionType": 1,
            "openType": 1,
            "holdVol": 1,
            "holdAvgPrice": 100,
        }
        row[field] = True
        return httpx.Response(200, json={"success": True, "data": [row]})

    with pytest.raises(MexcError, match="open position"):
        await _client_with_handler(handler).positions("BTC_USDT")


@pytest.mark.asyncio
async def test_positions_filters_explicitly_foreign_rows_before_validation():
    target = {
        "symbol": "BTC_USDT",
        "positionType": 1,
        "openType": 1,
        "holdVol": 1,
        "holdAvgPrice": 100,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTC_USDT"
        return httpx.Response(
            200,
            json={"success": True, "data": [target, {"symbol": "ETH_USDT"}]},
        )

    rows = await _client_with_handler(handler).positions("BTC_USDT")

    assert rows == [target]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"currency": "USDT"}, "not-a-list", 7, [{"currency": "USDT"}, "bad"]],
)
async def test_assets_rejects_unrecognized_or_non_object_rows(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="account-assets response"):
        await _client_with_handler(handler).assets(fresh=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_endpoint", ["assets", "positions"])
async def test_account_state_cancels_hanging_sibling_on_read_error(failed_endpoint):
    client = MexcClient("https://contract.mexc.com", "k", "s")
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()

    async def fail(*_args, **_kwargs):
        await sibling_started.wait()
        raise MexcError(f"{failed_endpoint} failed")

    async def hang(*_args, **_kwargs):
        sibling_started.set()
        try:
            await never_finishes.wait()
        finally:
            sibling_cancelled.set()

    if failed_endpoint == "assets":
        client.assets = fail
        client.positions = hang
    else:
        client.assets = hang
        client.positions = fail

    try:
        with pytest.raises(MexcError, match=f"{failed_endpoint} failed"):
            await asyncio.wait_for(client.account_state(fresh=True), timeout=0.25)
        assert sibling_cancelled.is_set()
    finally:
        never_finishes.set()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "BTC_USDT"},
        "not-a-list",
        7,
        [
            {
                "symbol": "BTC_USDT",
                "positionType": 1,
                "openType": 1,
                "holdVol": 1,
                "holdAvgPrice": 100,
            },
            "bad",
        ],
    ],
)
async def test_positions_rejects_unrecognized_or_non_object_rows(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": payload})

    with pytest.raises(MexcError, match="open.position"):
        await _client_with_handler(handler).positions("BTC_USDT", fresh=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("open_type", [None, 0, 3])
async def test_positions_reject_missing_or_unknown_open_type(open_type):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "symbol": "BTC_USDT",
                        "positionType": 1,
                        "openType": open_type,
                        "holdVol": 1,
                        "holdAvgPrice": 100,
                    }
                ],
            },
        )

    with pytest.raises(MexcError, match="openType"):
        await _client_with_handler(handler).positions("BTC_USDT", fresh=True)
