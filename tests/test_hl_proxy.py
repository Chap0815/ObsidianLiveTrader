"""Hyperliquid WS proxy — trade message normalization (F-21)."""

from app.realtime.hl_proxy import _normalize_hl


def test_trade_message_wrong_coin_is_skipped_not_forwarded():
    """F-21: a trade for a different coin must be skipped (continue), not
    fall through and be forwarded as the requested coin's trade."""
    msg = {
        "channel": "trades",
        "data": [
            {"coin": "BTC", "px": "60000.0", "sz": "0.5", "side": "S", "time": 1000},
            {"coin": "ETH", "px": "3000.0", "sz": "1.0", "side": "B", "time": 2000},
        ],
    }
    out = _normalize_hl(msg, coin="BTC")
    assert out is not None
    assert out["coin"] == "BTC"
    # Must reflect the real BTC trade, NOT the later foreign ETH one.
    assert out["px"] == 60000.0
    assert len(out["trades"]) == 1
    assert all(t["px"] == 60000.0 for t in out["trades"])


def test_trade_message_all_foreign_coin_yields_no_trades():
    msg = {
        "channel": "trades",
        "data": [
            {"coin": "ETH", "px": "3000.0", "sz": "1.0", "side": "B", "time": 1000},
        ],
    }
    out = _normalize_hl(msg, coin="BTC")
    assert out is None


def test_trade_message_missing_coin_field_still_accepted():
    """Existing behavior: a trade with no coin field at all is accepted
    (the venue sometimes omits it on the subscribed-coin stream)."""
    msg = {
        "channel": "trades",
        "data": [
            {"px": "60000.0", "sz": "0.5", "side": "S", "time": 1000},
        ],
    }
    out = _normalize_hl(msg, coin="BTC")
    assert out is not None
    assert out["px"] == 60000.0
