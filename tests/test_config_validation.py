"""Config validators: rate-budget + scanner-int guards (2026-07-20 hardening)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.config import Settings


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
