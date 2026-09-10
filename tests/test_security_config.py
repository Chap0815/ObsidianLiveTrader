"""Security / config remaining audit fixes."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import Settings
from app.security import normalize_symbol
from fastapi import HTTPException


def test_host_rejects_lan_bind():
    with pytest.raises(ValidationError):
        Settings(host="0.0.0.0")


@pytest.mark.parametrize("port", [0, 80, 65_536])
def test_settings_port_matches_setup_and_env_builder_bounds(port):
    with pytest.raises(ValidationError):
        Settings(port=port)


@pytest.mark.parametrize("port", [1024, 8787, 65_535])
def test_settings_port_accepts_documented_bounds(port):
    assert Settings(port=port).port == port


@pytest.mark.parametrize("exchange", ["", "   "])
def test_settings_rejects_blank_exchange(exchange):
    with pytest.raises(ValidationError, match="EXCHANGE must be"):
        Settings(_env_file=None, exchange=exchange)


def test_exchange_factory_rejects_unknown_mutated_exchange():
    from app.exchange_factory import create_exchange_client, exchange_ready

    settings = Settings(
        _env_file=None,
        exchange="mexc",
        mexc_api_key="synthetic-key",
        mexc_api_secret="synthetic-secret",
    )
    settings.exchange = "unknown"

    assert settings.exchange_ready is False
    assert exchange_ready(settings) is False
    with pytest.raises(ValueError, match="EXCHANGE must be"):
        create_exchange_client(settings)


def test_hyperliquid_whitespace_private_key_is_not_ready():
    from app.exchange_factory import exchange_ready

    settings = Settings(
        _env_file=None,
        exchange="hyperliquid",
        hl_private_key=" \t ",
    )

    assert settings.hl_ready is False
    assert settings.exchange_ready is False
    assert exchange_ready(settings) is False


@pytest.mark.parametrize(
    ("private_key", "account_address"),
    [
        ("synthetic-hl-key", ""),
        ("0x" + "a" * 63, ""),
        ("0x" + "0" * 64, ""),
        ("0x" + "f" * 64, ""),
        ("0x" + "a" * 64, "not-an-address"),
        ("0x" + "a" * 64, "0x" + "b" * 39),
    ],
)
def test_hyperliquid_malformed_credentials_are_not_ready(
    private_key, account_address
):
    from app.exchange_factory import exchange_ready

    settings = Settings(
        _env_file=None,
        exchange="hyperliquid",
        hl_private_key=private_key,
        hl_account_address=account_address,
    )

    assert settings.hl_ready is False
    assert settings.exchange_ready is False
    assert exchange_ready(settings) is False


@pytest.mark.parametrize("account_address", ["", "0x" + "b" * 40])
def test_hyperliquid_well_formed_credentials_are_ready(account_address):
    from app.exchange_factory import exchange_ready

    settings = Settings(
        _env_file=None,
        exchange="hyperliquid",
        hl_private_key="0x" + "a" * 64,
        hl_account_address=account_address,
    )

    assert settings.hl_ready is True
    assert settings.exchange_ready is True
    assert exchange_ready(settings) is True


@pytest.mark.parametrize(
    ("api_key", "api_secret"),
    [(" \t ", "synthetic-secret"), ("synthetic-key", " \t ")],
)
def test_mexc_whitespace_credentials_are_not_ready(api_key, api_secret):
    from app.exchange_factory import exchange_ready

    settings = Settings(
        _env_file=None,
        exchange="mexc",
        mexc_api_key=api_key,
        mexc_api_secret=api_secret,
    )

    assert settings.mexc_ready is False
    assert settings.exchange_ready is False
    assert exchange_ready(settings) is False


def test_mexc_url_must_https_allowlist():
    with pytest.raises(ValidationError):
        Settings(mexc_base_url="http://contract.mexc.com")
    with pytest.raises(ValidationError):
        Settings(mexc_base_url="https://evil.example.com")
    s = Settings(mexc_base_url="https://contract.mexc.com")
    assert s.mexc_base_url.startswith("https://")


def test_mexc_uses_current_api_host_and_migrates_legacy_default():
    current = "https://api.mexc.com"
    assert Settings(_env_file=None).mexc_base_url == current
    assert Settings(_env_file=None, mexc_base_url="").mexc_base_url == current
    assert (
        Settings(
            _env_file=None,
            mexc_base_url="https://contract.mexc.com",
        ).mexc_base_url
        == current
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mexc_base_url="https://futures.mexc.com")


@pytest.mark.parametrize(
    ("field", "url"),
    [
        (
            "hl_base_url",
            "https://user:secret@api.hyperliquid-testnet.xyz",
        ),
        (
            "hl_base_url",
            "https://api.hyperliquid-testnet.xyz?token=secret",
        ),
        ("mexc_base_url", "https://user:secret@api.mexc.com"),
        ("mexc_base_url", "https://api.mexc.com?token=secret"),
    ],
)
def test_exchange_base_urls_reject_embedded_secret_components(field, url):
    with pytest.raises(ValidationError, match="must not include"):
        Settings(_env_file=None, **{field: url})


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("hl_base_url", "https://api.hyperliquid-testnet.xyz/unexpected"),
        ("hl_base_url", "https://api.hyperliquid-testnet.xyz:444"),
        ("mexc_base_url", "https://api.mexc.com/unexpected"),
        ("mexc_base_url", "https://api.mexc.com:444"),
    ],
)
def test_exchange_base_urls_reject_noncanonical_endpoint_shape(field, url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: url})


def test_exchange_base_urls_allow_explicit_default_https_port():
    assert (
        Settings(
            _env_file=None,
            hl_base_url="https://api.hyperliquid-testnet.xyz:443",
        ).hl_base_url
        == "https://api.hyperliquid-testnet.xyz:443"
    )
    assert (
        Settings(_env_file=None, mexc_base_url="https://api.mexc.com:443").mexc_base_url
        == "https://api.mexc.com:443"
    )


def test_hyperliquid_network_flag_and_base_url_cannot_disagree():
    with pytest.raises(ValidationError, match="ambiguous Hyperliquid network"):
        Settings(
            _env_file=None,
            hl_testnet=True,
            hl_base_url="https://api.hyperliquid.xyz",
        )
    with pytest.raises(ValidationError, match="ambiguous Hyperliquid network"):
        Settings(
            _env_file=None,
            hl_testnet=False,
            hl_base_url="https://api.hyperliquid-testnet.xyz",
        )
    assert Settings(
        _env_file=None,
        hl_testnet=True,
        hl_base_url="https://api.hyperliquid-testnet.xyz",
    ).hl_testnet is True


def test_ollama_url_must_be_loopback():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://evil.example.com:11434/v1")
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="https://169.254.169.254/v1")
    s = Settings(ollama_base_url="http://127.0.0.1:11434/v1")
    assert "127.0.0.1" in s.ollama_base_url


@pytest.mark.parametrize(
    "url",
    [
        "http://placeholder@127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/v1?mode=bad",
        "http://127.0.0.1:11434/v1#bad",
    ],
)
def test_ollama_url_rejects_embedded_secret_components(url):
    with pytest.raises(ValidationError, match="must not include"):
        Settings(_env_file=None, ollama_base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:notaport/v1",
        "http://127.0.0.1:65536/v1",
    ],
)
def test_ollama_url_rejects_invalid_ports(url):
    with pytest.raises(ValidationError, match="valid port"):
        Settings(_env_file=None, ollama_base_url=url)


def test_ollama_url_allows_custom_valid_local_port():
    settings = Settings(
        _env_file=None,
        ollama_base_url="http://localhost:23456/v1",
    )
    assert settings.ollama_base_url == "http://localhost:23456/v1"


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


def test_tm_settings_defaults(monkeypatch):
    """Trade-Management-Layer defaults are safe-by-default and readable."""
    monkeypatch.delenv("TM_ENABLED")
    s = Settings(_env_file=None)
    assert s.tm_enabled is True
    assert s.tm_monitor_interval_s == 20
    assert s.tm_be_trigger_r == 1.0
    assert s.tm_be_fee_rt == 0.0006
    assert s.tm_time_stop_hours == 4.0
    assert s.tm_time_stop_min_r == 0.5


def test_test_harness_disables_background_network_loops():
    """Ordinary TestClient lifespans must not start real exchange readers."""
    s = Settings(_env_file=None)
    assert s.journal_enabled is False
    assert s.tm_enabled is False


def test_test_harness_does_not_read_repository_env():
    """Unit tests use explicit synthetic env files, never the local secret file."""
    assert Settings.model_config.get("env_file") is None


def _probe_test_database_path(sentinel: Path) -> tuple[str, str]:
    root = Path(__file__).resolve().parents[1]
    script = (
        "import os, runpy\n"
        "state = runpy.run_path('tests/conftest.py')\n"
        "print('SELECTED=' + os.environ['DATABASE_PATH'])\n"
        "print('DECLARED=' + state['_TEST_DB'])\n"
    )
    safe_env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    safe_env["DATABASE_PATH"] = str(sentinel)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=safe_env,
        capture_output=True,
        text=True,
        check=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return values["SELECTED"], values["DECLARED"]


def test_test_harness_overrides_external_database_path(tmp_path):
    sentinel = tmp_path / "must-not-use.db"
    selected, declared = _probe_test_database_path(sentinel)
    assert selected == declared
    assert selected != str(sentinel)


def test_test_harness_uses_process_unique_database_directory(tmp_path):
    first, _ = _probe_test_database_path(tmp_path / "first.db")
    second, _ = _probe_test_database_path(tmp_path / "second.db")
    assert Path(first).parent != Path(second).parent


def _probe_test_runtime_env(overrides: dict[str, str], names: tuple[str, ...]) -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    script = (
        "import os, runpy\n"
        "runpy.run_path('tests/conftest.py')\n"
        f"names = {names!r}\n"
        "[print(name + '=' + os.environ.get(name, '')) for name in names]\n"
    )
    safe_env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    safe_env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=safe_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_test_harness_forces_disarmed_test_runtime():
    names = (
        "EXCHANGE",
        "HL_TESTNET",
        "TRADING_ENABLED",
        "MAINNET_ACK",
        "INCLUDE_ACCOUNT_IN_LLM",
        "LLM_PROVIDER",
    )
    values = _probe_test_runtime_env(
        {
            "EXCHANGE": "hyperliquid",
            "HL_TESTNET": "false",
            "TRADING_ENABLED": "true",
            "MAINNET_ACK": "true",
            "INCLUDE_ACCOUNT_IN_LLM": "true",
            "LLM_PROVIDER": "openai",
        },
        names,
    )
    assert values == {
        "EXCHANGE": "mexc",
        "HL_TESTNET": "true",
        "TRADING_ENABLED": "false",
        "MAINNET_ACK": "false",
        "INCLUDE_ACCOUNT_IN_LLM": "false",
        "LLM_PROVIDER": "claude",
    }


def test_test_harness_clears_inherited_credentials():
    names = (
        "MEXC_API_KEY",
        "MEXC_API_SECRET",
        "HL_PRIVATE_KEY",
        "HL_ACCOUNT_ADDRESS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "XAI_API_KEY",
        "OPENAI_API_KEY",
    )
    values = _probe_test_runtime_env(
        {name: "synthetic-parent-secret" for name in names}, names
    )
    assert values == {name: "" for name in names}


def test_tm_settings_reject_out_of_bounds_and_nan():
    """TM_* numeric fields must reject NaN/Inf and out-of-bounds values so
    the monitor's gate comparisons never fail-open."""
    with pytest.raises(ValidationError):
        Settings(tm_monitor_interval_s=1)  # < 5
    with pytest.raises(ValidationError):
        Settings(tm_monitor_interval_s=301)  # > 300
    with pytest.raises(ValidationError):
        Settings(tm_be_trigger_r=float("nan"))
    with pytest.raises(ValidationError):
        Settings(tm_be_fee_rt=float("inf"))
    with pytest.raises(ValidationError):
        Settings(tm_time_stop_hours=0.1)  # < 0.25
    with pytest.raises(ValidationError):
        Settings(tm_time_stop_min_r=-10.0)  # < -5


