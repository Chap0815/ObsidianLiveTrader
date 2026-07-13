"""O2: Anthropic prompt caching on the deep-analyze system prompt.

The large static analyze system prompt is wrapped as a cacheable block
(`cache_control: {"type": "ephemeral"}`) on the Claude/Anthropic request
path ONLY, so repeat analyzes within the 5-min TTL reuse cached input
tokens. Non-Claude providers (xai/openai/ollama) still send a plain-string
system prompt. The prompt CONTENT is unchanged; below the cache minimum the
API simply does not cache (no error).
"""

import json

import pytest

import app.llm.client as client_mod
from app.config import Settings
from app.llm.client import _call_claude, _call_xai

_PROPOSAL = {
    "action": "STAY_OUT",
    "htf_trend": "ranging",
    "ltf_trend": "ranging",
    "rationale": "test",
}


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _CaptureClient:
    """Captures the JSON body posted to the provider."""

    last_body = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _CaptureClient.last_body = json
        return _FakeResp(200, self._resp())

    def _resp(self):
        raise NotImplementedError


class _ClaudeClient(_CaptureClient):
    def _resp(self):
        return {"content": [{"type": "text", "text": json.dumps(_PROPOSAL)}]}


class _XaiClient(_CaptureClient):
    def _resp(self):
        return {"choices": [{"message": {"content": json.dumps(_PROPOSAL)}}]}


def _settings(**kw):
    base = dict(
        anthropic_api_key="k",
        xai_api_key="k",
        include_account_in_llm=False,
    )
    base.update(kw)
    return Settings(**base)


@pytest.mark.asyncio
async def test_claude_system_prompt_is_cache_controlled(monkeypatch):
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _ClaudeClient)
    await _call_claude({"symbol": "BTC"}, _settings())
    system = _CaptureClient.last_body["system"]
    assert isinstance(system, list)
    assert system[0]["type"] == "text"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(system[0]["text"], str) and system[0]["text"]


@pytest.mark.asyncio
async def test_xai_system_prompt_is_plain_string(monkeypatch):
    """Cache_control is Anthropic-specific — other providers must not receive it."""
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _XaiClient)
    await _call_xai({"symbol": "BTC"}, _settings())
    messages = _CaptureClient.last_body["messages"]
    system_msg = next(m for m in messages if m["role"] == "system")
    assert isinstance(system_msg["content"], str)
