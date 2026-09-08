import pytest

from app.mexc.client import (
    INTERVAL_MAP,
    normalize_klines,
    parse_contract_meta,
    sign_payload,
    sorted_query,
)
from app.mexc.errors import MexcError


def test_sign_get_stable():
    # accessKey + reqTime + "a=1&b=2"
    sig = sign_payload(
        access_key="ak",
        secret_key="sk",
        req_time="1600000000000",
        param_string="a=1&b=2",
    )
    assert isinstance(sig, str)
    assert len(sig) == 64
    assert sig == sign_payload("ak", "sk", "1600000000000", "a=1&b=2")


def test_sign_post_uses_raw_json():
    body = '{"symbol":"BTC_USDT","vol":1}'
    s1 = sign_payload("ak", "sk", "1", body)
    s2 = sign_payload("ak", "sk", "1", body)
    assert s1 == s2


def test_sorted_query_skips_none_and_sorts():
    assert sorted_query(None) == ""
    assert sorted_query({}) == ""
    assert sorted_query({"b": 2, "a": 1, "c": None}) == "a=1&b=2"


def test_sorted_query_url_encodes_values():
    # spaces and special chars must be percent-encoded for signature stability
    q = sorted_query({"q": "a b", "x": "1&2"})
    assert "q=a%20b" in q or "q=a%20b" == q.split("&")[0]
    assert "x=1%262" in q


def test_interval_map():
    assert INTERVAL_MAP["5m"] == "Min5"
    assert INTERVAL_MAP["15m"] == "Min15"
    assert INTERVAL_MAP["1H"] == "Min60"
    assert INTERVAL_MAP["4H"] == "Hour4"
    assert INTERVAL_MAP["1D"] == "Day1"


def test_normalize_klines_seconds_to_ms():
    raw = {
        "time": [1609740600],
        "open": [33016.5],
        "close": [33040.5],
        "high": [33094.0],
        "low": [32995.0],
        "vol": [67332.0],
        "amount": [222515.85925],
    }
    candles = normalize_klines(raw)
    assert len(candles) == 1
    c = candles[0]
    assert c.time == 1609740600 * 1000
    assert c.open == 33016.5
    assert c.high == 33094.0
    assert c.low == 32995.0
    assert c.close == 33040.5
    assert c.vol == 67332.0
    assert c.amount == 222515.85925


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "vol", "amount"])
def test_normalize_klines_rejects_nonfinite_values(field):
    raw = {
        "time": [1609740600],
        "open": [1.0],
        "high": [1.0],
        "low": [1.0],
        "close": [1.0],
        "vol": [1.0],
        "amount": [1.0],
    }
    raw[field] = ["NaN"]

    with pytest.raises(MexcError, match=f"kline {field} is non-finite"):
        normalize_klines(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.pop("amount"),
        lambda raw: raw.update(open="not-an-array"),
        lambda raw: raw.update(high=[]),
    ],
)
def test_normalize_klines_rejects_missing_non_array_or_unequal_fields(mutation):
    raw = {
        "time": [1609740600],
        "open": [1.0],
        "high": [1.0],
        "low": [1.0],
        "close": [1.0],
        "vol": [0.0],
        "amount": [0.0],
    }
    mutation(raw)

    with pytest.raises(MexcError, match="kline payload"):
        normalize_klines(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("time", 0),
        ("open", 0),
        ("high", -1),
        ("low", 0),
        ("close", -1),
        ("vol", -1),
        ("amount", -1),
    ],
)
def test_normalize_klines_rejects_invalid_required_ranges(field, value):
    raw = {
        "time": [1609740600],
        "open": [1.0],
        "high": [1.0],
        "low": [1.0],
        "close": [1.0],
        "vol": [0.0],
        "amount": [0.0],
    }
    raw[field] = [value]

    with pytest.raises(MexcError, match="kline"):
        normalize_klines(raw)


@pytest.mark.parametrize(
    "field",
    ["contractSize", "priceUnit", "volUnit", "minVol", "maxVol"],
)
def test_contract_meta_rejects_nonfinite_filters(field):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "state": 0,
    }
    row[field] = "NaN"

    with pytest.raises(MexcError, match=f"{field} is non-finite"):
        parse_contract_meta(row)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contractSize", 0),
        ("priceUnit", -0.1),
        ("volUnit", 0),
        ("minVol", 0),
        ("maxVol", -1),
    ],
)
def test_contract_meta_rejects_invalid_positive_filters(field, value):
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
    row[field] = value

    with pytest.raises(MexcError, match="must be > 0"):
        parse_contract_meta(row)


def test_contract_meta_treats_string_false_api_flag_as_disabled():
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": 50,
        "minLeverage": 1,
        "apiAllowed": "false",
        "state": 0,
    }

    assert parse_contract_meta(row).api_allowed is False


@pytest.mark.parametrize(
    ("min_leverage", "max_leverage"),
    [(0, 50), (1, 0), (20, 10)],
)
def test_contract_meta_rejects_invalid_leverage_bounds(min_leverage, max_leverage):
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": max_leverage,
        "minLeverage": min_leverage,
        "apiAllowed": True,
        "state": 0,
    }

    with pytest.raises(MexcError, match="leverage bounds"):
        parse_contract_meta(row)


def test_contract_meta_normalizes_invalid_leverage_type_to_mexc_error():
    row = {
        "symbol": "BTC_USDT",
        "contractSize": 0.001,
        "priceUnit": 0.1,
        "volUnit": 1,
        "minVol": 1,
        "maxVol": 1000,
        "maxLeverage": "garbage",
        "minLeverage": 1,
        "apiAllowed": True,
        "state": 0,
    }

    with pytest.raises(MexcError, match="maxLeverage is not an integer"):
        parse_contract_meta(row)