def test_tm_trail_settings_defaults():
    """Auto-Trailing (ATR) defaults are safe-by-default and readable."""
    s = Settings()
    assert s.tm_trail_atr_mult == 2.0
    assert s.tm_trail_activation_r == 1.0
    assert s.tm_trail_atr_period == 14
    assert s.tm_trail_atr_tf == "15m"


def test_tm_trail_settings_reject_out_of_bounds_nan_and_invalid_tf():
    """TM_TRAIL_* fields must reject NaN/Inf/out-of-bounds/unknown timeframes
    so the trailing rule's gate comparisons never fail-open."""
    with pytest.raises(ValidationError):
        Settings(tm_trail_atr_mult=float("nan"))
    with pytest.raises(ValidationError):
        Settings(tm_trail_atr_mult=0.1)  # < 0.5
    with pytest.raises(ValidationError):
        Settings(tm_trail_activation_r=float("inf"))
    with pytest.raises(ValidationError):
        Settings(tm_trail_activation_r=-1.0)  # < 0
    with pytest.raises(ValidationError):
        Settings(tm_trail_atr_period=1)  # < 2
    with pytest.raises(ValidationError):
        Settings(tm_trail_atr_tf="3m")  # not in allowed set


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


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("anthropic_base_url", "https://user:secret@api.anthropic.com"),
        ("xai_base_url", "https://api.x.ai/v1?token=secret"),
        ("openai_base_url", "https://api.openai.com/v1#secret"),
    ],
)
def test_llm_base_urls_reject_embedded_secret_components(field, url):
    with pytest.raises(ValidationError, match="must not include"):
        Settings(_env_file=None, **{field: url})


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("anthropic_base_url", "https://api.anthropic.com/v1"),
        ("xai_base_url", "https://api.x.ai"),
        ("openai_base_url", "https://api.openai.com/v2"),
        ("anthropic_base_url", "https://api.anthropic.com:444"),
        ("xai_base_url", "https://api.x.ai:444/v1"),
        ("openai_base_url", "https://api.openai.com:444/v1"),
    ],
)
def test_llm_base_urls_reject_noncanonical_endpoint_shape(field, url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: url})


