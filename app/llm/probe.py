"""Read-only provider reachability/auth probe.

Used by both the setup test route (POST /api/setup/test-provider) and the
authenticated settings test route (POST /api/settings/test-provider).

Security:
- NEVER creates a completion (no token cost, no side effects) — only a cheap
  GET against the provider's model-list endpoint.
- Cloud provider hosts are HARD-PINNED constants; no request field can point the
  probe at another host (SSRF-safe).
- Ollama uses the server-side, loopback-validated base URL only — never a
  client-supplied URL.
- The api_key is used transiently for the request and never returned.
"""

from __future__ import annotations

import time

import httpx

# Hard-pinned cloud endpoints (SSRF: not derived from any request field).
_PINNED = {
    "claude": "https://api.anthropic.com/v1/models",
    "xai": "https://api.x.ai/v1/models",
    "openai": "https://api.openai.com/v1/models",
}

_TIMEOUT = 10.0


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _model_ids(payload: object, *, list_key: str, id_keys: tuple[str, ...]) -> set[str] | None:
    if not isinstance(payload, dict):
        return None
    rows = payload.get(list_key)
    if not isinstance(rows, list):
        return None
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        model_id = None
        for key in id_keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                model_id = value.strip()
                break
        if model_id is None:
            return None
        ids.add(model_id)
    return ids


def _ollama_model_key(model: str) -> str:
    return model[: -len(":latest")] if model.endswith(":latest") else model


async def probe_provider(
    provider: str,
    *,
    api_key: str = "",
    model: str = "",
    ollama_base_url: str = "",
) -> dict:
    """Cheap read-only reachability/auth check.

    Returns ``{"ok": bool, "detail": str, "latency_ms": int}``.
    """
    prov = (provider or "").strip().lower()
    aliases = {"anthropic": "claude", "grok": "xai", "codex": "openai", "local": "ollama"}
    prov = aliases.get(prov, prov)
    start = time.monotonic()

    if prov == "ollama":
        return await _probe_ollama(ollama_base_url, model, start)

    if prov not in _PINNED:
        return {"ok": False, "detail": f"Unknown provider: {provider}", "latency_ms": 0}

    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "detail": "No API key provided", "latency_ms": 0}

    url = _PINNED[prov]
    if prov == "claude":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.HTTPError, OSError):
        return {"ok": False, "detail": "Provider unavailable", "latency_ms": _elapsed_ms(start)}

    ms = _elapsed_ms(start)
    if resp.status_code == 200:
        try:
            model_ids = _model_ids(
                resp.json(), list_key="data", id_keys=("id",)
            )
        except (TypeError, ValueError):
            model_ids = None
        if model_ids is None:
            return {
                "ok": False,
                "detail": f"Invalid {prov} model-list response",
                "latency_ms": ms,
            }
        return {"ok": True, "detail": "Key valid; provider reachable", "latency_ms": ms}
    if resp.status_code in (401, 403):
        return {"ok": False, "detail": "Invalid key", "latency_ms": ms}
    return {"ok": False, "detail": f"Response {resp.status_code}", "latency_ms": ms}


async def _probe_ollama(ollama_base_url: str, model: str, start: float) -> dict:
    # Base is server-side/default only; strip a trailing /v1 to reach /api/tags.
    root = (ollama_base_url or "http://127.0.0.1:11434/v1").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = f"{root}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
    except (httpx.HTTPError, OSError):
        return {"ok": False, "detail": "Provider unavailable", "latency_ms": _elapsed_ms(start)}
    ms = _elapsed_ms(start)
    if resp.status_code == 200:
        try:
            model_ids = _model_ids(
                resp.json(), list_key="models", id_keys=("name", "model")
            )
        except (TypeError, ValueError):
            model_ids = None
        if model_ids is None:
            return {
                "ok": False,
                "detail": "Invalid Ollama model-list response",
                "latency_ms": ms,
            }
        requested_model = (model or "").strip()
        if requested_model and _ollama_model_key(requested_model) not in {
            _ollama_model_key(item) for item in model_ids
        }:
            return {
                "ok": False,
                "detail": "Configured model is not available",
                "latency_ms": ms,
            }
        return {
            "ok": True,
            "detail": f"{len(model_ids)} models found",
            "latency_ms": ms,
        }
    return {"ok": False, "detail": f"Response {resp.status_code}", "latency_ms": ms}
