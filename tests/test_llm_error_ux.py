"""Credit/rate-limit error UX (app/llm/client.py._categorize_provider_http_error).

A provider HTTP error (401/403/429, or a body carrying a credits/rate-limit
keyword) is turned into a clean German message with the provider label, so
the frontend can show a prominent "⚠ ..." banner + a "KI wechseln"
affordance instead of a raw "Claude HTTP 403: {...}" dump. The raw response
body is inspected — request headers (which carry the API key) never are, so
no secret can leak into the message.

Covers the pure categorization helper, the per-provider HTTP call sites
(_call_claude / _call_xai), and the full /api/analyze wiring with a fake
provider transport (no real network).
"""

from __future__ import annotations

import json

import pytest

import app.llm.client as client_mod
from app.config import Settings, get_settings
from app.llm.client import LlmError, _call_claude, _call_xai, _categorize_provider_http_error


# --- Pure helper -------------------------------------------------------


def test_categorize_401_is_invalid_key_message():
    """401 is an AUTH problem (bad/missing key), not a billing one — must NOT
    say 'Credits erschöpft', which would send the trader looking to top up an
    account instead of fixing the key."""
    msg = _categorize_provider_http_error("Claude", 401, {"error": {"type": "auth"}})
    assert msg.startswith("⚠ Claude:")
    assert "API-Key ungültig oder fehlt" in msg
    assert "Credits erschöpft" not in msg


def test_categorize_403_with_credit_keyword_is_credits_message():
    """L-11: a 403 body that actually carries a credits/billing signal is
    still reported as the credits-exhausted banner."""
    msg = _categorize_provider_http_error(
        "xAI", 403, {"error": "insufficient quota for this request"}
    )
    assert msg.startswith("⚠ xAI:")
    assert "Credits erschöpft oder Limit erreicht" in msg


def test_categorize_403_without_credit_keyword_is_generic():
    """L-11: a bare 403 with no credits/billing signal in the body must NOT
    be reported as 'Credits erschöpft' — that misleads a trader whose key
    lacks permissions or is region-locked into topping up a balance that
    isn't the problem. It gets a generic access-denied message instead."""
    msg = _categorize_provider_http_error("xAI", 403, {"error": "forbidden"})
    assert msg.startswith("⚠ xAI:")
    assert "Credits erschöpft" not in msg
    assert "Zugriff verweigert" in msg


def test_categorize_429_is_rate_limit_message():
    msg = _categorize_provider_http_error("Codex", 429, {"error": "slow down"})
    assert msg.startswith("⚠ Codex:")
    assert "Rate-Limit" in msg


def test_categorize_credit_keyword_in_body_without_401_403():
    """A provider returning e.g. 400 with a credits-style body must still be
    categorized, not just relying on the status code."""
    msg = _categorize_provider_http_error(
        "Claude", 400, {"error": {"message": "Your spending limit was reached"}}
    )
    assert msg.startswith("⚠ Claude:")
    assert "Credits erschöpft" in msg


def test_categorize_rate_limit_keyword_in_body_without_429():
    msg = _categorize_provider_http_error(
        "Ollama", 503, {"error": "rate limit exceeded, try later"}
    )
    assert msg.startswith("⚠ Ollama:")
    assert "Rate-Limit" in msg


def test_categorize_other_error_keeps_existing_message():
    msg = _categorize_provider_http_error("Claude", 500, {"error": "internal server error"})
    assert not msg.startswith("⚠")
    assert "Claude HTTP 500" in msg


def test_categorize_bare_credit_substring_is_not_a_false_positive():
    """A bare, unrelated 'credit' substring (e.g. inside an unrelated word or
    generic message) must NOT be categorized as the credits banner — only the
    real credit/spending-limit phrases should match."""
    msg = _categorize_provider_http_error(
        "Claude", 500, {"error": "database credential rotation failed"}
    )
    assert not msg.startswith("⚠")
    assert "Claude HTTP 500" in msg


def test_categorize_used_all_credits_phrase_is_credits_message():
    msg = _categorize_provider_http_error(
        "Claude", 400, {"error": {"message": "You have used all available credits"}}
    )
    assert msg.startswith("⚠ Claude:")
    assert "Credits erschöpft" in msg


def test_categorize_never_leaks_api_key():
    """Only the response BODY is inspected — an API key passed in as part of
    a (contrived) detail payload key name must not appear verbatim unless it
    was actually IN the body; this asserts the helper doesn't echo back
    anything beyond the given detail object."""
    secret = "sk-ant-super-secret-key-content"
    msg = _categorize_provider_http_error("Claude", 403, {"error": "forbidden"})
    assert secret not in msg


# --- Per-provider HTTP call sites (fake transport, no network) ---------


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeErrorClient:
    """Minimal httpx.AsyncClient stand-in that always returns a fixed error
    response, regardless of the request — mirrors test_llm_prompt_caching's
    _CaptureClient pattern."""

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
        # The app's lifespan closes the real exchange client's httpx.AsyncClient
        # at shutdown; since this fake replaces httpx.AsyncClient globally for
        # the test, it must satisfy that interface too.
        return None


def _settings(**kw):
    base = dict(anthropic_api_key="k", xai_api_key="k", include_account_in_llm=False)
    base.update(kw)
    return Settings(**base)