def test_llm_base_urls_allow_explicit_default_https_port():
    settings = Settings(
        _env_file=None,
        anthropic_base_url="https://api.anthropic.com:443",
        xai_base_url="https://api.x.ai:443/v1",
        openai_base_url="https://api.openai.com:443/v1",
    )
    assert settings.anthropic_base_url == "https://api.anthropic.com:443"
    assert settings.xai_base_url == "https://api.x.ai:443/v1"
    assert settings.openai_base_url == "https://api.openai.com:443/v1"


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
        malformed = tc.post(
            "/api/scan",
            json={},
            headers={"Origin": "http://testserver:not-a-port"},
        )
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"].lower()
    assert malformed.status_code == 403
    assert "cross-origin" in malformed.json()["detail"].lower()


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
        # Same host and effective port, but a different scheme is still a
        # different browser origin and must be blocked.
        r4 = tc.post(
            "/api/scan", json={}, headers={"Origin": "https://testserver:80"}
        )
    assert r1.status_code != 403 or "cross-origin" not in r1.json().get("detail", "").lower()
    assert r2.status_code != 403 or "cross-origin" not in r2.json().get("detail", "").lower()
    assert r3.status_code == 403 and "cross-origin" in r3.json()["detail"].lower()
    assert r4.status_code == 403 and "cross-origin" in r4.json()["detail"].lower()


