"""Q-07: a single _exchange_client(request) helper replaces the divergent
client-resolution that used to exist across main.py call-sites.

Before this fix:
  - analyze / reevaluate / market / scan checked ONLY app.state.mexc.
  - account / fills / mini used `app.state.mexc or app.state.exchange`.

Both groups now go through the same helper, so a request whose state has
only one of the two attributes populated behaves identically everywhere.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import _exchange_client, app


def test_exchange_client_helper_used():
    sentinel = object()

    req_mexc_only = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(mexc=sentinel))
    )
    req_exchange_only = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(exchange=sentinel))
    )
    req_neither = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    assert _exchange_client(req_mexc_only) is sentinel
    assert _exchange_client(req_exchange_only) is sentinel
    assert _exchange_client(req_neither) is None

    # Integration: /api/market used to check ONLY app.state.mexc. With that
    # cleared and only the .exchange alias populated, it must still resolve
    # a client instead of 503ing "MEXC client not initialized" — proving the
    # formerly mexc-only call-sites now share the same fallback as
    # account/fills/mini.
    mock_snap = {
        "symbol": "BTC_USDT",
        "last_price": 1.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 1.0},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }
    with TestClient(app) as tc:
        tc.app.state.mexc = None
        tc.app.state.exchange = MagicMock()
        with (
            patch(
                "app.main.build_market_snapshot",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch("app.main.snapshot_to_api_dict", return_value=mock_snap),
        ):
            r = tc.get("/api/market/BTC_USDT")
    assert r.status_code == 200, r.text
    assert r.json()["symbol"] == "BTC_USDT"
