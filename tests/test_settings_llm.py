"""Authenticated post-setup KI-key management: no readback, whitelist, CSRF."""

from unittest.mock import AsyncMock

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


def test_llm_key_update_replaces_analysis_cache(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "SETUP_COMPLETE=true\nXAI_API_KEY=old-key\nXAI_MODEL=grok-old\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_mod, "ENV_PATH", env)

    with _client() as tc:
        old_cache = {("BTC_USDT",): (0.0, {"proposal": "old-model"})}
        tc.app.state.analyze_cache = old_cache
        r = tc.post(
            "/api/settings/llm-key",
            json={"provider": "xai", "api_key": "new-key", "model": "grok-new"},
        )

        assert r.status_code == 200, r.text
        assert tc.app.state.analyze_cache == {}
        assert tc.app.state.analyze_cache is not old_cache


def test_llm_key_rejects_unknown_safety_field_before_write(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SETUP_COMPLETE=true\nTRADING_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "ENV_PATH", env)
    with _client() as tc:
        r = tc.post(
            "/api/settings/llm-key",
            json={
                "provider": "openai",
                "api_key": "sk-o",
                "TRADING_ENABLED": "true",
                "model": "gpt-5.1",
            },
        )
    assert r.status_code == 422
    assert env.read_text(encoding="utf-8") == (
        "SETUP_COMPLETE=true\nTRADING_ENABLED=false\n"
    )


def test_llm_key_rejects_oversized_key_before_write(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    original = "SETUP_COMPLETE=true\nOPENAI_API_KEY=\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(main_mod, "ENV_PATH", env)

    with _client() as tc:
        r = tc.post(
            "/api/settings/llm-key",
            json={"provider": "openai", "api_key": "x" * 8193},
        )

    assert r.status_code == 422
    assert env.read_text(encoding="utf-8") == original


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


def test_llm_select_rejects_unknown_persistence_field(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: Settings(llm_provider="xai", xai_api_key="xai-test"),
    )
    with _client() as tc:
        r = tc.post("/api/llm", json={"provider": "xai", "persist": True})

    assert r.status_code == 422


def test_llm_select_identifies_missing_model(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            llm_provider="xai",
            xai_api_key="synthetic-xai-key",
            xai_model=" \t ",
        ),
    )

    with _client() as tc:
        response = tc.post("/api/llm", json={"provider": "xai"})

    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "model" in detail
    assert "api key" not in detail


def test_llm_select_replaces_analysis_cache_when_provider_changes(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: Settings(
            llm_provider="claude",
            anthropic_api_key="claude-test",
            xai_api_key="xai-test",
        ),
    )
    with _client() as tc:
        tc.app.state.llm_override = None
        old_cache = {("BTC_USDT",): (0.0, {"provider": "claude"})}
        tc.app.state.analyze_cache = old_cache

        response = tc.post("/api/llm", json={"provider": "xai"})

        assert response.status_code == 200, response.text
        assert tc.app.state.llm_override == "xai"
        assert tc.app.state.analyze_cache == {}
        assert tc.app.state.analyze_cache is not old_cache


def test_llm_status_does_not_report_deliberate_override_as_fallback(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: Settings(
            llm_provider="claude",
            anthropic_api_key="synthetic-claude-key",
            xai_api_key="synthetic-xai-key",
        ),
    )
    with _client() as tc:
        tc.app.state.llm_override = "xai"
        response = tc.get("/api/llm")

    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "xai"
    assert response.json()["provider_configured"] == "claude"
    assert response.json()["fallback_active"] is False


def test_settings_probe_rejects_oversized_key_before_provider(monkeypatch):
    import app.llm.probe as probe_mod

    probe = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(probe_mod, "probe_provider", probe)
    with _client() as tc:
        r = tc.post(
            "/api/settings/test-provider",
            json={"provider": "openai", "api_key": "x" * 8193},
        )

    assert r.status_code == 422
    probe.assert_not_awaited()


def test_setup_probe_rejects_unknown_field_before_provider(monkeypatch):
    import app.llm.probe as probe_mod

    probe = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(probe_mod, "probe_provider", probe)
    monkeypatch.setattr(main_mod, "_setup_needed", lambda: True)
    with _client() as tc:
        r = tc.post(
            "/api/setup/test-provider",
            json={"provider": "openai", "api_key": "x", "save_key": True},
        )

    assert r.status_code == 422
    probe.assert_not_awaited()


def test_probe_request_forwards_valid_bounded_fields(monkeypatch):
    import app.llm.probe as probe_mod

    probe = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(probe_mod, "probe_provider", probe)
    monkeypatch.setattr(main_mod, "_setup_needed", lambda: True)
    with _client() as tc:
        r = tc.post(
            "/api/setup/test-provider",
            json={"provider": "openai", "api_key": "test-key", "model": "gpt-test"},
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    probe.assert_awaited_once()
    assert probe.await_args.args == ("openai",)
    assert probe.await_args.kwargs["api_key"] == "test-key"
    assert probe.await_args.kwargs["model"] == "gpt-test"


# ── Finding 3: os.replace transient Windows lock -> 409, not opaque 500 ─────
def test_llm_key_permission_error_maps_to_409(tmp_path, monkeypatch):
    """A transient Windows file lock during the .env replace (OneDrive/AV/an
    open editor briefly holding a handle) must not surface as an opaque
    500 — the old .env stays intact (fail-safe) and the client gets a clear
    409 to retry."""
    import app.env_builder as env_builder

    env = tmp_path / ".env"
    env.write_text(
        "SETUP_COMPLETE=true\nXAI_API_KEY=\nXAI_MODEL=grok-4\n", encoding="utf-8"
    )
    monkeypatch.setattr(main_mod, "ENV_PATH", env)

    def always_fail(src, dst):
        raise PermissionError("WinError 5: Zugriff verweigert")

    monkeypatch.setattr(env_builder.os, "replace", always_fail)
    monkeypatch.setattr(env_builder.time, "sleep", lambda s: None)  # no real delay in tests

    with _client() as tc:
        r = tc.post(
            "/api/settings/llm-key",
            json={"provider": "xai", "api_key": "xai-secret", "model": "grok-4"},
        )
    assert r.status_code == 409
    assert "locked" in r.json()["detail"]
    assert "XAI_API_KEY=xai-secret" not in env.read_text(encoding="utf-8")