@pytest.mark.asyncio
async def test_position_management_api_is_private_off_loopback(monkeypatch):
    from types import SimpleNamespace

    import app.security as security

    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(local_api_token="", trading_enabled=False),
    )
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/positions/alerts",
            "raw_path": b"/api/positions/alerts",
            "query_string": b"",
            "headers": [],
            "client": ("192.0.2.10", 12345),
            "server": ("localhost", 8787),
        }
    )
    call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

    response = await security.loopback_or_token_middleware(request, call_next)

    assert response.status_code == 403
    call_next.assert_not_awaited()


@pytest.mark.parametrize("client", [None, ("127.attacker", 12345)])
@pytest.mark.asyncio
async def test_private_api_fails_closed_for_unknown_or_non_ip_client(
    monkeypatch, client
):
    from types import SimpleNamespace

    import app.security as security

    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(local_api_token="", trading_enabled=False),
    )
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/account",
            "raw_path": b"/api/account",
            "query_string": b"",
            "headers": [],
            "client": client,
            "server": ("localhost", 8787),
        }
    )
    call_next = AsyncMock(return_value=JSONResponse({"ok": True}))

    response = await security.loopback_or_token_middleware(request, call_next)

    assert response.status_code == 403
    call_next.assert_not_awaited()


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


@pytest.mark.parametrize(
    ("provider", "key_field", "ready_property"),
    [
        ("claude", "anthropic_api_key", "claude_ready"),
        ("xai", "xai_api_key", "xai_ready"),
        ("openai", "openai_api_key", "openai_ready"),
    ],
)
def test_cloud_llm_whitespace_api_key_is_not_ready(
    provider, key_field, ready_property
):
    settings = Settings(
        _env_file=None,
        llm_provider=provider,
        **{key_field: " \t "},
    )

    assert getattr(settings, ready_property) is False
    assert settings.llm_ready is False


