from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_live_flags():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["live_trading"] is True
    assert body["trading_enabled"] is False  # default disarmed
    assert "mexc_configured" in body
    assert "xai_configured" in body or "llm_configured" in body or "claude_configured" in body
