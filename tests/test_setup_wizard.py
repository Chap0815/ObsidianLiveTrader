"""First-run env generator: content builder + lock-once-configured guard."""

import pytest

from app.config import Settings
from app.main import ENV_PATH, build_env_content


def _payload(**kw):
    base = dict(
        exchange="hl-testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="",
        mexc_api_key="",
        mexc_api_secret="",
        llm_provider="claude",
        llm_api_key="sk-ant-test",
        ollama_model="llama3.1",
        max_risk_pct=1.0,
        max_leverage=20,
        min_rrr=2.0,
        max_notional_usdt=500,
    )
    base.update(kw)
    return base


def test_env_content_valid_and_parseable(tmp_path, monkeypatch):
    # conftest pins EXCHANGE in os.environ; real env vars beat the env file
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_env_content(_payload())
    assert "EXCHANGE=hyperliquid" in content
    assert "HL_TESTNET=true" in content
    assert "TRADING_ENABLED=false" in content  # always disarmed on first run
    assert "LOCAL_API_TOKEN=" in content and "LOCAL_API_TOKEN=\n" not in content
    assert "ANTHROPIC_API_KEY=sk-ant-test" in content
    # Must round-trip through pydantic-settings without errors
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.exchange == "hyperliquid"
    assert s.hl_testnet is True
    assert s.trading_enabled is False
    assert s.max_risk_pct == 1.0


def test_env_content_mexc_variant(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    content = build_env_content(
        _payload(exchange="mexc", hl_private_key="", mexc_api_key="k", mexc_api_secret="s")
    )
    assert "EXCHANGE=mexc" in content
    assert "DEFAULT_SYMBOL=BTC_USDT" in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    assert Settings(_env_file=str(p)).exchange == "mexc"


def test_env_content_rejects_missing_keys():
    with pytest.raises(ValueError):
        build_env_content(_payload(hl_private_key=""))
    with pytest.raises(ValueError):
        build_env_content(_payload(hl_private_key="kein-hex"))
    with pytest.raises(ValueError):  # too short
        build_env_content(_payload(hl_private_key="0xabc"))
    with pytest.raises(ValueError):  # control-char injection attempt
        build_env_content(
            _payload(hl_private_key="0x" + "a" * 64 + "\nTRADING_ENABLED=true")
        )
    with pytest.raises(ValueError):
        build_env_content(_payload(exchange="mexc", mexc_api_key="", mexc_api_secret=""))
    with pytest.raises(ValueError):
        build_env_content(_payload(llm_provider="xai", llm_api_key=""))
    with pytest.raises(ValueError):
        build_env_content(_payload(max_risk_pct=99))


def test_ollama_and_none_need_no_key():
    assert "LLM_PROVIDER=ollama" in build_env_content(
        _payload(llm_provider="ollama", llm_api_key="")
    )
    # "none" starts with claude as inactive default, no key required
    assert "LLM_PROVIDER=claude" in build_env_content(
        _payload(llm_provider="none", llm_api_key="")
    )


@pytest.mark.skipif(not ENV_PATH.exists(), reason="needs existing .env to test the lock")
def test_setup_locked_when_env_exists():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/setup", follow_redirects=False)
        assert r.status_code == 303  # locked → back to dashboard
        r2 = c.post("/api/setup", json=_payload())
        assert r2.status_code == 403  # cannot overwrite existing config
