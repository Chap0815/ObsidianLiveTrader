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
import re
from unittest.mock import AsyncMock

import pytest

import app.llm.client as client_mod
from app.config import Settings
from app.llm.client import (
    LlmError,
    _call_claude_reevaluate,
    _call_xai,
    _call_xai_reevaluate,
    _log_llm_metrics,
    _strictify_schema,
    annotate_proposal,
    parse_proposal,
    parse_reevaluation,
    reevaluate_with_llm,
)
from app.models import ReevaluateProposal, TradeProposal


def _directional_proposal(recommended_leverage: str) -> TradeProposal:
    """A geometrically-valid BUY so annotate_proposal keeps it directional and
    only the leverage clamp is exercised."""
    return TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action="BUY",
        entry_price=100.0,
        stop_loss=98.0,
        tp1=106.0,
        rrr=3.0,
        recommended_leverage=recommended_leverage,
    )


def test_annotate_clamps_leverage_to_contract():
    """R2-02: a proposal recommending leverage ABOVE the per-coin
    contract.max_leverage is clamped down to min(risk_policy, contract) server
    side — the 'propose then block' at preview is gone. The policy cap (50) is
    higher than the coin cap (10), so the coin cap binds."""
    ctx = {
        "risk_policy": {"max_leverage": 50},
        "contract": {"max_leverage": 10},
    }
    out = annotate_proposal(_directional_proposal("20x isolated"), ctx)
    # No number in the string may exceed the coin cap …
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", out.recommended_leverage)]
    assert nums and max(nums) <= 10
    assert "10x" in out.recommended_leverage
    assert "20x" not in out.recommended_leverage

    # A range "5-25x" clamps only the offending upper bound; 5 is preserved.
    out2 = annotate_proposal(_directional_proposal("5-25x cross"), ctx)
    nums2 = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", out2.recommended_leverage)]
    assert max(nums2) <= 10 and 5.0 in nums2

    # Already within the cap -> untouched (no spurious note).
    out3 = annotate_proposal(_directional_proposal("5-8x isolated"), ctx)
    assert out3.recommended_leverage == "5-8x isolated"

    # Fallback: contract cap missing -> clamp to risk_policy.max_leverage only,
    # never crashes and never leaves it unclamped-upward.
    ctx_no_contract = {"risk_policy": {"max_leverage": 10}, "contract": {}}
    out4 = annotate_proposal(_directional_proposal("20x"), ctx_no_contract)
    nums4 = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", out4.recommended_leverage)]
    assert max(nums4) <= 10


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


class _CapturingClient:
    """httpx.AsyncClient stand-in that records the constructor kwargs (e.g.
    timeout) and every posted JSON body, returning a fixed response."""

    def __init__(self, status_code, payload):
        self._status_code = status_code
        self._payload = payload
        self.constructor_kwargs: list[dict] = []
        self.posted_bodies: list[dict] = []

    def __call__(self, *a, **kw):
        self.constructor_kwargs.append(kw)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.posted_bodies.append(json)
        return _FakeResp(self._status_code, self._payload)

    async def aclose(self):
        return None


class _SeqClient:
    """httpx.AsyncClient stand-in returning a queued sequence of responses,
    one per post() call — for exercising retry/fallback code paths."""

    def __init__(self, responses: list[tuple[int, dict]]):
        self._responses = list(responses)
        self.posted_bodies: list[dict] = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.posted_bodies.append(json)
        status, payload = self._responses.pop(0)
        return _FakeResp(status, payload)

    async def aclose(self):
        return None


def _settings(**kw):
    base = dict(anthropic_api_key="k", xai_api_key="k", include_account_in_llm=False)
    base.update(kw)
    return Settings(**base)