def test_resolved_llm_provider_skips_whitespace_key():
    settings = Settings(
        _env_file=None,
        llm_provider="claude",
        anthropic_api_key="",
        xai_api_key=" \t ",
        openai_api_key="synthetic-openai-key",
    )

    assert settings.resolved_llm_provider == "openai"


@pytest.mark.parametrize(
    ("provider", "key_field", "model_field", "ready_property"),
    [
        ("claude", "anthropic_api_key", "anthropic_model", "claude_ready"),
        ("xai", "xai_api_key", "xai_model", "xai_ready"),
        ("openai", "openai_api_key", "openai_model", "openai_ready"),
    ],
)
def test_cloud_llm_whitespace_model_is_not_ready(
    provider, key_field, model_field, ready_property
):
    settings = Settings(
        _env_file=None,
        llm_provider=provider,
        **{key_field: "synthetic-key", model_field: " \t "},
    )

    assert getattr(settings, model_field) == ""
    assert getattr(settings, ready_property) is False
    assert settings.llm_ready is False


def test_ollama_whitespace_model_is_not_ready():
    settings = Settings(
        _env_file=None,
        llm_provider="ollama",
        ollama_model=" \t ",
    )

    assert settings.ollama_model == ""
    assert settings.ollama_ready is False
    assert settings.llm_ready is False


def test_resolved_llm_provider_skips_key_with_whitespace_model():
    settings = Settings(
        _env_file=None,
        llm_provider="claude",
        anthropic_api_key="synthetic-claude-key",
        anthropic_model=" \t ",
        xai_api_key="synthetic-xai-key",
    )

    assert settings.resolved_llm_provider == "xai"


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


def test_mainnet_armed_without_ack_fails_closed(monkeypatch):
    """W3-01: armed on Hyperliquid MAINNET without MAINNET_ACK must fail-closed
    at startup (silent testnet→echtgeld switch is the dangerous moment)."""
    import app.main as main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            trading_enabled=True,
            exchange="hyperliquid",
            hl_testnet=False,
            local_api_token="test-token",
            mainnet_ack=False,
        ),
    )
    with pytest.raises(RuntimeError, match="MAINNET_ACK"):
        with TestClient(main.app):
            pass


