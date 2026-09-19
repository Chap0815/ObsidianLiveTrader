"""B3-06: /ws/market's Origin-Check muss konsistent mit dem HTTP-Pfad sein.

Vorher: der Handshake pruefte nur den HOST des Origin gegen eine feste
Loopback-Allowlist (127.0.0.1/localhost/::1) — ein Origin auf einem ANDEREN
Port derselben Loopback-Adresse (z.B. http://127.0.0.1:9999, waehrend die App
selbst auf 8787 laeuft) waere durchgerutscht. Der HTTP-CSRF-Guard in
app.security.loopback_or_token_middleware prueft dagegen schon PORT-GENAU
(_origin_matches_request) — dieselbe Origin/Referer-Logik wird hier
wiederverwendet, damit beide Pfade dasselbe Kriterium anwenden.

Die "guter Origin wird akzeptiert"-Gegenprobe fuer /ws/market lebt bereits in
tests/test_top10_gaps.py (test_ws_market_accepts_loopback_origin_mexc_poll_branch,
dort auf "http://testserver" angepasst, dem tatsaechlichen Host von
TestClient). Hier fehlt nur der Cross-PORT-Fall.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import URL
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import app
from app.security import _origin_matches_request


def test_secure_websocket_matches_https_origin_on_default_port():
    request = SimpleNamespace(url=URL("wss://example.test/ws/market"))

    assert _origin_matches_request("https://example.test", request) is True


def test_origin_check_rejects_invalid_request_port_without_raising():
    request = SimpleNamespace(url=URL("http://localhost:notaport/api/orders/preview"))

    assert _origin_matches_request("http://localhost", request) is False


def test_ws_market_rejects_cross_port_origin_with_1008(monkeypatch):
    """Gleicher Host, ANDERER Port als der tatsaechliche Request (TestClient
    bedient unter "testserver", Default-Port) -> muss wie ein fremder Origin
    mit Code 1008 abgelehnt werden (port-genauer Check, B3-06)."""
    from app.config import get_settings

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    get_settings.cache_clear()
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect) as ei:
            with tc.websocket_connect(
                "/ws/market?symbol=BTC",
                headers={"origin": "http://testserver:9999"},
            ):
                pass
    assert ei.value.code == 1008


def test_ws_market_rejects_invalid_interval_with_1008(monkeypatch):
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect) as ei:
            with tc.websocket_connect(
                "/ws/market?symbol=BTC&tf=invalid",
                headers={"origin": "http://testserver"},
            ):
                pass
    assert ei.value.code == 1008


@pytest.mark.asyncio
async def test_ws_market_rejects_non_loopback_client_before_accept(monkeypatch):
    """WebSockets bypass HTTP middleware, so the handler enforces loopback too."""
    import app.main as main

    class FakeWebSocket:
        headers: dict[str, str] = {}
        client = SimpleNamespace(host="192.0.2.10")
        url = URL("ws://localhost:8787/ws/market")
        app = SimpleNamespace(state=SimpleNamespace())

        def __init__(self):
            self.accepted = False
            self.close_codes: list[int] = []

        async def accept(self):
            self.accepted = True

        async def close(self, code=1000):
            self.close_codes.append(code)

        async def send_json(self, _payload):
            return None

    fallback = AsyncMock(side_effect=WebSocketDisconnect())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(exchange="mexc"),
    )
    monkeypatch.setattr(main, "normalize_symbol", lambda value: value)
    monkeypatch.setattr(main, "_mexc_poll_fallback", fallback)
    websocket = FakeWebSocket()

    await main.ws_market(websocket, symbol="BTC", tf="15m")

    assert websocket.accepted is False
    assert websocket.close_codes == [1008]
    fallback.assert_not_awaited()