def test_provider_response_json_object_redacts_non_json_body():
    marker = "SYNTHETIC_SECRET_MARKER"

    class Response:
        text = marker

        @staticmethod
        def json():
            raise ValueError(marker)

    with pytest.raises(LlmError) as exc_info:
        client_mod._provider_response_json_object(
            Response(), provider_label="Synthetic"
        )

    assert str(exc_info.value) == "Synthetic returned invalid response"
    assert marker not in str(exc_info.value)
    assert exc_info.value.raw == marker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_name", "provider_label"),
    [
        ("claude_analyze", "Claude"),
        ("xai_analyze", "xAI"),
        ("openai_analyze", "OpenAI"),
        ("claude_reevaluate", "Claude"),
        ("xai_reevaluate", "xAI"),
        ("openai_reevaluate", "OpenAI"),
    ],
)
async def test_provider_transports_reject_non_object_success_payload(
    monkeypatch, transport_name, provider_label
):
    marker = "SYNTHETIC_SECRET_MARKER"
    fake = _FakeClient(200, [marker])
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)
    context = {"symbol": "BTC"}
    settings = _settings()

    calls = {
        "claude_analyze": lambda: client_mod._call_claude(context, settings),
        "xai_analyze": lambda: client_mod._call_xai(context, settings),
        "openai_analyze": lambda: client_mod._call_openai_compat(
            context,
            base_url="https://synthetic.invalid/v1",
            api_key="synthetic-key",
            model="synthetic-model",
            provider_label="OpenAI",
        ),
        "claude_reevaluate": lambda: client_mod._call_claude_reevaluate(
            context, settings
        ),
        "xai_reevaluate": lambda: client_mod._call_xai_reevaluate(context, settings),
        "openai_reevaluate": lambda: client_mod._call_openai_compat_reevaluate(
            context,
            base_url="https://synthetic.invalid/v1",
            api_key="synthetic-key",
            model="synthetic-model",
            provider_label="OpenAI",
        ),
    }

    with pytest.raises(LlmError) as exc_info:
        await calls[transport_name]()

    assert str(exc_info.value) == f"{provider_label} returned invalid response"
    assert marker not in str(exc_info.value)
    assert exc_info.value.raw == [marker]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "transport_name"),
    (
        ("claude", "_call_claude"),
        ("anthropic", "_call_claude"),
        ("xai", "_call_xai"),
        ("grok", "_call_xai"),
        ("openai", "_call_openai"),
        ("codex", "_call_openai"),
        ("ollama", "_call_ollama"),
        ("local", "_call_ollama"),
    ),
)
async def test_analyze_dispatch_strips_private_context_without_opt_in(
    monkeypatch, provider, transport_name
):
    expected = object()
    transport = AsyncMock(return_value=expected)
    monkeypatch.setattr(client_mod, transport_name, transport)
    context = {
        "symbol": "BTC_USDT",
        "market": {"open_interest": 123.0},
        "account": {
            "equity_usdt": 5000.0,
            "positions": [{"hold_vol": 1.0}],
        },
        "remaining_risk_budget_pct": 2.5,
        "position": {"unrealized_pnl": 100.0},
        "unexpected_private_block": {"wallet": "secret"},
    }

    result = await client_mod.analyze_with_llm(
        context,
        _settings(llm_provider=provider, include_account_in_llm=False),
    )

    assert result is expected
    sent = transport.await_args.args[0]
    assert sent["symbol"] == "BTC_USDT"
    assert sent["market"] == {"open_interest": 123.0}
    assert sent["account"] == {
        "omitted": True,
        "reason": "INCLUDE_ACCOUNT_IN_LLM=false",
    }
    assert "remaining_risk_budget_pct" not in sent
    assert "position" not in sent
    assert "unexpected_private_block" not in sent
    assert context["account"]["equity_usdt"] == 5000.0


