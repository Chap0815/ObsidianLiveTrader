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

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import app


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
