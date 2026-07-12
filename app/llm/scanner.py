"""Market scanner: a cheap/fast model screens many coins for setups.

Token split by design: the scanner sends COMPACT per-coin summaries (no candle
arrays) to a small model (SCANNER_MODEL, default Sonnet) in ONE batched call.
The expensive deep analysis per coin stays with LLM_PROVIDER (e.g. Opus) and
runs only when the user opens a coin from the result list.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.analysis.context import build_tf_slice, _candles_public  # noqa: F401
from app.config import Settings
from app.llm.client import LlmError, compact_tf_for_llm, extract_json_object

SCANNER_SYSTEM_PROMPT = """You are a futures market screener for USDT-M perpetuals.
You receive compact summaries (indicators, structure, funding) for several coins.
Select ONLY coins with a clear, tradeable directional edge right now.

Selection method per coin:
1. Regime: read.ema_stack + price vs EMAs. Skip "mixed" unless a clean range
   fade at range_high/range_low exists.
2. Momentum: rsi14 and macd_hist tails must support the direction.
3. Location: price must be NEAR a actionable level (support/resistance/swing),
   not in the middle of nowhere. Use read.price_vs_ema20_pct for stretch.
4. Funding as tiebreaker: crowded funding against the setup lowers the score.

Scoring: 0-10. Only include coins with score >= 5. Maximum 6 results,
sorted by score descending. It is fine to return an empty list.

Output ONLY valid JSON, no markdown, exactly this schema:
{
  "results": [
    {
      "symbol": "string",
      "bias": "long|short",
      "score": number,
      "setup": "pullback|breakout|range-fade|reversal",
      "reason": "max 12 words, concrete",
      "key_level": number|null
    }
  ]
}
"""


class ScanResult(BaseModel):
    symbol: str
    bias: str = Field(pattern="^(long|short)$")
    score: float = Field(ge=0, le=10)
    setup: str = ""
    reason: str = ""
    key_level: float | None = None


def _salvage_result_objects(text: str) -> list[dict]:
    """Extract every complete top-level {...} object from possibly-truncated
    LLM output. If the model hit max_tokens mid-array, the last (incomplete)
    object is simply skipped and the earlier complete ones are kept."""
    objs: list[dict] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        start = i
        j = i
        closed = False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    frag = text[start : j + 1]
                    try:
                        d = json.loads(frag)
                        if isinstance(d, dict) and "symbol" in d and "bias" in d:
                            objs.append(d)
                    except json.JSONDecodeError:
                        pass
                    closed = True
                    break
            j += 1
        if not closed:
            # This "{" never closed (e.g. the outer results wrapper, or a
            # truncated final entry). Skip it and keep scanning for inner ones.
            i = start + 1
            continue
        i = j + 1
    return objs


def parse_scan_results(
    text: str, allowed_symbols: set[str] | None = None
) -> list[ScanResult]:
    """Parse + validate the screener output. Invalid rows are dropped.

    Falls back to per-object salvage when the whole JSON won't parse (e.g. the
    model was cut off at max_tokens mid-array) so partial results still show.

    F-22: when `allowed_symbols` is given, any result whose symbol isn't in
    that server-side set (the coins actually sent to the LLM) is dropped —
    the LLM's output isn't trusted to only mention symbols it was given."""
    rows: list[dict] = []
    try:
        data = json.loads(extract_json_object(text))
        raw = data.get("results") if isinstance(data, dict) else data
        rows = [r for r in (raw or []) if isinstance(r, dict)]
    except json.JSONDecodeError:
        rows = _salvage_result_objects(text)
        if not rows:
            raise  # truly unparseable — let the caller report it

    allowed_upper = (
        {s.upper() for s in allowed_symbols} if allowed_symbols is not None else None
    )
    out: list[ScanResult] = []
    seen: set[str] = set()
    for row in rows:
        try:
            r = ScanResult.model_validate(row)
        except ValidationError:
            continue
        key = r.symbol.upper()
        if allowed_upper is not None and key not in allowed_upper:
            continue  # hallucinated / out-of-scope symbol — never surfaced
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:6]


def _mini_tf(slice_obj, tf: str) -> dict[str, Any]:
    """Ultra-compact TF summary: read + last indicators + levels, no candles."""
    slice_dict = {
        "tf": tf,
        "candles": _candles_public(slice_obj.candles),
        "indicators": slice_obj.indicators,
        "structure": slice_obj.structure,
    }
    c = compact_tf_for_llm(slice_dict, recent_bars=0)
    c.pop("recent_candles", None)
    # trim tails to the essentials for screening
    tails = c.get("indicators_tail") or {}
    c["indicators_tail"] = {
        "rsi14": tails.get("rsi14", [])[-6:],
        "macd_hist": tails.get("macd_hist", [])[-6:],
    }
    return c


async def build_scan_contexts(
    client: Any,
    overview: list[dict[str, Any]],
    tf: str,
    htf: str,
    *,
    kline_limit: int = 120,
    concurrency: int = 4,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch klines per coin (bounded concurrency) and build mini contexts."""
    sem = asyncio.Semaphore(concurrency)
    errors: list[str] = []

    async def _one(row: dict[str, Any]) -> dict[str, Any] | None:
        sym = row["symbol"]
        async with sem:
            try:
                ltf_candles = await client.klines(sym, tf, limit_hint=kline_limit)
                htf_candles = await client.klines(sym, htf, limit_hint=kline_limit)
            except Exception as e:  # exchange hiccup on one coin must not kill the scan
                errors.append(f"{sym}: {e}")
                return None
        if not ltf_candles:
            errors.append(f"{sym}: no candles")
            return None
        return {
            "symbol": sym,
            "last_price": row.get("last") or ltf_candles[-1].close,
            "volume24_usd": row.get("volume24"),
            "funding_rate": row.get("funding"),
            # HTF first (regime before LTF timing)
            "htf": _mini_tf(build_tf_slice(htf, htf_candles), htf),
            "ltf": _mini_tf(build_tf_slice(tf, ltf_candles), tf),
        }

    results = await asyncio.gather(*(_one(r) for r in overview))
    return [r for r in results if r is not None], errors


