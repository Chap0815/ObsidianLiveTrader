"""Market scanner: a cheap/fast model screens many coins for setups.

Token split by design: the scanner sends COMPACT per-coin summaries (no candle
arrays) to a small model (SCANNER_MODEL, default Sonnet) in ONE batched call.
The expensive deep analysis per coin stays with LLM_PROVIDER (e.g. Opus) and
runs only when the user opens a coin from the result list.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.analysis.context import (  # noqa: F401
    build_tf_slice,
    _candles_public,
    _fetch_daily_candles,
    _funding_annualized,
    _funding_extreme,
)
from app.config import Settings
from app.llm.client import (
    LlmError,
    _categorize_provider_http_error,
    _log_llm_metrics,
    _oi_read_label,
    _post_with_retry,
    compact_daily_for_llm,
    compact_tf_for_llm,
    extract_json_object,
)

SCANNER_SYSTEM_PROMPT = """You are a futures market screener for USDT-M perpetuals.
You receive compact summaries (indicators, structure, funding) for several coins.
Select ONLY coins with a clear, tradeable directional edge right now.

This screen is the fast first stage before a stricter deep analysis. To stop
surfacing coins the deep stage will only reject, apply that stage's cheap
NON-NEGOTIABLE gates here too. These are the ONLY hard rejects — REJECT a coin
(do not include it) if ANY holds:
  - No-chase / over-stretch: price has already run far from value —
    |read.price_vs_ema20_pct| is large (roughly > 1.5x a normal push) with no
    fresh pullback level nearby. An already-extended breakout is not a fresh
    entry.
  - Coarse RRR infeasible: the distance from price to the NEAREST opposing
    structure level (support for a short, resistance for a long) is smaller
    than a sane structure+ATR stop on the other side — i.e. reward < risk. If
    there is no room to a target, skip it.

Against the daily regime is NOT a hard reject. The deep analyzer does NOT veto
an against-daily setup — it still TRADES it at low confidence (daily is a
confidence CAP, not a veto). So do NOT drop a coin just because daily_stack (the
1D ema_stack anchor) opposes the bias. Instead treat it as a SCORE PENALTY:
CAP its score at 6 (surface it only if it otherwise clears both hard rejects
above and still earns >= 6). Mirroring the analyzer here stops the screen from
silently starving it of valid low-confidence against-daily setups.

Selection method per coin:
1. Regime: read.ema_stack + price vs EMAs, cross-checked against daily_stack.
   Skip "mixed" unless a clean range fade at range_high/range_low exists.
2. Momentum: rsi14 and macd_hist tails must support the direction.
3. Location: price must be NEAR an actionable level (support/resistance/swing),
   not in the middle of nowhere. Use read.price_vs_ema20_pct for stretch.
4. Funding as tiebreaker: use fundingExtreme ("crowded_long"/"crowded_short"/
   "neutral") and fundingAnnualized, not just the raw rate — crowded funding
   against the setup (e.g. crowded_long under a long idea = squeeze risk)
   lowers the score.
5. Positioning (only when a coin carries oi_read): an OI read that CONFIRMS the
   bias (price_up_oi_up for a long, price_down_oi_up for a short) is a
   supporting confluence that lifts the score; price_up_oi_down (short covering)
   under a long idea is a fade-risk deprioritizer. When oi_read is absent
   (MEXC / cold-start), ignore it — do not infer positioning.

Scoring: 0-10 as an ABSOLUTE quality bar, not a relative ranking. A 6 means
"a disciplined analyst would actually take this now." Only include coins with
score >= 5 that clear both hard rejects above (against-daily is a score cap, not
a reject). Maximum 6 results, sorted by score descending. Returning an empty
list is the correct answer in a quiet market.

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


# Absolute keep gate for the deep-analysis handoff. The scanner score is an
# absolute quality bar (not a relative top-N rank): a coin below this is one the
# deep analyzer would reject anyway, so it must not be surfaced (audit B2).
#
# L-02: kept BELOW the prompt's against-daily score cap (6, see
# SCANNER_SYSTEM_PROMPT above) on purpose. When both were 6.0, an against-daily
# setup capped at exactly 6 only survived if the model scored it at PRECISELY
# 6.0 -- one epsilon under and the floor dropped it, starving the whole 5-6
# band the cap was designed to let through. Left as a module constant (not a
# Settings field): it's read from exactly one call site (scan_with_llm below)
# together with the prompt's hard-coded "6" cap, so the two must change in
# lockstep -- a config knob here would let them drift out of sync without
# actually buying any deployment-time flexibility.
SCANNER_MIN_SCORE = 5.0


