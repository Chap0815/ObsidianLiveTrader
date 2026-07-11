"""LLM client for advisory Trade Proposals (Claude default, xAI optional).

Never places orders. User must apply → preview → confirm; gates re-validate.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.llm.prompts import build_system_prompt, build_user_prompt
from app.models import TradeProposal

_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def strip_markdown_fences(text: str) -> str:
    s = (text or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return s


_VALID_SETUP_CONFIDENCE = {"low", "medium", "high"}


def _normalize_setup_confidence(data: dict) -> dict:
    """Coerce/clear an unexpected setup_confidence value so schema validation
    doesn't hard-fail on a minor LLM formatting slip (e.g. "Low", "n/a").

    Missing the key entirely is already fine — the Pydantic field default
    ("medium") applies. This only touches present-but-invalid values.
    """
    val = data.get("setup_confidence")
    if val is None:
        return data
    if isinstance(val, str) and val.strip().lower() in _VALID_SETUP_CONFIDENCE:
        data["setup_confidence"] = val.strip().lower()
    else:
        data.pop("setup_confidence", None)  # falls back to the model default
    return data


def _find_comma_cut_points(s: str) -> list[tuple[int, list[str]]]:
    """Scan `s` once and record, for every top-level-or-nested comma OUTSIDE a
    string, the index right after it plus a snapshot of the still-open
    container stack (each entry is the matching closer: "}" or "]").

    Used by `salvage_proposal_json` to find safe places to truncate a
    cut-off JSON object and re-close it.
    """
    stack: list[str] = []
    in_str = False
    esc = False
    points: list[tuple[int, list[str]]] = []
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
        elif ch == ",":
            points.append((i + 1, list(stack)))
    return points


def salvage_proposal_json(text: str) -> dict | None:
    """Best-effort recovery for a truncated main-proposal JSON object.

    Mirrors the scanner's `_salvage_result_objects` idea but for a single
    object rather than an array: walks backwards through the last complete
    comma boundary before the cut-off, drops the incomplete trailing
    fragment, re-closes the still-open braces/brackets and retries
    `json.loads`. Returns None if nothing recoverable is found.
    """
    s = strip_markdown_fences(text)
    start = s.find("{")
    if start == -1:
        return None
    s = s[start:]
    for idx, stack in reversed(_find_comma_cut_points(s)):
        body = s[:idx].rstrip()
        if body.endswith(","):
            body = body[:-1]
        candidate = body + "".join(reversed(stack))
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_json_object(text: str) -> str:
    """Return the first balanced top-level {...} JSON object from LLM text.

    Models without a strict json-mode (Sonnet, Grok, Ollama, …) often wrap the
    object in prose or ```fences``` — plain json.loads then fails at char 0.
    This strips fences first, then brace-matches (string/escape aware) so
    surrounding chatter (before or after the object) no longer breaks parsing.
    """
    s = strip_markdown_fences(text)
    start = s.find("{")
    if start == -1:
        return s  # let the caller's json.loads raise a clear error
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s[start:]  # unbalanced → let json.loads report it


def parse_proposal(text: str) -> TradeProposal:
    try:
        data = json.loads(extract_json_object(text))
    except json.JSONDecodeError:
        salvaged = salvage_proposal_json(text)
        if salvaged is None:
            raise
        data = salvaged
    if isinstance(data, dict):
        data = _normalize_setup_confidence(data)
    return TradeProposal.model_validate(data)


def compute_simple_rrr(
    entry: float | None,
    stop: float | None,
    tp1: float | None,
    *,
    action: str | None = None,
) -> float | None:
    """Directional RRR; returns None if SL/TP geometry is invalid for the side.

    abs()-only math would report a positive RRR when SL and TP sit on the same
    side of entry — that misleads the UI. Prefer action-based geometry; if
    action is unknown, require opposite sides of entry.
    """
    if entry is None or stop is None or tp1 is None:
        return None
    e, s, t = float(entry), float(stop), float(tp1)
    act = (action or "").upper()
    if act in ("BUY", "STRONG_BUY"):
        risk = e - s
        reward = t - e
    elif act in ("SELL", "STRONG_SHORT"):
        risk = s - e
        reward = e - t
    else:
        # STAY_OUT / unknown: only accept opposite-side geometry
        if (s - e) * (t - e) >= 0:
            return None
        risk = abs(e - s)
        reward = abs(t - e)
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 4)


def _price_plausibility_reason(
    proposal: TradeProposal, context: dict[str, Any] | None
) -> str | None:
    """Flag hallucinated entry/SL levels that sit implausibly far from
    last_price/ATR. Returns a human-readable reason, or None if plausible
    (or if there isn't enough context to judge).

    Thresholds: |entry - last_price| <= 3x ATR; SL distance from entry
    between 0.3x and 5x ATR. ATR/last_price come from the LTF analysis
    context (the timeframe the LLM sets entry/SL timing from).
    """
    if not context or proposal.action == "STAY_OUT":
        return None
    last_price = context.get("last_price")
    ltf = context.get("ltf") if isinstance(context.get("ltf"), dict) else {}
    read = ltf.get("read") if isinstance(ltf.get("read"), dict) else {}
    atr = read.get("atr14")
    if not isinstance(last_price, (int, float)) or not isinstance(atr, (int, float)):
        return None
    if atr <= 0:
        return None
    entry = proposal.entry_price
    sl = proposal.stop_loss
    reasons: list[str] = []
    if entry is not None:
        dist = abs(float(entry) - float(last_price))
        if dist > 3.0 * atr:
            reasons.append(
                f"entry {entry} is {dist / atr:.1f}x ATR from last_price {last_price}"
            )
    if entry is not None and sl is not None:
        sl_dist = abs(float(entry) - float(sl))
        if sl_dist < 0.3 * atr:
            reasons.append(f"SL distance {sl_dist:.6g} < 0.3x ATR ({atr:.6g})")
        elif sl_dist > 5.0 * atr:
            reasons.append(f"SL distance {sl_dist:.6g} > 5x ATR ({atr:.6g})")
    if not reasons:
        return None
    return "; ".join(reasons)


def annotate_proposal(
    proposal: TradeProposal, context: dict[str, Any] | None = None
) -> TradeProposal:
    computed = compute_simple_rrr(
        proposal.entry_price,
        proposal.stop_loss,
        proposal.tp1,
        action=proposal.action,
    )
    if computed is None:
        # Drop a model-claimed RRR that we cannot recompute from geometry
        if proposal.rrr is not None and proposal.action != "STAY_OUT":
            proposal = proposal.model_copy(update={"rrr": None})
    else:
        proposal = proposal.model_copy(update={"rrr": computed})

    reason = _price_plausibility_reason(proposal, context)
    if reason:
        note = (
            f"Auto-downgraded to STAY_OUT: entry/SL implausible vs "
            f"last_price/ATR ({reason})."
        )
        rationale = f"{proposal.rationale} | {note}" if proposal.rationale else note
        proposal = proposal.model_copy(
            update={
                "action": "STAY_OUT",
                "setup_confidence": "low",
                "entry_price": None,
                "tp1": None,
                "tp2": None,
                "tp3": None,
                "stop_loss": None,
                "rrr": None,
                "rationale": rationale,
            }
        )
    return proposal


def _series_tail(series: Any, k: int = 12) -> list:
    """Last k values of an indicator series, rounded to keep tokens low."""
    if not isinstance(series, list):
        return []
    out = []
    for v in series[-k:]:
        if isinstance(v, (int, float)):
            out.append(round(float(v), 6))
        else:
            out.append(v)
    return out


def _ema_stack_label(last: dict[str, Any], last_close: float | None) -> str:
    """bullish / bearish / mixed from EMA20/50/200 ordering + price location."""
    try:
        e20, e50, e200 = last.get("ema20"), last.get("ema50"), last.get("ema200")
        if e20 is None or e50 is None or e200 is None:
            return "unknown"
        if e20 > e50 > e200 and (last_close is None or last_close > e50):
            return "bullish"
        if e20 < e50 < e200 and (last_close is None or last_close < e50):
            return "bearish"
        return "mixed"
    except TypeError:
        return "unknown"


def compact_tf_for_llm(slice_dict: dict[str, Any], *, recent_bars: int = 60) -> dict[str, Any]:
    candles = slice_dict.get("candles") or []
    indicators = slice_dict.get("indicators") or {}
    structure = slice_dict.get("structure") or {}
    last = (indicators.get("last") if isinstance(indicators, dict) else None) or {}
    tail = candles[-recent_bars:] if len(candles) > recent_bars else candles
    last_close = candles[-1].get("close") if candles else None

    struct_out = {
        "support": structure.get("support"),
        "resistance": structure.get("resistance"),
        "range_high": structure.get("range_high"),
        "range_low": structure.get("range_low"),
        "last_price": structure.get("last_price"),
        "major_pools": (structure.get("major_pools") or [])[:6],
    }
    # Recent swing points: lets the model anchor SL/TP at real structure
    swings = structure.get("swings") or {}
    struct_out["recent_swing_highs"] = [
        {"price": s.get("price"), "time": s.get("time")}
        for s in (swings.get("highs") or [])[-4:]
    ]
    struct_out["recent_swing_lows"] = [
        {"price": s.get("price"), "time": s.get("time")}
        for s in (swings.get("lows") or [])[-4:]
    ]

    # Momentum/vola direction, not just a snapshot
    indicators_tail = {
        "rsi14": _series_tail(indicators.get("rsi14")),
        "macd_hist": _series_tail(indicators.get("macd_hist")),
        "atr14": _series_tail(indicators.get("atr14"), 6),
        "ema20": _series_tail(indicators.get("ema20"), 6),
        "ema50": _series_tail(indicators.get("ema50"), 6),
    }

    read = {
        "ema_stack": _ema_stack_label(last, last_close),
        "atr14": last.get("atr14"),
    }
    try:
        if last_close and last.get("ema20"):
            read["price_vs_ema20_pct"] = round(
                (last_close - last["ema20"]) / last["ema20"] * 100.0, 3
            )
        if last_close and last.get("vwap"):
            read["price_vs_vwap_pct"] = round(
                (last_close - last["vwap"]) / last["vwap"] * 100.0, 3
            )
    except (TypeError, ZeroDivisionError):
        pass

    return {
        "tf": slice_dict.get("tf"),
        "recent_candles": tail,
        "indicators_last": last,
        "indicators_tail": indicators_tail,
        "read": read,
        "structure": struct_out,
    }


def build_llm_context(
    market_api: dict[str, Any],
    account: dict[str, Any] | None,
    settings: Settings,
) -> dict[str, Any]:
    account = account or {}
    include_acct = bool(getattr(settings, "include_account_in_llm", True))
    # HTF first so the model reads regime before LTF timing context
    ctx: dict[str, Any] = {
        "symbol": market_api.get("symbol"),
        "last_price": market_api.get("last_price"),
        "funding": market_api.get("funding") or {},
        "contract": market_api.get("contract") or {},
        "htf": compact_tf_for_llm(market_api.get("htf") or {}),
        "ltf": compact_tf_for_llm(market_api.get("ltf") or {}),
        "risk_policy": {
            "max_leverage": settings.max_leverage,
            "max_risk_pct": settings.max_risk_pct,
            "min_rrr": settings.min_rrr,
            "strict_rrr": settings.strict_rrr,
            "max_notional_usdt": settings.max_notional_usdt,
        },
        "note": (
            "Advisory only. Read htf (regime) before ltf (timing). "
            "Human must apply and pass risk gates before any order. "
            "Prefer STAY_OUT over forced trades."
        ),
    }
    if include_acct:
        ctx["account"] = {
            "equity_usdt": account.get("equity_usdt"),
            "available_usdt": account.get("available_usdt"),
            "positions": account.get("positions") or [],
            "error": account.get("error"),
        }
    else:
        ctx["account"] = {"omitted": True, "reason": "INCLUDE_ACCOUNT_IN_LLM=false"}
    return ctx


class LlmError(Exception):
    def __init__(self, message: str, *, raw: Any = None):
        super().__init__(message)
        self.raw = raw


# Back-compat alias used by older imports/tests
GrokError = LlmError


def _parse_content_to_proposal(
    content: str, *, provider: str, context: dict[str, Any] | None = None
) -> TradeProposal:
    if not content or not str(content).strip():
        raise LlmError(f"{provider} returned empty content")
    try:
        proposal = parse_proposal(str(content))
    except json.JSONDecodeError as e:
        raise LlmError(f"{provider} output is not valid JSON: {e}", raw=content) from e
    except ValidationError as e:
        raise LlmError(
            f"{provider} JSON failed schema validation: {e}", raw=content
        ) from e
    return annotate_proposal(proposal, context)


async def _call_claude(context: dict[str, Any], settings: Settings) -> TradeProposal:
    key = (settings.anthropic_api_key or "").strip()
    if not key:
        raise LlmError("Claude API key not configured (set ANTHROPIC_API_KEY / CLAUDE_API_KEY)")

    url = settings.anthropic_base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": settings.anthropic_version,
        "Content-Type": "application/json",
    }
    # No temperature: Opus 4.8 rejects the deprecated param entirely
    # max_tokens capped: the proposal schema is small and rationale is
    # capped at ~90 words, so 1800 tokens comfortably fits a complete
    # response while shortening how long a stall/truncation can run.
    body: dict[str, Any] = {
        "model": settings.anthropic_model,
        "max_tokens": 1800,
        "system": build_system_prompt(),
        "messages": [
            {"role": "user", "content": build_user_prompt(context)},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LlmError(f"Claude request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:800]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(f"Claude HTTP {r.status_code}: {detail}", raw=detail)

    try:
        payload = r.json()
    except json.JSONDecodeError as e:
        raise LlmError("Claude returned non-JSON response") from e

    # content: [{ "type": "text", "text": "..." }, ...]
    text_parts: list[str] = []
    try:
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        content = "\n".join(text_parts).strip()
    except (TypeError, AttributeError) as e:
        raise LlmError("Claude response missing content blocks", raw=payload) from e

    return _parse_content_to_proposal(content, provider="Claude", context=context)


async def _call_xai(context: dict[str, Any], settings: Settings) -> TradeProposal:
    if not settings.xai_api_key:
        raise LlmError("xAI API key not configured (set XAI_API_KEY)")

    url = settings.xai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.xai_api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": settings.xai_model,
        "max_tokens": 1800,
        "messages": [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_prompt(context)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LlmError(f"xAI request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:500]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(f"xAI HTTP {r.status_code}: {detail}", raw=detail)

    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise LlmError("xAI response missing choices content", raw=getattr(r, "text", None)) from e

    return _parse_content_to_proposal(str(content), provider="xAI", context=context)


async def _call_openai_compat(
    context: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    model: str,
    provider_label: str,
    timeout: float = 120.0,
    json_response_format: bool = True,
) -> TradeProposal:
    """Chat-completions call for OpenAI-compatible APIs (OpenAI, Ollama)."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 1800,
        "messages": [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_prompt(context)},
        ],
        "temperature": 0.2,
    }
    if json_response_format:
        body["response_format"] = {"type": "json_object"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LlmError(f"{provider_label} request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:500]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(f"{provider_label} HTTP {r.status_code}: {detail}", raw=detail)

    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise LlmError(
            f"{provider_label} response missing choices content",
            raw=getattr(r, "text", None),
        ) from e

    return _parse_content_to_proposal(str(content), provider=provider_label, context=context)


async def _call_openai(context: dict[str, Any], settings: Settings) -> TradeProposal:
    if not settings.openai_api_key:
        raise LlmError("OpenAI API key not configured (set OPENAI_API_KEY)")
    return await _call_openai_compat(
        context,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        provider_label="Codex",
    )


async def _call_ollama(context: dict[str, Any], settings: Settings) -> TradeProposal:
    # Local server; slower models need patience. No strict json mode —
    # not every Ollama model supports response_format; parser strips fences.
    return await _call_openai_compat(
        context,
        base_url=settings.ollama_base_url,
        api_key="",
        model=settings.ollama_model,
        provider_label="Ollama",
        timeout=300.0,
        json_response_format=False,
    )


async def analyze_with_llm(context: dict[str, Any], settings: Settings) -> TradeProposal:
    """Dispatch by LLM_PROVIDER: claude | xai | openai (Codex) | ollama."""
    provider = (settings.llm_provider or "claude").strip().lower()
    if provider in ("claude", "anthropic"):
        return await _call_claude(context, settings)
    if provider in ("xai", "grok"):
        return await _call_xai(context, settings)
    if provider in ("openai", "codex"):
        return await _call_openai(context, settings)
    if provider in ("ollama", "local"):
        return await _call_ollama(context, settings)
    raise LlmError(
        f"Unknown LLM_PROVIDER={provider!r} (use claude, xai, openai or ollama)"
    )


# Back-compat name
async def analyze_with_grok(context: dict[str, Any], settings: Settings) -> TradeProposal:
    return await analyze_with_llm(context, settings)
