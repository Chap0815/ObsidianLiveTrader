"""Authenticated post-setup KI-key management: no readback, whitelist, CSRF."""

from unittest.mock import AsyncMock

import app.main as main_mod


def _client():
    from fastapi.testclient import TestClient

    return TestClient(main_mod.app)


def test_get_status_returns_no_secrets():
    from app.env_builder import DEFAULT_MODELS

    with _client() as tc:
        r = tc.get("/api/settings/llm")
    assert r.status_code == 200
    data = r.json()
    assert "providers" in data and "active" in data
    ids = {p["id"] for p in data["providers"]}
    assert ids == {"claude", "xai", "openai", "ollama"}
    for p in data["providers"]:
        assert set(p) == {"id", "label", "configured", "model", "default_model"}
        assert p["default_model"] == DEFAULT_MODELS[p["id"]]
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

    marker = "SYNTHETIC_OVERSIZED_KEY_MARKER"
    secret = marker + "x" * (8193 - len(marker))
    with _client() as tc:
        r = tc.post(
            "/api/settings/llm-key",
            json={"provider": "openai", "api_key": secret},
        )

    assert r.status_code == 422
    assert marker not in r.text
    assert "[redacted]" in r.text
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


def test_llm_status_reports_external_position_review_as_blocked(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: Settings(
            llm_provider="claude",
            anthropic_api_key="synthetic-claude-key",
            include_account_in_llm=False,
        ),
    )
    with _client() as tc:
        tc.app.state.llm_override = None
        response = tc.get("/api/llm")

    assert response.status_code == 200, response.text
    assert response.json()["position_reevaluation_allowed"] is False


def test_llm_switch_reports_local_position_review_as_allowed(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        main_mod,
        "get_settings",
        lambda: Settings(
            llm_provider="claude",
            anthropic_api_key="synthetic-claude-key",
            include_account_in_llm=False,
        ),
    )
    with _client() as tc:
        tc.app.state.llm_override = None
        response = tc.post("/api/llm", json={"provider": "ollama"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "ollama"
    assert body["position_reevaluation_allowed"] is True


def test_settings_probe_rejects_oversized_key_before_provider(monkeypatch):
    import app.llm.probe as probe_mod

    probe = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(probe_mod, "probe_provider", probe)
    marker = "SYNTHETIC_PROBE_KEY_MARKER"
    secret = marker + "x" * (8193 - len(marker))
    with _client() as tc:
        r = tc.post(
            "/api/settings/test-provider",
            json={"provider": "openai", "api_key": secret},
        )

    assert r.status_code == 422
    assert marker not in r.text
    assert "[redacted]" in r.text
    probe.assert_not_awaited()


def test_validation_error_redacts_nested_secret_inputs(monkeypatch):
    marker = "SYNTHETIC_NESTED_KEY_MARKER"
    monkeypatch.setattr(main_mod, "_setup_needed", lambda: True)
    with _client() as tc:
        r = tc.post(
            "/api/setup/test-provider",
            json={
                "provider": {"ANTHROPIC_API_KEY": marker},
                "api_key": "bounded",
            },
        )

    assert r.status_code == 422
    assert marker not in r.text
    assert "[redacted]" in r.text


def test_validation_error_redacts_scalar_body_that_may_be_a_secret():
    marker = "SYNTHETIC_RAW_BODY_SECRET_MARKER"
    with _client() as tc:
        r = tc.post("/api/settings/test-provider", json=marker)

    assert r.status_code == 422
    assert marker not in r.text
    assert "[redacted]" in r.text


def test_validation_error_redacts_provider_specific_secret_field():
    marker = "SYNTHETIC_PROVIDER_KEY_MARKER"
    with _client() as tc:
        r = tc.post(
            "/api/settings/test-provider",
            json={
                "provider": "openai",
                "api_key": "bounded",
                "ANTHROPIC_API_KEY": marker,
            },
        )

    assert r.status_code == 422
    assert marker not in r.text
    assert "[redacted]" in r.text


def test_validation_error_redacts_one_time_confirm_token():
    marker = "SYNTHETIC_CONFIRM_TOKEN_MARKER"
    with _client() as tc:
        r = tc.post("/api/orders/confirm", json={"token": "A" * 43 + marker})

    assert r.status_code == 422
    assert marker not in r.text
    assert "[redacted]" in r.text


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


def test_llm_key_acl_failure_maps_to_server_error_without_publishing(
    tmp_path, monkeypatch
):
    import app.env_builder as env_builder

    env = tmp_path / ".env"
    original = "SETUP_COMPLETE=true\nXAI_API_KEY=old-value\nXAI_MODEL=grok-4\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(main_mod, "ENV_PATH", env)
    monkeypatch.setattr(env_builder, "restrict_env_permissions", lambda path: False)

    with _client() as tc:
        response = tc.post(
            "/api/settings/llm-key",
            json={"provider": "xai", "api_key": "new-secret", "model": "grok-4"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Local configuration permissions could not be secured."
    )
    assert env.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".env.*.tmp")) == []