def parse_scan_results(
    text: str,
    allowed_symbols: set[str] | None = None,
    *,
    min_score: float = 0.0,
) -> list[ScanResult]:
    """Parse + validate the screener output. Invalid rows are dropped.

    Falls back to per-object salvage when the whole JSON won't parse (e.g. the
    model was cut off at max_tokens mid-array) so partial results still show.

    F-22: when `allowed_symbols` is given, any result whose symbol isn't in
    that server-side set (the coins actually sent to the LLM) is dropped —
    the LLM's output isn't trusted to only mention symbols it was given.

    `min_score` is a server-side ABSOLUTE floor (default 0 = keep everything the
    model returned): rows scoring below it are dropped so the scanner stops
    handing the deep analyzer marginal coins it will reject (audit B2)."""
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
        if r.score < min_score:
            continue  # below the absolute quality bar — deep stage would reject
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


def _daily_stack(daily: str, daily_candles: list[Any]) -> str:
    """1D ema_stack anchor label for the screener (bullish/bearish/mixed/
    unknown). Empty candles (fetch failed / new coin) -> "unknown"."""
    if not daily_candles:
        return "unknown"
    slice_obj = build_tf_slice(daily, daily_candles)
    slice_dict = {
        "tf": daily,
        "candles": _candles_public(slice_obj.candles),
        "indicators": slice_obj.indicators,
        "structure": slice_obj.structure,
    }
    return (compact_daily_for_llm(slice_dict).get("read") or {}).get(
        "ema_stack", "unknown"
    )