@pytest.mark.asyncio
async def test_analyze_dispatch_recursively_strips_private_context_without_opt_in(
    monkeypatch,
):
    marker = "SYNTHETIC_NESTED_PRIVATE_MARKER"
    expected = object()
    transport = AsyncMock(return_value=expected)
    monkeypatch.setattr(client_mod, "_call_claude", transport)
    context = {
        "symbol": "BTC_USDT",
        "market": {
            "open_interest": 123.0,
            "nested": {
                "public_signal": "stable",
                "account": {"equity_usdt": marker},
                "position": {"hold_vol": marker},
                "private_diagnostic": marker,
                "api_key": marker,
            },
        },
        "risk_policy": {"max_notional_pct_of_equity": 25.0},
    }

    result = await client_mod.analyze_with_llm(
        context,
        _settings(llm_provider="claude", include_account_in_llm=False),
    )

    assert result is expected
    sent = transport.await_args.args[0]
    assert sent["market"] == {
        "open_interest": 123.0,
        "nested": {"public_signal": "stable"},
    }
    assert sent["risk_policy"] == {"max_notional_pct_of_equity": 25.0}
    assert marker not in str(sent)
    assert context["market"]["nested"]["account"]["equity_usdt"] == marker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_key",
    [
        "accountData",
        "user-account-balance",
        "positionData",
        "openPositions",
        "walletAddress",
        "apiKey",
        "accessToken",
        "privateKey",
        "authCredential",
        "sessionCookie",
        "userPassword",
    ],
)
async def test_analyze_dispatch_strips_private_key_variants_without_opt_in(
    monkeypatch, private_key
):
    marker = "SYNTHETIC_PRIVATE_KEY_VARIANT_MARKER"
    transport = AsyncMock(return_value=object())
    monkeypatch.setattr(client_mod, "_call_claude", transport)

    await client_mod.analyze_with_llm(
        {
            "symbol": "BTC_USDT",
            "market": {
                "nested": {"public_signal": "stable", private_key: marker}
            },
        },
        _settings(llm_provider="claude", include_account_in_llm=False),
    )

    sent = transport.await_args.args[0]
    assert sent["market"] == {"nested": {"public_signal": "stable"}}
    assert marker not in str(sent)


@pytest.mark.asyncio
async def test_analyze_dispatch_preserves_private_context_with_explicit_opt_in(
    monkeypatch,
):
    expected = object()
    transport = AsyncMock(return_value=expected)
    monkeypatch.setattr(client_mod, "_call_claude", transport)
    context = {
        "symbol": "BTC_USDT",
        "account": {"equity_usdt": 5000.0, "positions": []},
        "remaining_risk_budget_pct": 2.5,
    }
    settings = _settings(llm_provider="claude", include_account_in_llm=True)

    result = await client_mod.analyze_with_llm(context, settings)

    assert result is expected
    transport.assert_awaited_once_with(context, settings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider", ("claude", "anthropic", "xai", "grok", "openai", "codex")
)
async def test_reevaluate_dispatch_blocks_external_provider_without_opt_in(
    monkeypatch, provider
):
    transports = {
        name: AsyncMock()
        for name in (
            "_call_claude_reevaluate",
            "_call_xai_reevaluate",
            "_call_openai_reevaluate",
        )
    }
    for name, transport in transports.items():
        monkeypatch.setattr(client_mod, name, transport)

    with pytest.raises(LlmError, match="INCLUDE_ACCOUNT_IN_LLM=true"):
        await reevaluate_with_llm(
            {"position": {"hold_vol": 1.0}},
            _settings(llm_provider=provider, include_account_in_llm=False),
        )

    for transport in transports.values():
        transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_reevaluate_dispatch_allows_external_provider_with_opt_in(monkeypatch):
    expected = ReevaluateProposal(
        action="HOLD", confidence="medium", reason="synthetic", risk_notes=""
    )
    transport = AsyncMock(return_value=expected)
    monkeypatch.setattr(client_mod, "_call_claude_reevaluate", transport)

    result = await reevaluate_with_llm(
        {"position": {"hold_vol": 1.0}},
        _settings(llm_provider="claude", include_account_in_llm=True),
    )

    assert result is expected
    transport.assert_awaited_once()


@pytest.mark.asyncio
async def test_reevaluate_dispatch_allows_local_ollama_without_external_opt_in(
    monkeypatch,
):
    expected = ReevaluateProposal(
        action="HOLD", confidence="medium", reason="synthetic", risk_notes=""
    )
    transport = AsyncMock(return_value=expected)
    monkeypatch.setattr(client_mod, "_call_ollama_reevaluate", transport)

    result = await reevaluate_with_llm(
        {"position": {"account_fields_omitted": True}},
        _settings(llm_provider="ollama", include_account_in_llm=False),
    )

    assert result is expected
    transport.assert_awaited_once()


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


