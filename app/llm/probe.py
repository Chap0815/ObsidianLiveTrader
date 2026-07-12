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
        return await _probe_ollama(ollama_base_url, start)

    if prov not in _PINNED:
        return {"ok": False, "detail": f"Unbekannter Anbieter: {provider}", "latency_ms": 0}

    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "detail": "Kein API Key angegeben", "latency_ms": 0}

    url = _PINNED[prov]
    if prov == "claude":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.HTTPError, OSError) as e:
        return {"ok": False, "detail": f"nicht erreichbar: {e}", "latency_ms": _elapsed_ms(start)}

    ms = _elapsed_ms(start)
    if resp.status_code == 200:
        return {"ok": True, "detail": "Key gültig, Anbieter erreichbar", "latency_ms": ms}
    if resp.status_code in (401, 403):
        return {"ok": False, "detail": "Key ungültig", "latency_ms": ms}
    return {"ok": False, "detail": f"Antwort {resp.status_code}", "latency_ms": ms}


async def _probe_ollama(ollama_base_url: str, start: float) -> dict:
    # Base is server-side/default only; strip a trailing /v1 to reach /api/tags.
    root = (ollama_base_url or "http://127.0.0.1:11434/v1").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = f"{root}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
    except (httpx.HTTPError, OSError) as e:
        return {"ok": False, "detail": f"nicht erreichbar: {e}", "latency_ms": _elapsed_ms(start)}
    ms = _elapsed_ms(start)
    if resp.status_code == 200:
        try:
            n = len((resp.json() or {}).get("models") or [])
        except Exception:
            n = 0
        return {"ok": True, "detail": f"{n} Modelle gefunden", "latency_ms": ms}
    return {"ok": False, "detail": f"Antwort {resp.status_code}", "latency_ms": ms}
