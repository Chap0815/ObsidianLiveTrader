"""First-run setup: marker-based gating + lock-once-complete guard.

The env-content builder itself is covered by test_env_builder.py; this file
covers the /setup + /api/setup wiring and the SETUP_COMPLETE marker.
"""

import pytest

from app.config import Settings
from app.env_builder import build_full_env, normalize_answers
from app.main import ENV_PATH


def _payload(**kw):
    base = dict(
        exchange="hl-testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="",
        llm_provider="claude",
        llm_api_key="sk-ant-test",
        risk_profile="conservative",
        port=8787,
    )
    base.update(kw)
    return base


def test_full_env_roundtrips_through_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_full_env(normalize_answers(_payload()))
    assert "EXCHANGE=hyperliquid" in content
    assert "HL_TESTNET=true" in content
    assert "TRADING_ENABLED=false" in content
    assert "SETUP_COMPLETE=true" in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.exchange == "hyperliquid"
    assert s.hl_testnet is True
    assert s.trading_enabled is False
    assert s.setup_complete is True


def test_marker_transitions(tmp_path):
    """_setup_needed keys on SETUP_COMPLETE, not mere existence."""
    incomplete = tmp_path / ".env.a"
    incomplete.write_text("SETUP_COMPLETE=false\nHOST=127.0.0.1\n", encoding="utf-8")
    assert Settings(_env_file=str(incomplete)).setup_complete is False

    complete = tmp_path / ".env.b"
    complete.write_text("SETUP_COMPLETE=true\nHOST=127.0.0.1\n", encoding="utf-8")
    assert Settings(_env_file=str(complete)).setup_complete is True


@pytest.mark.skipif(
    not ENV_PATH.exists(), reason="needs existing .env to test the lock"
)
def test_setup_locked_when_setup_complete():
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import _setup_needed, app

    # Only meaningful when the live .env is actually marked complete.
    if _setup_needed():
        pytest.skip("live .env is not SETUP_COMPLETE")
    get_settings.cache_clear()
    with TestClient(app) as c:
        r = c.get("/setup", follow_redirects=False)
        assert r.status_code == 303  # locked → back to dashboard
        r2 = c.post("/api/setup", json=_payload())
        assert r2.status_code == 403  # cannot overwrite existing config
