"""Authenticated post-setup KI-key management: no readback, whitelist, CSRF."""

import app.main as main_mod


def _client():
    from fastapi.testclient import TestClient

    return TestClient(main_mod.app)


def test_get_status_returns_no_secrets():
    with _client() as tc:
        r = tc.get("/api/settings/llm")
    assert r.status_code == 200
    data = r.json()
    assert "providers" in data and "active" in data
    ids = {p["id"] for p in data["providers"]}
    assert ids == {"claude", "xai", "openai", "ollama"}
    for p in data["providers"]:
        assert set(p) == {"id", "label", "configured", "model"}
        # non-secret fields only — never an api key
        assert "api_key" not in p
        assert "key" not in p


def test_llm_key_rejects_unknown_provider():
    with _client() as tc:
        r = tc.post("/api/settings/llm-key", json={"provider": "bogus", "api_key": "x"})
    assert r.status_code == 400


def test_llm_key_rejects_none_provider():
    with _client() as tc:
        r = tc.post("/api/settings/llm-key", json={"provider": "none"})
    assert r.status_code == 400


def test_llm_key_writes_via_whitelist_patch_and_never_echoes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "SETUP_COMPLETE=true\nTRADING_ENABLED=false\nXAI_API_KEY=\nXAI_MODEL=grok-4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_mod, "ENV_PATH", env)
    with _client() as tc:
        r = tc.post(
            "/api/settings/llm-key",
            json={"provider": "xai", "api_key": "xai-secret-123", "model": "grok-4"},
        )
    assert r.status_code == 200
    body = r.text
    assert "xai-secret-123" not in body  # secret never echoed
    out = env.read_text(encoding="utf-8")
    assert "XAI_API_KEY=xai-secret-123" in out
    assert "TRADING_ENABLED=false" in out  # preserved
    assert out.count("XAI_API_KEY=") == 1  # updated in place


def test_llm_key_cannot_touch_trading_enabled(tmp_path, monkeypatch):
    """Even a crafted body cannot flip a safety flag — provider is enum-gated and
    the patcher is whitelist-only."""
    env = tmp_path / ".env"
    env.write_text("SETUP_COMPLETE=true\nTRADING_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "ENV_PATH", env)
    with _client() as tc:
        # extra keys in the body are ignored; only provider's whitelisted vars write
        r = tc.post(
            "/api/settings/llm-key",
            json={
                "provider": "openai",
                "api_key": "sk-o",
                "TRADING_ENABLED": "true",
                "model": "gpt-5.1",
            },
        )
    assert r.status_code == 200
    assert "TRADING_ENABLED=false" in env.read_text(encoding="utf-8")


def test_settings_csrf_cross_origin_blocked():
    with _client() as tc:
        r = tc.post(
            "/api/settings/llm-key",
            json={"provider": "xai", "api_key": "x"},
            headers={"Origin": "https://evil.example.com"},
        )
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"].lower()


def test_test_provider_rejects_unknown():
    with _client() as tc:
        r = tc.post("/api/settings/test-provider", json={"provider": "bogus"})
    assert r.status_code == 400
