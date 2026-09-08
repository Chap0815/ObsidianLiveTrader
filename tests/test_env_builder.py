"""Shared .env builder: normalize, build_minimal/full, atomic whitelist patch."""

import os

import pytest

import app.env_builder as env_builder
from app.config import Settings
from app.env_builder import (
    DEFAULT_MODELS,
    SETTINGS_LLM_WRITABLE,
    build_full_env,
    build_minimal_env,
    normalize_answers,
    patch_env_vars,
    sanitize_env_value,
)


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


# ── sanitize ────────────────────────────────────────────────────────────────
def test_sanitize_rejects_control_chars():
    with pytest.raises(ValueError):
        sanitize_env_value("X", "0x" + "a" * 64 + "\nTRADING_ENABLED=true")
    assert sanitize_env_value("X", "  ok  ") == "ok"
    assert sanitize_env_value("X", None) == ""


# ── normalize ────────────────────────────────────────────────────────────────
def test_normalize_rejects_bad_hl_key():
    with pytest.raises(ValueError):
        normalize_answers(_payload(hl_private_key=""))
    with pytest.raises(ValueError):
        normalize_answers(_payload(hl_private_key="kein-hex"))
    with pytest.raises(ValueError):
        normalize_answers(_payload(hl_private_key="0xabc"))


def test_normalize_mainnet_requires_typed_confirm():
    with pytest.raises(ValueError):
        normalize_answers(_payload(exchange="hl-mainnet"))
    ok = normalize_answers(_payload(exchange="hl-mainnet", mainnet_confirm="MAINNET"))
    assert ok["exchange"] == "hl-mainnet"


def test_normalize_mexc_requires_both_keys():
    with pytest.raises(ValueError):
        normalize_answers(
            _payload(exchange="mexc", hl_private_key="", mexc_api_key="", mexc_api_secret="")
        )
    ok = normalize_answers(
        _payload(exchange="mexc", hl_private_key="", mexc_api_key="k", mexc_api_secret="s")
    )
    assert ok["is_mexc"] is True


def test_normalize_cloud_provider_needs_key():
    with pytest.raises(ValueError):
        normalize_answers(_payload(llm_provider="xai", llm_api_key=""))
    assert normalize_answers(_payload(llm_provider="ollama", llm_api_key=""))["llm_provider"] == "ollama"
    assert normalize_answers(_payload(llm_provider="none", llm_api_key=""))["llm_provider"] == "none"


def test_normalize_port_and_host_bounds():
    with pytest.raises(ValueError):
        normalize_answers(_payload(port=80))
    with pytest.raises(ValueError, match="PORT must be between"):
        normalize_answers(_payload(port=0))
    with pytest.raises(ValueError):
        normalize_answers(_payload(host="10.0.0.5"))
    assert normalize_answers(_payload(port=9999))["port"] == 9999


@pytest.mark.parametrize("value", [False, True])
def test_normalize_rejects_boolean_port(value):
    with pytest.raises(ValueError, match="PORT is not a number"):
        normalize_answers(_payload(port=value))


@pytest.mark.parametrize("value", [None, "", 8787.5])
def test_normalize_rejects_explicit_non_integer_port(value):
    with pytest.raises(ValueError, match="PORT is not a number"):
        normalize_answers(_payload(port=value))


def test_normalize_custom_risk_range_checked():
    with pytest.raises(ValueError):
        normalize_answers(_payload(risk_profile="custom", max_risk_pct=99))
    a = normalize_answers(
        _payload(
            risk_profile="custom",
            max_risk_pct=2.0,
            max_leverage=10,
            min_rrr=2.0,
            max_notional_pct_of_equity=1000,
        )
    )
    assert a["max_risk_pct"] == 2.0


def test_normalize_rejects_fractional_max_leverage():
    with pytest.raises(ValueError, match="MAX_LEVERAGE must be an integer"):
        normalize_answers(
            _payload(risk_profile="custom", max_leverage=10.9)
        )