def _scan_user_prompt(contexts: list[dict[str, Any]]) -> str:
    payload = json.dumps(contexts, ensure_ascii=False, separators=(",", ":"), default=str)
    return (
        "Screen the following coins and return the results JSON.\n\nCOINS:\n" + payload
    )


async def _anthropic_text(
    system: str, user: str, model: str, settings: Settings, timeout: float = 120.0
) -> str:
    url = settings.anthropic_base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": settings.anthropic_version,
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LlmError(f"Scanner request failed: {e}") from e
    if r.status_code >= 400:
        raise LlmError(f"Scanner HTTP {r.status_code}: {r.text[:400]}")
    payload = r.json()
    parts = [
        str(b.get("text") or "")
        for b in payload.get("content") or []
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    # extract_json_object() strips any prose/fences the model adds around JSON
    return "\n".join(parts).strip()


async def _openai_compat_text(
    system: str,
    user: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float = 120.0,
) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(base_url.rstrip("/") + "/chat/completions", headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LlmError(f"Scanner request failed: {e}") from e
    if r.status_code >= 400:
        raise LlmError(f"Scanner HTTP {r.status_code}: {r.text[:400]}")
    try:
        return str(r.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise LlmError("Scanner response missing content") from e


async def scan_with_llm(
    contexts: list[dict[str, Any]], settings: Settings
) -> tuple[list[ScanResult], str]:
    """One batched screener call. Returns (results, model_used)."""
    if not contexts:
        return [], "none"
    user = _scan_user_prompt(contexts)

    # Cheap model first (token split), fall back to whatever is configured
    if settings.claude_ready:
        model = settings.scanner_model
        text = await _anthropic_text(SCANNER_SYSTEM_PROMPT, user, model, settings)
    elif settings.xai_ready:
        model = settings.xai_model
        text = await _openai_compat_text(
            SCANNER_SYSTEM_PROMPT, user, model, settings.xai_base_url, settings.xai_api_key
        )
    elif settings.openai_ready:
        model = settings.openai_model
        text = await _openai_compat_text(
            SCANNER_SYSTEM_PROMPT, user, model, settings.openai_base_url, settings.openai_api_key
        )
    elif settings.ollama_ready:
        model = settings.ollama_model
        text = await _openai_compat_text(
            SCANNER_SYSTEM_PROMPT, user, model, settings.ollama_base_url, "", timeout=300.0
        )
    else:
        raise LlmError("No LLM configured for the scanner")

    # Server-side allowlist: only symbols we actually sent to the LLM may
    # come back (F-22) — a hallucinated-but-valid-looking symbol is dropped.
    allowed_symbols = {
        str(c.get("symbol") or "").upper() for c in contexts if c.get("symbol")
    }
    try:
        return parse_scan_results(text, allowed_symbols=allowed_symbols), model
    except json.JSONDecodeError as e:
        preview = (text or "")[:200].replace("\n", " ")
        raise LlmError(
            f"Scanner-Antwort ist kein JSON ({e}). Modell {model} lieferte: "
            f"{preview!r}",
            raw=text,
        ) from e
