"""Read-only provider probe: host-pinning (SSRF), status mapping, no readback."""

import httpx
import pytest

import app.llm.probe as probe_mod
from app.llm.probe import probe_provider


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    """Records requested URLs/headers; returns a scripted response."""

    calls: list = []
    resp = _FakeResp(200, {"models": [1, 2, 3]})
    raise_exc = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeClient.calls.append((url, headers or {}))
        if _FakeClient.raise_exc:
            raise _FakeClient.raise_exc
        return _FakeClient.resp


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.resp = _FakeResp(200, {"models": [1, 2, 3]})
    _FakeClient.raise_exc = None
    monkeypatch.setattr(probe_mod.httpx, "AsyncClient", _FakeClient)
    yield


@pytest.mark.asyncio
async def test_cloud_hosts_are_pinned():
    await probe_provider("claude", api_key="k")
    await probe_provider("xai", api_key="k")
    await probe_provider("openai", api_key="k")
    urls = [c[0] for c in _FakeClient.calls]
    assert urls == [
        "https://api.anthropic.com/v1/models",
        "https://api.x.ai/v1/models",
        "https://api.openai.com/v1/models",
    ]


@pytest.mark.asyncio
async def test_claude_uses_xapikey_header():
    await probe_provider("claude", api_key="secret-key")
    _, headers = _FakeClient.calls[-1]
    assert headers.get("x-api-key") == "secret-key"
    assert headers.get("anthropic-version") == "2023-06-01"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_bearer_header_for_xai_openai():
    await probe_provider("openai", api_key="sk-o")
    _, headers = _FakeClient.calls[-1]
    assert headers.get("Authorization") == "Bearer sk-o"


@pytest.mark.asyncio
async def test_missing_key_short_circuits():
    r = await probe_provider("xai", api_key="")
    assert r["ok"] is False
    assert not _FakeClient.calls  # never hit the network


@pytest.mark.asyncio
async def test_unknown_provider():
    r = await probe_provider("bogus", api_key="k")
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_401_maps_to_invalid():
    _FakeClient.resp = _FakeResp(401)
    r = await probe_provider("openai", api_key="bad")
    assert r["ok"] is False
    assert "ungültig" in r["detail"].lower()


@pytest.mark.asyncio
async def test_200_ok():
    r = await probe_provider("openai", api_key="good")
    assert r["ok"] is True
    assert "latency_ms" in r


@pytest.mark.asyncio
async def test_ollama_strips_v1_and_ignores_key():
    await probe_provider(
        "ollama", api_key="ignored", ollama_base_url="http://127.0.0.1:11434/v1"
    )
    url, headers = _FakeClient.calls[-1]
    assert url == "http://127.0.0.1:11434/api/tags"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_network_error_maps_to_unreachable():
    _FakeClient.raise_exc = httpx.ConnectError("boom")
    r = await probe_provider("openai", api_key="k")
    assert r["ok"] is False
    assert "erreichbar" in r["detail"].lower()