async def build_scan_contexts(
    client: Any,
    overview: list[dict[str, Any]],
    tf: str,
    htf: str,
    *,
    kline_limit: int = 260,
    concurrency: int = 2,
    daily: str = "1D",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch klines per coin (bounded concurrency) and build mini contexts.

    `kline_limit` must stay >= 250 so EMA200 is computable for every timeframe:
    with the old 120 the scanner's `read.ema_stack` and the 1D `daily_stack`
    were ALWAYS "unknown", so the regime + against-daily gates could never fire
    (audit B2). The same depth feeds the depth-aware daily cache (audit B1)."""
    sem = asyncio.Semaphore(concurrency)
    errors: list[str] = []

    async def _one(row: dict[str, Any]) -> dict[str, Any] | None:
        sym = row["symbol"]
        async with sem:
            try:
                # O4: a coin's three timeframe fetches are independent — run them
                # concurrently so each coin's round-trips overlap. Concurrency was
                # dropped 4->2 alongside this so the in-flight burst (2 coins x 3 =
                # 6) stays close to the old 4, still net faster than sequential.
                # Fail-safe preserved: a failed ltf/htf raises -> handled below;
                # _fetch_daily_candles never raises (returns [] -> "unknown").
                ltf_candles, htf_candles, daily_candles = await asyncio.gather(
                    client.klines(sym, tf, limit_hint=kline_limit),
                    client.klines(sym, htf, limit_hint=kline_limit),
                    _fetch_daily_candles(client, sym, daily, kline_limit),
                )
            except Exception as e:  # exchange hiccup on one coin must not kill the scan
                errors.append(f"{sym}: {e}")
                return None
        if not ltf_candles:
            errors.append(f"{sym}: no candles")
            return None
        rate = row.get("funding")
        ctx: dict[str, Any] = {
            "symbol": sym,
            "last_price": row.get("last") or ltf_candles[-1].close,
            "volume24_usd": row.get("volume24"),
            # 1D regime anchor so a pick hard against the daily is scored down
            "daily_stack": _daily_stack(daily, daily_candles),
            # HTF first (regime before LTF timing)
            "htf": _mini_tf(build_tf_slice(htf, htf_candles), htf),
            "ltf": _mini_tf(build_tf_slice(tf, ltf_candles), tf),
        }
        # Crowdedness context for the funding tiebreaker (audit A9). L-08: the
        # RAW rate is intentionally omitted here -- the prompt only ever reads
        # fundingExtreme/fundingAnnualized (see SCANNER_SYSTEM_PROMPT point 4),
        # so shipping the raw number too was pure token ballast repeated across
        # up to scanner_max_coins (20) coins per scan.
        if isinstance(rate, (int, float)):
            ctx["funding_extreme"] = _funding_extreme(rate)
            ctx["funding_annualized"] = _funding_annualized(rate, None)
        # OI positioning read for the screener (audit I4). Only added when the
        # overview row actually carries OI (null on MEXC / HL cold-start), so it
        # stays graceful and never invents positioning.
        oi_read = _oi_read_label(
            {
                "open_interest": row.get("open_interest"),
                "oi_change_pct_1h": row.get("oi_change_pct_1h"),
            },
            _candles_public(ltf_candles),
        )
        if oi_read is not None:
            ctx["oi_read"] = oi_read
        return ctx

    results = await asyncio.gather(*(_one(r) for r in overview))
    return [r for r in results if r is not None], errors


def _scan_user_prompt(contexts: list[dict[str, Any]]) -> str:
    payload = json.dumps(contexts, ensure_ascii=False, separators=(",", ":"), default=str)
    return (
        "Screen the following coins and return the results JSON.\n\nCOINS:\n" + payload
    )


async def _anthropic_text(
    system: str,
    user: str,
    model: str,
    settings: Settings,
    timeout: float = 120.0,
    *,
    provider_label: str = "Claude",
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
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LlmError(f"Scanner ({provider_label}) request failed: {e}") from e
    if r.status_code >= 400:
        detail: Any = r.text[:400]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(
            _categorize_provider_http_error(provider_label, r.status_code, detail),
            raw=detail,
        )
    payload = r.json()
    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider=provider_label,
        model=model,
        route="scanner",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )
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
    *,
    provider_label: str = "Scanner",
    max_tokens: int = 8000,
) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # max_tokens + temperature MUST be set: without an explicit output budget a
    # reasoning model (e.g. Grok-4) can spend the default budget on reasoning
    # tokens and return an EMPTY content string, which then fails JSON parsing
    # ("Modell … lieferte: ''"). Mirrors the working analyze call in client.py.
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    url = base_url.rstrip("/") + "/chat/completions"

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as c:
            return await c.post(url, headers=headers, json=body)

    t0 = time.monotonic()
    try:
        # O2-14/L2X-12: this call carries production xai/grok scanner traffic
        # — route it through the same transient-retry helper the analyze/
        # reevaluate calls use so a single 502/503/504/429 or client timeout
        # gets one retry instead of failing the whole scan.
        r = await _post_with_retry(_post)
    except httpx.HTTPError as e:
        raise LlmError(f"Scanner ({provider_label}) request failed: {e}") from e
    if r.status_code >= 400:
        detail: Any = r.text[:400]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(
            _categorize_provider_http_error(provider_label, r.status_code, detail),
            raw=detail,
        )
    try:
        payload = r.json()
        choice = payload["choices"][0]
        content = str(choice["message"].get("content") or "")
    except (KeyError, IndexError, TypeError) as e:
        raise LlmError("Scanner response missing content") from e
    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider=provider_label,
        model=model,
        route="scanner",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )
    if not content.strip():
        finish = choice.get("finish_reason") or "?"
        raise LlmError(
            f"Scanner ({provider_label}, {model}) returned EMPTY content "
            f"(finish_reason={finish}). For a reasoning model this usually means "
            "the token budget was exhausted before any answer — raise max_tokens "
            "or use a non-reasoning scanner model."
        )
    return content


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
        text = await _anthropic_text(
            SCANNER_SYSTEM_PROMPT, user, model, settings, provider_label="Claude"
        )
    elif settings.xai_ready:
        model = settings.xai_model
        text = await _openai_compat_text(
            SCANNER_SYSTEM_PROMPT,
            user,
            model,
            settings.xai_base_url,
            settings.xai_api_key,
            provider_label="xAI",
        )
    elif settings.openai_ready:
        model = settings.openai_model
        text = await _openai_compat_text(
            SCANNER_SYSTEM_PROMPT,
            user,
            model,
            settings.openai_base_url,
            settings.openai_api_key,
            provider_label="Codex",
        )
    elif settings.ollama_ready:
        model = settings.ollama_model
        text = await _openai_compat_text(
            SCANNER_SYSTEM_PROMPT,
            user,
            model,
            settings.ollama_base_url,
            "",
            timeout=300.0,
            provider_label="Ollama",
        )
    else:
        raise LlmError("No LLM configured for the scanner")

    # Server-side allowlist: only symbols we actually sent to the LLM may
    # come back (F-22) — a hallucinated-but-valid-looking symbol is dropped.
    allowed_symbols = {
        str(c.get("symbol") or "").upper() for c in contexts if c.get("symbol")
    }
    try:
        return (
            parse_scan_results(
                text, allowed_symbols=allowed_symbols, min_score=SCANNER_MIN_SCORE
            ),
            model,
        )
    except json.JSONDecodeError as e:
        preview = (text or "")[:200].replace("\n", " ")
        raise LlmError(
            f"Scanner-Antwort ist kein JSON ({e}). Modell {model} lieferte: "
            f"{preview!r}",
            raw=text,
        ) from e