def test_llm_metrics_do_not_log_untrusted_provider_values(caplog):
    marker = "SYNTHETIC_SECRET_MARKER"
    payload = {
        "stop_reason": marker,
        "choices": [{"finish_reason": marker}],
        "usage": {
            "input_tokens": marker,
            "output_tokens": marker,
            "prompt_tokens": marker,
            "completion_tokens": marker,
            "cache_read_input_tokens": marker,
            "cache_creation_input_tokens": marker,
            "completion_tokens_details": {"reasoning_tokens": marker},
            "prompt_tokens_details": {"cached_tokens": marker},
        },
    }

    with caplog.at_level(logging.INFO, logger="app.llm.client"):
        _log_llm_metrics(
            provider="synthetic",
            model="synthetic-model",
            route="test",
            elapsed_ms=1.0,
            payload=payload,
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if "LLM_METRICS" in record.getMessage()
    ]
    assert len(messages) == 1
    assert marker not in messages[0]
    assert "finish_reason=None" in messages[0]
    assert "stop_reason=None" in messages[0]


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


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "HOLD"},
        {"action": "HOLD", "confidence": "certain"},
    ],
)
def test_reevaluation_missing_or_invalid_confidence_defaults_low(payload):
    proposal = parse_reevaluation(json.dumps(payload))

    assert proposal.confidence == "low"


@pytest.mark.asyncio
async def test_xai_analyze_budget_and_timeout(monkeypatch):
    """Task 12 (O2-12/L2X-11): grok-4 is always-on-reasoning and reasoning
    tokens count against max_tokens, so the old 1800/1200 caps silently
    starved the JSON answer. analyze must send max_tokens=10000 and reevaluate
    max_tokens=4000, both over a 120s httpx timeout (was 90s — inverted vs.
    Claude's 120s despite grok being the faster model)."""
    analyze_payload = {
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
        "usage": {},
    }
    fake_analyze = _CapturingClient(200, analyze_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake_analyze)

    await _call_xai({"symbol": "BTC"}, _settings())

    assert fake_analyze.constructor_kwargs[-1]["timeout"] == 120.0
    assert fake_analyze.posted_bodies[-1]["max_tokens"] == 10000

    reevaluate_payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"action": "HOLD", "confidence": "medium", "reason": "ok"}
                    )
                },
            }
        ],
        "usage": {},
    }
    fake_reevaluate = _CapturingClient(200, reevaluate_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake_reevaluate)

    await _call_xai_reevaluate({"symbol": "BTC"}, _settings())

    assert fake_reevaluate.constructor_kwargs[-1]["timeout"] == 120.0
    assert fake_reevaluate.posted_bodies[-1]["max_tokens"] == 4000