@pytest.mark.parametrize(
    "field",
    [
        "max_risk_pct",
        "max_leverage",
        "min_rrr",
        "max_notional_pct_of_equity",
        "max_notional_usdt",
    ],
)
@pytest.mark.parametrize("value", [False, True])
def test_normalize_rejects_boolean_risk_numbers(field, value):
    payload = _payload(
        risk_profile="custom",
        max_risk_pct=2.0,
        max_leverage=10,
        min_rrr=2.0,
        max_notional_pct_of_equity=1000,
        max_notional_usdt=500,
    )
    payload[field] = value

    with pytest.raises(ValueError, match="is not a number"):
        normalize_answers(payload)


def test_normalize_include_account_defaults_false():
    assert normalize_answers(_payload())["include_account_in_llm"] is False


def test_normalize_include_account_requires_explicit_boolean():
    assert (
        normalize_answers(_payload(include_account_in_llm=True))[
            "include_account_in_llm"
        ]
        is True
    )
    for invalid in ("false", "true", 0, 1, None, [], {}):
        with pytest.raises(ValueError, match="Account-data.*boolean"):
            normalize_answers(_payload(include_account_in_llm=invalid))


# ── build_minimal_env ────────────────────────────────────────────────────────
def test_build_minimal_env_safe_and_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_minimal_env(port=8787)
    assert "SETUP_COMPLETE=false" in content
    assert "PORT=8787" in content
    assert "TRADING_ENABLED=false" in content
    assert "ALLOW_MANUAL_TRIGGER=false" in content
    assert "INCLUDE_ACCOUNT_IN_LLM=false" in content
    assert "LOCAL_API_TOKEN=" in content and "LOCAL_API_TOKEN=\n" not in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.setup_complete is False
    assert s.trading_enabled is False
    assert s.exchange_ready is False


def test_build_minimal_env_rejects_bad_port_falls_back():
    assert "PORT=8787" in build_minimal_env(port=80)
    assert "PORT=8787" in build_minimal_env(port="nope")  # type: ignore[arg-type]


