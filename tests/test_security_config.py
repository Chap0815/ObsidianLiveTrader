"""Security / config remaining audit fixes."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.security import normalize_symbol
from fastapi import HTTPException


def test_host_rejects_lan_bind():
    with pytest.raises(ValidationError):
        Settings(host="0.0.0.0")


def test_mexc_url_must_https_allowlist():
    with pytest.raises(ValidationError):
        Settings(mexc_base_url="http://contract.mexc.com")
    with pytest.raises(ValidationError):
        Settings(mexc_base_url="https://evil.example.com")
    s = Settings(mexc_base_url="https://contract.mexc.com")
    assert s.mexc_base_url.startswith("https://")


def test_ollama_url_must_be_loopback():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://evil.example.com:11434/v1")
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="https://169.254.169.254/v1")
    s = Settings(ollama_base_url="http://127.0.0.1:11434/v1")
    assert "127.0.0.1" in s.ollama_base_url


def test_risk_floats_reject_nan_and_infinity():
    """F-06: NaN/Infinity in money/risk fields must be rejected at construction,
    not silently accepted (which would make gate comparisons fail-open)."""
    for field in (
        "max_risk_pct",
        "min_rrr",
        "max_notional_pct_of_equity",
        "risk_slippage_pct",
        "max_notional_usdt",
        "max_price_drift_pct",
        "market_entry_slippage_pct",
        "sl_verify_delay_s",
    ):
        with pytest.raises(ValidationError):
            Settings(**{field: float("nan")})
        with pytest.raises(ValidationError):
            Settings(**{field: float("inf")})
        with pytest.raises(ValidationError):
            Settings(**{field: float("-inf")})


def test_risk_floats_reject_out_of_range():
    with pytest.raises(ValidationError):
        Settings(max_risk_pct=0.0)  # must be > 0
    with pytest.raises(ValidationError):
        Settings(max_risk_pct=150.0)  # > 100
    with pytest.raises(ValidationError):
        Settings(max_leverage=0)  # must be >= 1
    with pytest.raises(ValidationError):
        Settings(max_notional_pct_of_equity=-1.0)  # must be >= 0
    with pytest.raises(ValidationError):
        Settings(max_notional_usdt=-1.0)  # must be >= 0


def test_risk_floats_accept_valid_values():
    s = Settings(
        max_risk_pct=5.0,
        min_rrr=2.0,
        max_notional_pct_of_equity=5000.0,
        risk_slippage_pct=0.05,
        max_notional_usdt=500.0,
        max_price_drift_pct=0.5,
        market_entry_slippage_pct=0.15,
        sl_verify_delay_s=0.7,
        max_leverage=50,
    )
    assert s.max_risk_pct == 5.0
    assert s.max_notional_pct_of_equity == 5000.0
    # 0 = off must still be allowed for the equity-relative cap and the
    # fixed-USDT warning threshold
    s2 = Settings(max_notional_pct_of_equity=0.0, max_notional_usdt=0.0)
    assert s2.max_notional_pct_of_equity == 0.0
    assert s2.max_notional_usdt == 0.0


def test_llm_base_urls_https_allowlist():
    with pytest.raises(ValidationError):
        Settings(anthropic_base_url="https://evil.example.com")
    with pytest.raises(ValidationError):
        Settings(xai_base_url="http://api.x.ai/v1")
    with pytest.raises(ValidationError):
        Settings(openai_base_url="https://evil.openai.com/v1")
    s = Settings(
        anthropic_base_url="https://api.anthropic.com",
        xai_base_url="https://api.x.ai/v1",
        openai_base_url="https://api.openai.com/v1",
    )
    assert "anthropic.com" in s.anthropic_base_url
    assert "api.x.ai" in s.xai_base_url
    assert "openai.com" in s.openai_base_url


def test_symbol_regex():
    from app.security import SYMBOL_RE_HL, SYMBOL_RE_MEXC

    assert SYMBOL_RE_MEXC.match("BTC_USDT")
    assert SYMBOL_RE_HL.match("BTC")
    assert SYMBOL_RE_HL.match("BTC_USDT")
    assert not SYMBOL_RE_HL.match("../evil")
    with pytest.raises(HTTPException):
        normalize_symbol("not a symbol!!!")
    # Active EXCHANGE from settings (default hyperliquid) → bare coin
    assert normalize_symbol("btc_usdt") in ("BTC", "BTC_USDT")


def test_strict_available_margin_gate():
    from app.models import ContractMeta, OrderTicket
    from app.risk.gates import validate_order

    ticket = OrderTicket(
        symbol="BTC_USDT",
        side="long",
        order_type="limit",
        vol=10000.0,  # huge
        leverage=5,
        price=100_000.0,
        entry=100_000.0,
        stop_loss=99_000.0,
        take_profit=120_000.0,
        open_type=1,
    )
    contract = ContractMeta(
        symbol="BTC_USDT",
        contract_size=0.0001,
        price_unit=0.1,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1_000_000.0,
        max_leverage=125,
        min_leverage=1,
        api_allowed=True,
    )
    # notional = 10000 * 0.0001 * 100000 = 100000; IM at 5x = 20000 > available 100
    s = Settings(
        trading_enabled=True,
        max_notional_usdt=1_000_000,
        max_risk_pct=100.0,
        strict_available_margin=True,
        max_leverage=125,
        local_api_token="test-token",
    )
    g = validate_order(
        ticket,
        contract,
        equity=50_000.0,
        settings=s,
        last_price=100_000.0,
        available_usdt=100.0,
    )
    assert g.ok is False
    assert any("available" in e.lower() or "IM" in e for e in g.errors)


def test_csrf_cross_origin_mutating_blocked(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        r = tc.post(
            "/api/scan",
            json={},
            headers={"Origin": "https://evil.example.com"},
        )
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"].lower()


def test_csrf_same_origin_and_no_origin_pass(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        # SAME-origin (Origin host:port == the server the request hit) is allowed
        # by the CSRF guard (may still fail later for other reasons, but must NOT
        # be the 403 cross-origin block). TestClient's host is "testserver".
        r1 = tc.post(
            "/api/scan", json={}, headers={"Origin": "http://testserver"}
        )
        # no Origin/Referer (non-browser) also passes the guard
        r2 = tc.post("/api/scan", json={})
        # cross-PORT on the same loopback host is a DIFFERENT origin → blocked
        # (the port-aware same-origin fix; hostname-only would have let it pass)
        r3 = tc.post(
            "/api/scan", json={}, headers={"Origin": "http://testserver:9999"}
        )
    assert r1.status_code != 403 or "cross-origin" not in r1.json().get("detail", "").lower()
    assert r2.status_code != 403 or "cross-origin" not in r2.json().get("detail", "").lower()
    assert r3.status_code == 403 and "cross-origin" in r3.json()["detail"].lower()


def test_resolved_llm_provider_falls_back_to_a_configured_one():
    """LLM_PROVIDER=claude with no Anthropic key must resolve to a provider that
    IS configured (e.g. xai) instead of failing the analysis on unconfigured
    Claude."""
    from app.config import Settings

    s = Settings(llm_provider="claude", anthropic_api_key="", xai_api_key="xai-abc")
    assert s.resolved_llm_provider == "xai"
    # a configured provider is left unchanged
    s2 = Settings(llm_provider="xai", xai_api_key="xai-abc")
    assert s2.resolved_llm_provider == "xai"


def test_api_responses_have_no_store_and_nosniff():
    """B-01: every /api/* JSON response must be non-cacheable, non-sniffable
    and must not leak the path via Referer — private data (balances,
    proposals, journal) must never end up in a browser/proxy disk cache."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as tc:
        r = tc.get("/api/health")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_env_file_written_restrictive_perms(tmp_path):
    """B-08: .env carries exchange API secrets and the local auth token, so it
    must not be group-/world-readable after an atomic write. POSIX: chmod
    0600 (owner-only). Windows has no POSIX mode bits; the icacls best-effort
    branch must actually tighten the ACL down to the current user (Full
    Control) with inherited ACEs stripped — not just avoid raising."""
    import os as _os
    import stat

    from app.env_builder import SETTINGS_LLM_WRITABLE, patch_env_vars

    p = tmp_path / ".env"
    p.write_text("TRADING_ENABLED=false\nXAI_API_KEY=\n", encoding="utf-8")
    patch_env_vars(p, {"XAI_API_KEY": "secret-value"}, allowed=set(SETTINGS_LLM_WRITABLE))

    if _os.name == "nt":
        import subprocess

        out = subprocess.run(
            ["icacls", str(p)], capture_output=True, text=True, check=False
        ).stdout
        user = _os.environ.get("USERNAME", "")
        assert user, "USERNAME env var must be set to assert the ACL narrowing"
        low = out.lower()
        assert user.lower() in low
        # Windows grants SYSTEM + BUILTIN\Administrators on new files by
        # default (inherited from the parent dir) — real evidence the
        # hardening ran is that /inheritance:r stripped exactly those
        # inherited ACEs, leaving only the current user's grant.
        assert "nt authority\\system" not in low
        assert "builtin\\administrators" not in low
    else:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600