@pytest.mark.asyncio
async def test_xai_empty_content_reports_finish_reason(monkeypatch, caplog):
    """O2-14/L2X-12: an empty completion with finish_reason=='length' means
    grok burned the whole max_tokens budget on reasoning before emitting any
    answer. That must surface as a WARNING (mirroring the Claude
    stop_reason==max_tokens log) plus a clear, actionable LlmError instead of
    a generic/confusing 'empty content' or JSON-parse failure."""
    truncated_payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": ""},
            }
        ],
        "usage": {},
    }
    fake = _FakeClient(200, truncated_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        with pytest.raises(LlmError) as exc_info:
            await _call_xai({"symbol": "BTC"}, _settings())

    message = str(exc_info.value)
    assert "truncated" in message
    assert "token budget" in message
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("length" in r.getMessage() for r in warnings)

    # Same handling for the reevaluate call.
    fake2 = _FakeClient(200, truncated_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake2)

    with pytest.raises(LlmError) as exc_info2:
        await _call_xai_reevaluate({"symbol": "BTC"}, _settings())

    message2 = str(exc_info2.value)
    assert "truncated" in message2
    assert "token budget" in message2


@pytest.mark.asyncio
async def test_xai_body_has_no_reasoning_effort(monkeypatch):
    """O2-14: grok-4 answers HTTP 400 to an unsupported `reasoning_effort`
    param (only grok-3-mini accepts it). Pin that neither the analyze nor the
    reevaluate xai request body ever includes that key."""
    analyze_payload = {
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
        "usage": {},
    }
    fake_analyze = _CapturingClient(200, analyze_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake_analyze)

    await _call_xai({"symbol": "BTC"}, _settings())

    assert "reasoning_effort" not in fake_analyze.posted_bodies[-1]

    reevaluate_payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"action": "HOLD", "confidence": "medium", "reason": "ok"}
                    )
                },
            }
        ],
        "usage": {},
    }
    fake_reevaluate = _CapturingClient(200, reevaluate_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake_reevaluate)

    await _call_xai_reevaluate({"symbol": "BTC"}, _settings())

    assert "reasoning_effort" not in fake_reevaluate.posted_bodies[-1]


@pytest.mark.asyncio
async def test_claude_reevaluate_budget_and_warnlog(monkeypatch, caplog):
    """Task 15 (P2-04/O2-01): the Claude reevaluate call used to send neither
    a `thinking` field nor enough max_tokens for the fixed 1200-token cap to
    survive adaptive thinking eating into it — and had no truncation warning
    at all (unlike the analyze call's stop_reason==max_tokens log). Pin the
    fixed request shape (adaptive thinking, max_tokens=8000) and the WARNING
    log when the response is truncated."""
    ok_payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"action": "HOLD", "confidence": "medium", "reason": "ok"}),
            }
        ],
        "stop_reason": "end_turn",
    }
    fake = _CapturingClient(200, ok_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    await _call_claude_reevaluate({"symbol": "BTC"}, _settings())

    body = fake.posted_bodies[-1]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["max_tokens"] == 8000

    truncated_payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"action": "HOLD", "confidence": "medium", "reason": "ok"}),
            }
        ],
        "stop_reason": "max_tokens",
    }
    fake2 = _CapturingClient(200, truncated_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake2)

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        await _call_claude_reevaluate({"symbol": "BTC"}, _settings())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("max_tokens" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_xai_sends_json_schema_strict(monkeypatch):
    """O2-13: analyze + reevaluate must send response_format=json_schema
    with strict:true, generated from the Pydantic model at call time (so a
    future field addition to TradeProposal/ReevaluateProposal flows through
    automatically) instead of the old bare json_object mode."""
    analyze_payload = {
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
        "usage": {},
    }
    fake = _CapturingClient(200, analyze_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    await _call_xai({"symbol": "BTC"}, _settings())

    rf = fake.posted_bodies[-1]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"]
    schema = rf["json_schema"]["schema"]
    assert schema["properties"]["action"]["enum"] == [
        "STRONG_BUY",
        "BUY",
        "STAY_OUT",
        "SELL",
        "STRONG_SHORT",
    ]

    reevaluate_payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"action": "HOLD", "confidence": "medium", "reason": "ok"}
                    )
                },
            }
        ],
        "usage": {},
    }
    fake2 = _CapturingClient(200, reevaluate_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake2)

    await _call_xai_reevaluate({"symbol": "BTC"}, _settings())

    rf2 = fake2.posted_bodies[-1]["response_format"]
    assert rf2["type"] == "json_schema"
    assert rf2["json_schema"]["strict"] is True
    assert "action" in rf2["json_schema"]["schema"]["properties"]


def _assert_all_objects_strict(node) -> None:
    """Recursively assert every object-shaped schema node (root, nested
    $defs, items, anyOf branches, ...) has additionalProperties:false and
    lists ALL of its own properties in `required` (optionality must be
    expressed via a nullable type, never via omission from required —
    grok's strict:true rejects the latter)."""
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, node
            props = node.get("properties", {})
            assert set(node.get("required", [])) == set(props.keys()), node
        for v in node.values():
            _assert_all_objects_strict(v)
    elif isinstance(node, list):
        for item in node:
            _assert_all_objects_strict(item)


