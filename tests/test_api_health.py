import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app


def test_health_returns_live_flags():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # F-24: default install is disarmed — live_trading must reflect that,
    # not unconditionally report True regardless of TRADING_ENABLED/testnet.
    assert body["live_trading"] is False
    assert body["trading_enabled"] is False  # default disarmed
    assert "mexc_configured" in body
    assert "xai_configured" in body or "llm_configured" in body or "claude_configured" in body


# --- F-24: live_trading must derive from the actual armed/mainnet state -----


def test_health_live_trading_false_when_disarmed(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(trading_enabled=False))
    r = TestClient(app).get("/api/health")
    assert r.json()["live_trading"] is False


def test_health_live_trading_false_on_hl_testnet_even_if_armed(monkeypatch):
    """Armed but still pointed at HL testnet must not report as live."""
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _settings(trading_enabled=True, exchange="hyperliquid", hl_testnet=True),
    )
    r = TestClient(app).get("/api/health")
    body = r.json()
    assert body["trading_enabled"] is True
    assert body["live_trading"] is False


def test_health_live_trading_true_when_armed_and_mainnet(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _settings(trading_enabled=True, exchange="hyperliquid", hl_testnet=False),
    )
    r = TestClient(app).get("/api/health")
    assert r.json()["live_trading"] is True


def test_health_live_trading_true_when_armed_mexc(monkeypatch):
    """MEXC has no testnet flag surfaced here — armed is enough."""
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _settings(trading_enabled=True, exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    r = TestClient(app).get("/api/health")
    assert r.json()["live_trading"] is True


def test_health_does_not_report_provider_alias_as_fallback(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _settings(llm_provider="grok", xai_api_key="synthetic-xai-key"),
    )
    with TestClient(app) as client:
        client.app.state.llm_override = None
        body = client.get("/api/health").json()

    assert body["llm_provider"] == "xai"
    assert body["llm_provider_configured"] == "grok"
    assert body["llm_fallback_active"] is False


@pytest.mark.parametrize(
    ("settings_kwargs", "allowed"),
    [
        (
            {
                "llm_provider": "claude",
                "anthropic_api_key": "synthetic-claude-key",
                "include_account_in_llm": False,
            },
            False,
        ),
        (
            {
                "llm_provider": "claude",
                "anthropic_api_key": "synthetic-claude-key",
                "include_account_in_llm": True,
            },
            True,
        ),
        ({"llm_provider": "ollama", "include_account_in_llm": False}, True),
    ],
)
def test_health_reports_when_position_reevaluation_is_allowed(
    monkeypatch, settings_kwargs, allowed
):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(**settings_kwargs))
    with TestClient(app) as client:
        client.app.state.llm_override = None
        body = client.get("/api/health").json()

    assert body["position_reevaluation_allowed"] is allowed


def _settings(**kwargs):
    from app.config import Settings

    base = dict(local_api_token="test-token")
    base.update(kwargs)
    return Settings(**base)


@pytest.mark.parametrize(
    ("settings_kwargs", "status_label", "network_label"),
    [
        (
            {
                "trading_enabled": False,
                "exchange": "hyperliquid",
                "hl_testnet": False,
            },
            "DISARMED",
            "HYPERLIQUID · MAINNET",
        ),
        (
            {
                "trading_enabled": True,
                "exchange": "hyperliquid",
                "hl_testnet": True,
            },
            "ARMED · TESTNET",
            "HYPERLIQUID · TESTNET",
        ),
        (
            {
                "trading_enabled": True,
                "exchange": "mexc",
                "mexc_api_key": "k",
                "mexc_api_secret": "s",
            },
            "ARMED · REAL FUNDS",
            "MEXC · LIVE VENUE",
        ),
    ],
)
def test_dashboard_initial_trading_status_matches_network(
    monkeypatch, settings_kwargs, status_label, network_label
):
    monkeypatch.setattr(main, "_setup_needed", lambda: False)
    monkeypatch.setattr(main, "get_settings", lambda: _settings(**settings_kwargs))

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert f'<span class="arm-text">{status_label}</span>' in response.text
    exchange_id = settings_kwargs["exchange"]
    assert (
        f'<span id="exchange-label" data-exchange="{exchange_id}">{network_label}</span>'
        in response.text
    )