def test_mainnet_with_ack_starts(monkeypatch):
    """W3-01: the same armed-mainnet config starts once MAINNET_ACK=true, and
    health then reports live_trading=True (drives the red MAINNET chip)."""
    import app.main as main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            trading_enabled=True,
            exchange="hyperliquid",
            hl_testnet=False,
            local_api_token="test-token",
            mainnet_ack=True,
        ),
    )
    # Instanz-Lock neutralisieren: dieser Test prueft den MAINNET-Gate + Chip,
    # NICHT die Single-Instance-Mechanik (das tut test_file_lock_detects_
    # second_instance). Der echte Lock liegt im geteilten tmp-DB-Verzeichnis
    # und wuerde sonst mit jedem parallelen pytest-Lauf kollidieren.
    monkeypatch.setattr(main, "_acquire_instance_lock", lambda data_dir: data_dir)
    monkeypatch.setattr(main, "_release_instance_lock", lambda lock_path: None)
    with TestClient(main.app) as tc:
        r = tc.get("/api/health")
    assert r.status_code == 200
    assert r.json()["live_trading"] is True


def test_windows_env_acl_removes_explicit_grants_and_handles_literal_paths(tmp_path, monkeypatch):
    import os
    import subprocess

    from app.env_builder import restrict_env_permissions

    if os.name != "nt":
        pytest.skip("Windows ACL integration")
    p = tmp_path / "synthetic [owner's] $value.env"
    p.write_text("synthetic configuration\n", encoding="utf-8")
    subprocess.run(
        ["icacls", str(p), "/grant", "*S-1-1-0:F"],
        capture_output=True, check=True, timeout=10,
    )
    # A forged display name must not change which actual identity receives access.
    monkeypatch.setenv("USERNAME", "nonexistent-display-name")
    monkeypatch.setenv("USERDOMAIN", "nonexistent-domain")
    # Exercise the writer without autoloadable PowerShell modules, as on
    # runners launched from another PowerShell version.
    run = subprocess.run

    def without_modules(args, **kwargs):
        args = [*args]
        args[-1] = "$PSModuleAutoLoadingPreference = 'None'; " + args[-1]
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", without_modules)
    restrict_env_permissions(p)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$acl = [System.IO.File]::GetAccessControl($env:TEST_ACL_PATH); "
        "$rules = @($acl.GetAccessRules($true, $true, "
        "[System.Security.Principal.SecurityIdentifier])); "
        "$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
        "[Console]::WriteLine((@($rules.Count, $acl.AreAccessRulesProtected, "
        "($rules.Count -eq 1 -and $rules[0].IdentityReference -eq $sid), "
        "($rules[0].FileSystemRights -eq 'FullControl'), "
        "($rules[0].AccessControlType -eq 'Allow')) -join ','))"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "TEST_ACL_PATH": str(p)},
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1,True,True,True,True"
    assert p.read_text(encoding="utf-8") == "synthetic configuration\n"


def test_windows_env_acl_failure_is_reported_without_raw_output(tmp_path, monkeypatch, caplog):
    import os
    from types import SimpleNamespace

    import app.env_builder as builder

    if os.name != "nt":
        pytest.skip("Windows ACL failure path")
    p = tmp_path / "synthetic.env"
    p.write_text("synthetic configuration\n", encoding="utf-8")
    monkeypatch.setattr(
        builder.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr=b"untrusted-output"),
    )
    builder.restrict_env_permissions(p)
    assert "Could not harden permissions" in caplog.text
    assert "rc=1" in caplog.text
    assert "untrusted-output" not in caplog.text
    assert p.read_text(encoding="utf-8") == "synthetic configuration\n"


