"""Config validators: rate-budget + scanner-int guards (2026-07-20 hardening)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.config import Settings


_NUMERIC_SETTINGS_FIELDS = sorted(
    name
    for name, field in Settings.model_fields.items()
    if field.annotation in {int, float}
)
_BOOLEAN_SETTINGS_FIELDS = sorted(
    name for name, field in Settings.model_fields.items() if field.annotation is bool
)


@pytest.mark.parametrize("field", _NUMERIC_SETTINGS_FIELDS)
@pytest.mark.parametrize("value", [False, True])
def test_numeric_settings_reject_python_booleans(field, value):
    with pytest.raises(ValidationError, match="boolean is not a numeric setting"):
        Settings(_env_file=None, **{field: value})


def test_numeric_settings_keep_accepting_env_style_numeric_strings():
    settings = Settings(
        _env_file=None,
        max_leverage="25",
        max_risk_pct="2.5",
        hl_http_timeout_s="7.5",
    )

    assert settings.max_leverage == 25
    assert settings.max_risk_pct == pytest.approx(2.5)
    assert settings.hl_http_timeout_s == pytest.approx(7.5)


@pytest.mark.parametrize("field", _BOOLEAN_SETTINGS_FIELDS)
@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0])
def test_boolean_settings_reject_numeric_values(field, value):
    with pytest.raises(ValidationError, match="numeric value is not a boolean setting"):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", _BOOLEAN_SETTINGS_FIELDS)
@pytest.mark.parametrize("value", ["0", "1", "no", "yes", "off", "on"])
def test_boolean_settings_reject_ambiguous_env_strings(field, value):
    with pytest.raises(ValidationError, match="must use true or false"):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("true", True), ("FALSE", False), ("TRUE", True)],
)
def test_boolean_settings_keep_accepting_env_style_strings(value, expected):
    assert Settings(_env_file=None, journal_enabled=value).journal_enabled is expected


def test_stage1_k_default_is_40():
    assert Settings().scanner_prefilter_stage1_k == 40


def test_defaults_construct_cleanly():
    # The new validators must not break bare default construction.
    s = Settings()
    assert s.hl_read_max_rps == 10.0
    assert s.hl_read_burst == 20.0


def test_negative_read_rps_rejected():
    with pytest.raises(ValidationError):
        Settings(hl_read_max_rps=-1.0)


def test_zero_read_rps_allowed_disables_limiter():
    # 0 is the documented "off" switch — must stay valid.
    assert Settings(hl_read_max_rps=0.0).hl_read_max_rps == 0.0


def test_inf_read_rps_rejected():
    # inf would pass a bare v<0 check and silently disable the limiter
    # (max(1e-6, inf) == inf → never throttle). Must be rejected.
    with pytest.raises(ValidationError):
        Settings(hl_read_max_rps=math.inf)


def test_burst_below_one_rejected():
    with pytest.raises(ValidationError):
        Settings(hl_read_burst=0.5)


def test_inf_burst_rejected():
    with pytest.raises(ValidationError):
        Settings(hl_read_burst=math.inf)


def test_stage1_k_below_one_rejected():
    with pytest.raises(ValidationError):
        Settings(scanner_prefilter_stage1_k=0)


@pytest.mark.parametrize("value", [0, 501])
def test_scanner_max_coins_outside_bounded_fanout_rejected(value):
    with pytest.raises(ValidationError, match="SCANNER_MAX_COINS"):
        Settings(_env_file=None, scanner_max_coins=value)


@pytest.mark.parametrize("value", [1, 500])
def test_scanner_max_coins_safe_boundaries_allowed(value):
    assert Settings(_env_file=None, scanner_max_coins=value).scanner_max_coins == value


# --- Finding 1: close_verify_delay_s (2026-07-21 hardening) ---------------


def test_close_verify_delay_s_default_is_zero():
    assert Settings().close_verify_delay_s == 0.0


def test_close_verify_delay_s_inf_rejected():
    # inf would pass max(0.0, float(x or 0.0)) unchanged and hang the
    # close-verify asyncio.sleep() indefinitely (app/orders/service.py).
    with pytest.raises(ValidationError):
        Settings(close_verify_delay_s=math.inf)


def test_close_verify_delay_s_above_range_rejected():
    with pytest.raises(ValidationError):
        Settings(close_verify_delay_s=61.0)


def test_close_verify_delay_s_negative_rejected():
    with pytest.raises(ValidationError):
        Settings(close_verify_delay_s=-1.0)


def test_close_verify_delay_s_in_range_allowed():
    assert Settings(close_verify_delay_s=5.0).close_verify_delay_s == 5.0


@pytest.mark.parametrize("field", ["sl_verify_attempts", "close_verify_attempts"])
@pytest.mark.parametrize("value", [0, 11])
def test_exchange_verify_attempts_outside_safe_range_rejected(field, value):
    with pytest.raises(ValidationError, match=r"must be an integer in \[1, 10\]"):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", ["sl_verify_attempts", "close_verify_attempts"])
@pytest.mark.parametrize("value", [1, 10])
def test_exchange_verify_attempts_safe_boundaries_allowed(field, value):
    assert getattr(Settings(_env_file=None, **{field: value}), field) == value


# --- Finding 2: kline_limit_hint (2026-07-21 hardening) --------------------


def test_kline_limit_hint_default_is_500():
    assert Settings().kline_limit_hint == 500


def test_kline_limit_hint_below_min_rejected():
    # 0/negative silently falls back to only 10 candles for ATR/HTF analysis
    # (app/hyperliquid/client.py: max(int(limit_hint), 10)).
    with pytest.raises(ValidationError):
        Settings(kline_limit_hint=0)


def test_kline_limit_hint_above_max_rejected():
    # Unbounded above puts 429 pressure on the exchange API.
    with pytest.raises(ValidationError):
        Settings(kline_limit_hint=100_000)


def test_kline_limit_hint_in_range_allowed():
    assert Settings(kline_limit_hint=200).kline_limit_hint == 200


# --- Finding 3: preview_token_ttl_seconds (2026-07-21 hardening) -----------


def test_preview_token_ttl_seconds_default_is_60():
    assert Settings().preview_token_ttl_seconds == 60


def test_preview_token_ttl_seconds_zero_rejected():
    # 0/negative diverges from tokens.py's max(1, int(ttl)) clamp, giving
    # the in-memory token and the DB-preview row different expiries.
    with pytest.raises(ValidationError):
        Settings(preview_token_ttl_seconds=0)


def test_preview_token_ttl_seconds_negative_rejected():
    with pytest.raises(ValidationError):
        Settings(preview_token_ttl_seconds=-5)


def test_preview_token_ttl_seconds_above_max_rejected():
    with pytest.raises(ValidationError):
        Settings(preview_token_ttl_seconds=3601)


def test_preview_token_ttl_seconds_in_range_allowed():
    assert Settings(preview_token_ttl_seconds=120).preview_token_ttl_seconds == 120


# --- Journal resolver and calibration bounds --------------------------------


def test_journal_settings_defaults_construct_cleanly():
    settings = Settings(_env_file=None)
    assert settings.journal_resolve_interval_s == 60
    assert settings.journal_window_hours == 24
    assert settings.journal_min_sample == 20


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("journal_resolve_interval_s", 4),
        ("journal_resolve_interval_s", 3601),
        ("journal_window_hours", 0),
        ("journal_window_hours", 8761),
        ("journal_min_sample", 0),
        ("journal_min_sample", 1001),
    ],
)
def test_journal_settings_reject_out_of_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_journal_settings_accept_documented_ranges():
    settings = Settings(
        _env_file=None,
        journal_resolve_interval_s=300,
        journal_window_hours=48,
        journal_min_sample=50,
    )
    assert settings.journal_resolve_interval_s == 300
    assert settings.journal_window_hours == 48
    assert settings.journal_min_sample == 50
