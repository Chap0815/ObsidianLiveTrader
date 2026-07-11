from app.mexc.client import (
    INTERVAL_MAP,
    normalize_klines,
    sign_payload,
    sorted_query,
)


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