def test_env_file_written_restrictive_perms(tmp_path):
    """B-08: .env carries exchange API secrets and the local auth token, so it
    must not be group-/world-readable after an atomic write. POSIX: chmod
    0600 (owner-only). Windows has no POSIX mode bits; the ACL-hardening
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
        # hardening ran is that the replacement ACL stripped those
        # inherited ACEs, leaving only the current user's grant.
        assert "nt authority\\system" not in low
        assert "builtin\\administrators" not in low
    else:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600


def test_patch_env_hardens_tmp_before_replace(tmp_path, monkeypatch):
    """B3-02: restrict_env_permissions must run on the tmp file BEFORE
    os.replace swaps it onto the real .env — otherwise there is a window
    where the freshly-written .env briefly carries broad inherited ACLs
    while already holding secrets."""
    import os as _os

    import app.env_builder as env_builder

    p = tmp_path / ".env"
    p.write_text("TRADING_ENABLED=false\nXAI_API_KEY=\n", encoding="utf-8")

    calls: list[str] = []
    real_restrict = env_builder.restrict_env_permissions
    real_replace = _os.replace

    def spy_restrict(path):
        calls.append("restrict")
        return real_restrict(path)

    def spy_replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(env_builder, "restrict_env_permissions", spy_restrict)
    monkeypatch.setattr(env_builder.os, "replace", spy_replace)

    from app.env_builder import SETTINGS_LLM_WRITABLE, patch_env_vars

    patch_env_vars(p, {"XAI_API_KEY": "secret-value"}, allowed=set(SETTINGS_LLM_WRITABLE))

    assert "restrict" in calls and "replace" in calls
    assert calls.index("restrict") < calls.index("replace"), (
        "hardening must happen before the atomic replace, not after"
    )


def test_require_local_token_fails_closed_when_armed_without_token(monkeypatch):
    """B (audit, LOW): TRADING_ENABLED=true + empty LOCAL_API_TOKEN must be
    denied AT THE PER-REQUEST AUTH GATE too, not only prevented at Settings
    construction (armed_requires_local_token). Defense in depth: the
    real-world combo is already impossible to construct via env-based
    Settings(), but require_local_token itself must not silently fail open if
    that invariant is ever weakened elsewhere — armed + unauthenticated must
    stay closed at every layer, loudly."""
    import app.security as security

    class _FakeSettings:
        trading_enabled = True
        local_api_token = ""

    monkeypatch.setattr(security, "get_settings", lambda: _FakeSettings())
    with pytest.raises(HTTPException) as exc:
        security.require_local_token(x_local_token=None, local_auth=None)
    assert exc.value.status_code in (401, 403)


def test_require_local_token_still_open_when_disarmed_without_token(monkeypatch):
    """Disarmed (TRADING_ENABLED=false) + no token configured is the intended
    analysis-only UX (no real money at risk) and must keep working exactly as
    before — the fail-closed fix must not regress the default dev/test
    setup that every other test in this suite relies on."""
    import app.security as security

    class _FakeSettings:
        trading_enabled = False
        local_api_token = ""

    monkeypatch.setattr(security, "get_settings", lambda: _FakeSettings())
    # Must NOT raise
    security.require_local_token(x_local_token=None, local_auth=None)


def test_env_tmp_uses_mkstemp_not_fixed_name(tmp_path, monkeypatch):
    """B3-04: a fixed tmp filename (``.env.tmp``) lets a local process
    pre-create or symlink that path before the write lands. The tmp file
    must come from tempfile.mkstemp (O_EXCL, unpredictable name) in the
    same directory as the real .env, so os.replace stays atomic."""
    import os as _os
    from pathlib import Path

    import app.env_builder as env_builder

    p = tmp_path / ".env"
    p.write_text("TRADING_ENABLED=false\nXAI_API_KEY=\n", encoding="utf-8")

    seen: list[Path] = []
    real_replace = _os.replace

    def spy_replace(src, dst):
        seen.append(Path(src))
        return real_replace(src, dst)

    monkeypatch.setattr(env_builder.os, "replace", spy_replace)

    from app.env_builder import SETTINGS_LLM_WRITABLE, patch_env_vars

    patch_env_vars(p, {"XAI_API_KEY": "s1"}, allowed=set(SETTINGS_LLM_WRITABLE))
    patch_env_vars(p, {"XAI_API_KEY": "s2"}, allowed=set(SETTINGS_LLM_WRITABLE))

    assert len(seen) == 2
    assert seen[0].name != ".env.tmp", "must not use the old fixed tmp name"
    assert seen[0].parent == p.parent, "tmp file must live next to .env (atomic replace)"
    assert seen[0].name != seen[1].name, "tmp name must be unpredictable, not reused"