# ── build_full_env ───────────────────────────────────────────────────────────
def test_build_full_env_port_is_answer_port_not_8788(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_full_env(normalize_answers(_payload(port=8787)))
    assert "PORT=8787" in content
    assert "PORT=8788" not in content
    assert "SETUP_COMPLETE=true" in content
    assert "ANTHROPIC_API_KEY=sk-ant-test" in content
    assert "ANTHROPIC_MODEL=claude-sonnet-5" in content
    assert "ALLOW_MANUAL_TRIGGER=false" in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.port == 8787
    assert s.setup_complete is True
    assert s.trading_enabled is False


def test_build_full_env_preset_omits_max_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_full_env(normalize_answers(_payload(risk_profile="conservative")))
    assert "RISK_PROFILE=conservative" in content
    # preset must NOT hardcode the limits — _apply_risk_profile fills them
    assert "\nMAX_RISK_PCT=" not in content
    assert "\nMAX_LEVERAGE=" not in content
    assert "\nMAX_NOTIONAL_PCT_OF_EQUITY=" not in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    # conservative preset applied
    assert s.max_risk_pct == 1.0
    assert s.max_leverage == 20
    assert s.min_rrr == 2.0


def test_build_full_env_custom_writes_raw_limits(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_full_env(
        normalize_answers(
            _payload(
                risk_profile="custom",
                max_risk_pct=3.0,
                max_leverage=15,
                min_rrr=1.5,
                max_notional_pct_of_equity=2000,
            )
        )
    )
    assert "MAX_RISK_PCT=3.0" in content
    assert "MAX_LEVERAGE=15" in content
    assert "MAX_NOTIONAL_PCT_OF_EQUITY=2000" in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.max_risk_pct == 3.0
    assert s.max_leverage == 15
    assert s.max_notional_pct_of_equity == 2000.0


def test_build_full_env_mexc_default_symbol(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    content = build_full_env(
        normalize_answers(
            _payload(exchange="mexc", hl_private_key="", mexc_api_key="k", mexc_api_secret="s")
        )
    )
    assert "EXCHANGE=mexc" in content
    assert "MEXC_BASE_URL=https://api.mexc.com" in content
    assert "contract.mexc.com" not in content
    assert "DEFAULT_SYMBOL=BTC_USDT" in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    assert Settings(_env_file=str(p)).exchange == "mexc"


def test_build_full_env_chosen_provider_only_gets_key():
    content = build_full_env(normalize_answers(_payload(llm_provider="xai", llm_api_key="xai-k")))
    assert "XAI_API_KEY=xai-k" in content
    assert "ANTHROPIC_API_KEY=\n" in content  # other providers empty
    assert "LLM_PROVIDER=xai" in content


# ── patch_env_vars ───────────────────────────────────────────────────────────
def test_patch_env_updates_in_place_and_preserves(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\nTRADING_ENABLED=false\nXAI_API_KEY=\nXAI_MODEL=grok-4\n",
        encoding="utf-8",
    )
    patch_env_vars(p, {"XAI_API_KEY": "xai-new"}, allowed=set(SETTINGS_LLM_WRITABLE))
    out = p.read_text(encoding="utf-8")
    assert "XAI_API_KEY=xai-new" in out
    assert "TRADING_ENABLED=false" in out  # untouched
    assert "# comment" in out
    assert out.count("XAI_API_KEY=") == 1  # updated in place, not appended


def test_patch_env_appends_under_footer_when_missing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("TRADING_ENABLED=false\n", encoding="utf-8")
    patch_env_vars(p, {"OPENAI_API_KEY": "sk-o"}, allowed=set(SETTINGS_LLM_WRITABLE))
    out = p.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-o" in out
    assert "updated by settings" in out


def test_patch_env_rejects_non_whitelisted_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("TRADING_ENABLED=false\n", encoding="utf-8")
    with pytest.raises(ValueError):
        patch_env_vars(p, {"TRADING_ENABLED": "true"}, allowed=set(SETTINGS_LLM_WRITABLE))
    assert "TRADING_ENABLED=false" in p.read_text(encoding="utf-8")


def test_patch_env_rejects_control_char_value(tmp_path):
    p = tmp_path / ".env"
    p.write_text("XAI_API_KEY=\n", encoding="utf-8")
    with pytest.raises(ValueError):
        patch_env_vars(
            p, {"XAI_API_KEY": "x\nTRADING_ENABLED=true"}, allowed=set(SETTINGS_LLM_WRITABLE)
        )


def test_default_models_have_all_providers():
    for prov in ("claude", "xai", "openai", "ollama"):
        assert DEFAULT_MODELS[prov]


# ── Finding 3: os.replace retry on a transient Windows file lock ───────────
def test_patch_env_retries_transient_permission_error_then_succeeds(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("XAI_API_KEY=\n", encoding="utf-8")
    real_replace = os.replace
    calls: list[int] = []

    def flaky_replace(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("WinError 5: Zugriff verweigert")
        return real_replace(src, dst)

    monkeypatch.setattr(env_builder.os, "replace", flaky_replace)
    monkeypatch.setattr(env_builder.time, "sleep", lambda s: None)  # no real delay in tests

    patch_env_vars(p, {"XAI_API_KEY": "xai-new"}, allowed=set(SETTINGS_LLM_WRITABLE))

    assert len(calls) == 3
    assert "XAI_API_KEY=xai-new" in p.read_text(encoding="utf-8")


def test_patch_env_gives_up_after_persistent_permission_error(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("XAI_API_KEY=\n", encoding="utf-8")

    def always_fail(src, dst):
        raise PermissionError("WinError 5: Zugriff verweigert")

    monkeypatch.setattr(env_builder.os, "replace", always_fail)
    monkeypatch.setattr(env_builder.time, "sleep", lambda s: None)

    with pytest.raises(PermissionError):
        patch_env_vars(p, {"XAI_API_KEY": "xai-new"}, allowed=set(SETTINGS_LLM_WRITABLE))

    # fail-safe: the original .env must remain untouched, and the tmp file
    # must not be left behind.
    assert p.read_text(encoding="utf-8") == "XAI_API_KEY=\n"
    assert list(tmp_path.glob(".env.*.tmp")) == []