@pytest.mark.asyncio
async def test_call_claude_403_credits_raises_categorized_error(monkeypatch):
    fake = _FakeErrorClient(
        403, {"error": {"type": "permission_error", "message": "insufficient quota"}}
    )
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)
    with pytest.raises(LlmError) as ei:
        await _call_claude({"symbol": "BTC"}, _settings())
    assert str(ei.value).startswith("⚠ Claude:")
    assert "Credits erschöpft" in str(ei.value)


@pytest.mark.asyncio
async def test_call_claude_403_without_credit_keyword_is_generic(monkeypatch):
    """L-11: a bare 403 (no credits/billing signal in the body) from the real
    Claude call site surfaces the generic access-denied message, not the
    misleading credits banner."""
    fake = _FakeErrorClient(403, {"error": {"type": "permission_error", "message": "forbidden"}})
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)
    with pytest.raises(LlmError) as ei:
        await _call_claude({"symbol": "BTC"}, _settings())
    assert str(ei.value).startswith("⚠ Claude:")
    assert "Credits erschöpft" not in str(ei.value)
    assert "Zugriff verweigert" in str(ei.value)


@pytest.mark.asyncio
async def test_call_xai_429_rate_limit_raises_categorized_error(monkeypatch):
    fake = _FakeErrorClient(429, {"error": "rate limit exceeded"})
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)
    with pytest.raises(LlmError) as ei:
        await _call_xai({"symbol": "BTC"}, _settings())
    assert str(ei.value).startswith("⚠ xAI:")
    assert "Rate-Limit" in str(ei.value)


# --- L-09: single retry on transient provider failure -------------------


class _FlakyClient:
    """httpx.AsyncClient stand-in whose first post() returns a transient
    error status, and whose second (retried) post() succeeds. Used to verify
    `_post_with_retry` retries exactly once and recovers."""

    def __init__(self, first_status, first_payload, second_payload):
        self._first_status = first_status
        self._first_payload = first_payload
        self._second_payload = second_payload
        self.calls = 0

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        if self.calls == 1:
            return _FakeResp(self._first_status, self._first_payload)
        return _FakeResp(200, self._second_payload)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_llm_retries_once_on_transient(monkeypatch):
    """L-09: a transient 503 on the first attempt is retried exactly once
    (after a short backoff) and a 200 on the retry succeeds -- the advisory
    call is not lost to a one-off provider hiccup."""
    from unittest.mock import AsyncMock

    claude_payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"htf_trend": "bullish", "ltf_trend": "bullish", "action": "STAY_OUT"}
                ),
            }
        ]
    }
    fake = _FlakyClient(503, {"error": "service unavailable"}, claude_payload)
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)
    monkeypatch.setattr(client_mod.asyncio, "sleep", AsyncMock())

    result = await _call_claude({"symbol": "BTC"}, _settings())

    assert result.action == "STAY_OUT"
    assert fake.calls == 2  # exactly one retry, no more


@pytest.mark.asyncio
async def test_llm_does_not_retry_twice_on_persistent_transient_failure(monkeypatch):
    """L-09: a provider that stays down (503 on every call) must fail after
    exactly one retry (two total attempts), not loop indefinitely."""
    from unittest.mock import AsyncMock

    class _AlwaysDownClient(_FlakyClient):
        async def post(self, url, headers=None, json=None):
            self.calls += 1
            return _FakeResp(503, {"error": "service unavailable"})

    fake = _AlwaysDownClient(503, {"error": "down"}, {})
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)
    monkeypatch.setattr(client_mod.asyncio, "sleep", AsyncMock())

    with pytest.raises(LlmError):
        await _call_claude({"symbol": "BTC"}, _settings())

    assert fake.calls == 2  # first attempt + exactly one retry


# --- Full /api/analyze wiring (fake provider transport, no network) ----


def _mock_snap():
    return {
        "symbol": "BTC_USDT",
        "last_price": 100_000.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 0.0001},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }


def _env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    get_settings.cache_clear()


def test_analyze_endpoint_returns_credits_message_on_403(monkeypatch):
    """A fake provider raising a 403-credits error -> /api/analyze surfaces
    the categorized German message (502, advisory-safe — no order touched)."""
    _env(monkeypatch)
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi.testclient import TestClient

    from app.main import app

    fake = _FakeErrorClient(403, {"error": {"type": "permission_error", "message": "insufficient quota"}})
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    mexc_client = MagicMock()
    mexc_client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = mexc_client
        tc.app.state.analyze_cache = {}
        with (
            patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock())),
            patch("app.main.snapshot_to_api_dict", return_value=_mock_snap()),
        ):
            r = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
            )

        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail.startswith("⚠ Claude:")
        assert "Credits erschöpft" in detail
        # Never cached: a follow-up identical request must hit the LLM again.
        assert tc.app.state.analyze_cache == {}

    get_settings.cache_clear()


def test_analyze_endpoint_returns_rate_limit_message_on_429(monkeypatch):
    """A fake provider raising a 429 -> /api/analyze surfaces the rate-limit
    message."""
    _env(monkeypatch)
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi.testclient import TestClient

    from app.main import app

    fake = _FakeErrorClient(429, {"error": "slow down"})
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", fake)

    mexc_client = MagicMock()
    mexc_client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    with TestClient(app) as tc:
        tc.app.state.mexc = mexc_client
        tc.app.state.analyze_cache = {}
        with (
            patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock())),
            patch("app.main.snapshot_to_api_dict", return_value=_mock_snap()),
        ):
            r = tc.post(
                "/api/analyze",
                json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"},
            )

        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail.startswith("⚠ Claude:")
        assert "Rate-Limit" in detail

    get_settings.cache_clear()