@pytest.mark.asyncio
async def test_xai_schema_has_additional_properties_false(monkeypatch):
    """CRITICAL (plan review): grok strict:true REQUIRES
    additionalProperties:false AND every property listed in `required` on
    EVERY object node, including nested $defs (ProposalKeyLevels,
    ProposalManagement) — Pydantic's model_json_schema() emits neither by
    default. Skipping this post-processing makes every xai call answer 400
    and silently kills Structured Outputs."""
    analyze_payload = {
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
        "usage": {},
    }
    fake = _CapturingClient(200, analyze_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    await _call_xai({"symbol": "BTC"}, _settings())

    schema = fake.posted_bodies[-1]["response_format"]["json_schema"]["schema"]
    _assert_all_objects_strict(schema)
    # Nested models must actually be present and exercised, not skipped by
    # e.g. an empty/absent $defs.
    assert "$defs" in schema
    assert len(schema["$defs"]) >= 2
    # Block 2/TP2 Task P3: pre_mortem must be DECLARED (so grok can emit it)
    # but expressed as a nullable type, not dropped from `required` --
    # _assert_all_objects_strict above already proves every property
    # (pre_mortem included) is in `required`; this pins the nullable shape
    # so a model that omits the reasoning still returns valid JSON (null),
    # instead of the strict schema forcing a non-empty string.
    pm = schema["properties"]["pre_mortem"]
    assert {"type": "null"} in pm.get("anyOf", [])

    # Direct unit coverage of the helper itself on a synthetic schema that
    # mirrors the two shapes _strictify_schema must handle: a top-level
    # optional field expressed as anyOf[type, null] (must NOT be dropped
    # from required) and a $ref'd nested object living in $defs.
    synthetic = {
        "type": "object",
        "properties": {
            "required_field": {"type": "string"},
            "optional_field": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "nested": {"$ref": "#/$defs/Inner"},
        },
        "required": ["required_field"],
        "$defs": {
            "Inner": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            }
        },
    }
    out = _strictify_schema(synthetic)
    _assert_all_objects_strict(out)
    assert set(out["required"]) == {"required_field", "optional_field", "nested"}
    assert out["$defs"]["Inner"]["additionalProperties"] is False
    assert out["$defs"]["Inner"]["required"] == ["x"]


@pytest.mark.asyncio
async def test_xai_falls_back_to_json_object_on_400(monkeypatch):
    """O2-13: Structured Outputs (json_schema, strict) is the primary path,
    but stays a safety-netted addition, not a hard requirement. If the
    account/model/proxy combination rejects the json_schema request shape
    (HTTP 400), retry once with the old plain json_object response_format
    instead of hard-failing the whole analysis — mirrors _call_claude's
    thinking->plain 400-fallback."""
    ok_payload = {
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
        "usage": {},
    }
    seq = _SeqClient([(400, {"error": "schema not supported"}), (200, ok_payload)])
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", seq)

    result = await _call_xai({"symbol": "BTC"}, _settings())

    assert result.action == "STAY_OUT"
    assert len(seq.posted_bodies) == 2
    assert seq.posted_bodies[0]["response_format"]["type"] == "json_schema"
    assert seq.posted_bodies[1]["response_format"] == {"type": "json_object"}

    ok_reeval_payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"action": "HOLD", "confidence": "medium", "reason": "ok"}
                    )
                },
            }
        ],
        "usage": {},
    }
    seq2 = _SeqClient([(400, {"error": "schema not supported"}), (200, ok_reeval_payload)])
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", seq2)

    result2 = await _call_xai_reevaluate({"symbol": "BTC"}, _settings())

    assert result2.action == "HOLD"
    assert len(seq2.posted_bodies) == 2
    assert seq2.posted_bodies[0]["response_format"]["type"] == "json_schema"
    assert seq2.posted_bodies[1]["response_format"] == {"type": "json_object"}
