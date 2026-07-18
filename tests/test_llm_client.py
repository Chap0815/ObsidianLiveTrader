"""Observability for LLM provider calls (Task 11 / findings O2-02, L2X-03,
O2-08, O2-04).

Covers:
- Every provider call emits exactly one `LLM_METRICS` INFO log line with
  latency + usage/cache/finish_reason, read defensively from either the
  OpenAI-shaped (xai/openai/ollama) or Anthropic-shaped (claude) response.
- The salvage branch of parse_proposal/parse_reevaluation logs a WARNING
  when a truncated response is recovered (O2-08).
"""

from __future__ import annotations

import json
import logging

import pytest

import app.llm.client as client_mod
from app.config import Settings
from app.llm.client import _call_xai, parse_proposal, parse_reevaluation


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in returning a fixed response."""

    def __init__(self, status_code, payload):
        self._status_code = status_code
        self._payload = payload

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        return _FakeResp(self._status_code, self._payload)

    async def aclose(self):
        return None


def _settings(**kw):
    base = dict(anthropic_api_key="k", xai_api_key="k", include_account_in_llm=False)
    base.update(kw)
    return Settings(**base)


@pytest.mark.asyncio
async def test_xai_call_logs_usage_and_reasoning_tokens(monkeypatch, caplog):
    """The xAI (Grok) call site logs one LLM_METRICS line carrying the
    OpenAI-shaped usage fields: prompt/completion tokens, the nested
    reasoning_tokens (completion_tokens_details) and cached_tokens
    (prompt_tokens_details), plus finish_reason."""
    xai_payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"htf_trend": "bullish", "ltf_trend": "bullish", "action": "STAY_OUT"}
                    )
                },
            }
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 150},
            "prompt_tokens_details": {"cached_tokens": 400},
        },
    }
    fake = _FakeClient(200, xai_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    with caplog.at_level(logging.INFO, logger="app.llm.client"):
        result = await _call_xai({"symbol": "BTC"}, _settings())

    assert result.action == "STAY_OUT"
    metrics_records = [r for r in caplog.records if "LLM_METRICS" in r.getMessage()]
    assert len(metrics_records) == 1
    msg = metrics_records[0].getMessage()
    assert "xAI" in msg
    assert "reasoning_tokens=150" in msg
    assert "cached_tokens=400" in msg
    assert "prompt_tokens=1200" in msg
    assert "completion_tokens=300" in msg
    assert "finish_reason=stop" in msg


def test_salvage_path_emits_warning(caplog):
    """O2-08: recovering a truncated proposal via salvage must log a WARNING
    so a silently-truncated response becomes visible/correlatable."""
    full = json.dumps(
        {
            "htf_trend": "bullish",
            "ltf_trend": "bullish",
            "key_levels": {},
            "action": "BUY",
            "entry_price": 65100.0,
            "stop_loss": 64100.0,
            "tp1": 67000.0,
            "rationale": "some text",
        }
    )
    cut_idx = full.index('"rationale"')
    truncated = full[:cut_idx] + '"rationale": "HTF uptrend pullback ho'

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        p = parse_proposal(truncated)

    assert p.action == "BUY"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("recovered via salvage" in r.getMessage() for r in warnings)
    assert any("truncated" in r.getMessage() for r in warnings)


def test_reevaluation_salvage_path_emits_warning(caplog):
    """Same O2-08 coverage for the reevaluate parser."""
    full = json.dumps(
        {
            "confidence": "medium",
            "action": "HOLD",
            "reason": "position still valid given structure",
        }
    )
    cut_idx = full.index('"reason"')
    truncated = full[:cut_idx] + '"reason": "position still val'

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        parse_reevaluation(truncated)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("recovered via salvage" in r.getMessage() for r in warnings)
