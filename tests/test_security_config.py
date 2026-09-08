"""Security / config remaining audit fixes."""

import pytest
from pydantic import ValidationError

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


def test_tm_settings_defaults():
    """Trade-Management-Layer defaults are safe-by-default and readable."""
    s = Settings()
    assert s.tm_enabled is True
    assert s.tm_monitor_interval_s == 20
    assert s.tm_be_trigger_r == 1.0
    assert s.tm_be_fee_rt == 0.0006
    assert s.tm_time_stop_hours == 4.0
    assert s.tm_time_stop_min_r == 0.5


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
    import json
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
    restrict_env_permissions(p)
    script = (
        "$acl = Get-Acl -LiteralPath $env:TEST_ACL_PATH; "
        "$rules = @($acl.GetAccessRules($true, $true, "
        "[System.Security.Principal.SecurityIdentifier])); "
        "$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
        "@{count=$rules.Count; protected=$acl.AreAccessRulesProtected; "
        "currentOnly=($rules.Count -eq 1 -and $rules[0].IdentityReference -eq $sid); "
        "fullControl=($rules[0].FileSystemRights -eq 'FullControl'); "
        "allow=($rules[0].AccessControlType -eq 'Allow')} | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "TEST_ACL_PATH": str(p)},
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert json.loads(result.stdout) == {
        "count": 1, "protected": True, "currentOnly": True,
        "fullControl": True, "allow": True,
    }
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
