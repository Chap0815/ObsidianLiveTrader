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
