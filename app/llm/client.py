"""LLM client for advisory Trade Proposals (Claude default, xAI optional).

Never places orders. User must apply → preview → confirm; gates re-validate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.llm.prompts import (
    build_reevaluate_system_prompt,
    build_reevaluate_user_prompt,
    build_system_prompt,
    build_user_prompt,
)
from app.models import ReevaluateProposal, TradeProposal

log = logging.getLogger("app.llm.client")

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
    """Coerce/clear an unexpected setup_confidence / conviction_score value so
    schema validation doesn't hard-fail on a minor LLM formatting slip (e.g.
    "Low", "n/a", a conviction_score of 12 or "7").

    Missing the key entirely is already fine — the Pydantic field default
    applies. This only touches present-but-invalid values.
    """
    val = data.get("setup_confidence")
    if val is not None:
        if isinstance(val, str) and val.strip().lower() in _VALID_SETUP_CONFIDENCE:
            data["setup_confidence"] = val.strip().lower()
        else:
            data.pop("setup_confidence", None)  # falls back to the model default
    # conviction_score (0-10): clamp an in-range-able number, drop anything else
    # so the salvage/free-parse path can't hard-fail on a slip (the xai strict
    # schema already constrains it; this guards the non-schema paths).
    if "conviction_score" in data:
        cs = data.get("conviction_score")
        try:
            data["conviction_score"] = max(0, min(10, int(cs)))
        except (TypeError, ValueError):
            data.pop("conviction_score", None)  # -> model default (None)
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
        log.warning(
            "Proposal JSON recovered via salvage; response was truncated"
        )
        data = salvaged
    if isinstance(data, dict):
        data = _normalize_setup_confidence(data)
    return TradeProposal.model_validate(data)


def _normalize_reevaluate_confidence(data: dict) -> dict:
    """Same slip-tolerant coercion as `_normalize_setup_confidence`, applied
    to the reevaluation's `confidence` field."""
    val = data.get("confidence")
    if val is None:
        return data
    if isinstance(val, str) and val.strip().lower() in _VALID_SETUP_CONFIDENCE:
        data["confidence"] = val.strip().lower()
    else:
        data.pop("confidence", None)  # falls back to the model default
    return data


def parse_reevaluation(text: str) -> ReevaluateProposal:
    try:
        data = json.loads(extract_json_object(text))
    except json.JSONDecodeError:
        salvaged = salvage_proposal_json(text)
        if salvaged is None:
            raise
        log.warning(
            "Reevaluation JSON recovered via salvage; response was truncated"
        )
        data = salvaged
    if isinstance(data, dict):
        data = _normalize_reevaluate_confidence(data)
    return ReevaluateProposal.model_validate(data)


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


_DIRECTIONAL = {"BUY", "STRONG_BUY", "SELL", "STRONG_SHORT"}


def _geometry_inverted(proposal: TradeProposal) -> bool:
    """True when a directional proposal has entry/SL/TP1 all set but their
    geometry is inverted for the side (SL/TP on the wrong side of entry), so
    compute_simple_rrr can't produce a real RRR. Such a call is untradeable
    and must be downgraded to STAY_OUT, not shipped with rrr=null (audit A4)."""
    if proposal.action not in _DIRECTIONAL:
        return False
    if proposal.entry_price is None or proposal.stop_loss is None or proposal.tp1 is None:
        return False
    return (
        compute_simple_rrr(
            proposal.entry_price,
            proposal.stop_loss,
            proposal.tp1,
            action=proposal.action,
        )
        is None
    )


def _price_plausibility_flags(
    proposal: TradeProposal, context: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """Return (hard_reason, warn_reason) for entry/SL sanity vs ATR.

    HARD (auto-STAY_OUT) = a genuinely implausible/hallucinated level: entry
    beyond 8x the reference ATR from last_price, or an SL beyond 8x the
    reference ATR. (SL on the wrong side of entry is a separate, unrelated
    hard check — see `_geometry_inverted` — and is untouched.)

    WARN (keep the trade, cap confidence at "medium", attach a note) = an
    unusually tight SL (<0.3x LTF ATR — a legitimate tight scalp stop, not
    necessarily a hallucination), a moderately far entry (within the (3x, 8x]
    ref-ATR band), or a deep limit entry / wide structural stop that is far in
    *LTF* ATR terms but still within the *HTF* ATR structure band. Entries/
    stops are frequently anchored to HTF/daily structure or to a tight scalp
    thesis, so nuking them straight to STAY_OUT previously killed good
    pullback/limit/scalp setups (audit A5, L-04). The reference ATR scales up
    to HTF/daily ATR when it is larger.
    """
    if not context or proposal.action == "STAY_OUT":
        return None, None
    last_price = context.get("last_price")
    ltf = context.get("ltf") if isinstance(context.get("ltf"), dict) else {}
    ltf_read = ltf.get("read") if isinstance(ltf.get("read"), dict) else {}
    ltf_atr = ltf_read.get("atr14")
    if not isinstance(last_price, (int, float)) or not isinstance(ltf_atr, (int, float)):
        return None, None
    if ltf_atr <= 0:
        return None, None
    htf = context.get("htf") if isinstance(context.get("htf"), dict) else {}
    htf_read = htf.get("read") if isinstance(htf.get("read"), dict) else {}
    htf_atr = htf_read.get("atr14")
    daily = context.get("daily") if isinstance(context.get("daily"), dict) else {}
    daily_read = daily.get("read") if isinstance(daily.get("read"), dict) else {}
    daily_atr = daily_read.get("atr14")
    # Reference ATR for the FAR-entry / wide-SL hard band is the widest of
    # LTF / HTF / DAILY ATR. The system anchors entries+stops to daily
    # structure, so a deep daily-pullback limit measured only against the small
    # LTF/HTF ATR was being hard-downgraded to STAY_OUT (audit B3/I2). The
    # tighter checks (<0.3x LTF ATR SL, the LTF-band WARN) still use LTF ATR.
    ref_atr = float(ltf_atr)
    if isinstance(htf_atr, (int, float)) and htf_atr > ref_atr:
        ref_atr = float(htf_atr)
    if isinstance(daily_atr, (int, float)) and daily_atr > ref_atr:
        ref_atr = float(daily_atr)

    entry = proposal.entry_price
    sl = proposal.stop_loss
    hard: list[str] = []
    warn: list[str] = []
    if entry is not None:
        dist = abs(float(entry) - float(last_price))
        if dist > 8.0 * ref_atr:
            hard.append(
                f"entry {entry} is {dist / ltf_atr:.1f}x LTF ATR "
                f"({dist / ref_atr:.1f}x ref ATR) from last_price {last_price}"
            )
        elif dist > 3.0 * ref_atr:
            # Moderately far entry (L-04): within the (3x, 8x] ref-ATR band —
            # keep it, warn, don't nuke a legitimate deep pullback/limit.
            warn.append(
                f"entry {dist / ref_atr:.1f}x ref ATR from last_price "
                f"(moderate deviation)"
            )
        elif dist > 3.0 * ltf_atr:
            warn.append(
                f"limit entry {dist / ltf_atr:.1f}x LTF ATR from last_price "
                f"(within HTF-ATR structure band)"
            )
    if entry is not None and sl is not None:
        sl_dist = abs(float(entry) - float(sl))
        if sl_dist < 0.3 * ltf_atr:
            # L-04: an unusably tight SL is a legitimate tight-scalp stop as
            # often as a hallucination — warn instead of hard-nuking it.
            warn.append(f"tight SL distance {sl_dist:.6g} < 0.3x LTF ATR ({ltf_atr:.6g})")
        elif sl_dist > 8.0 * ref_atr:
            hard.append(f"SL distance {sl_dist:.6g} > 8x ref ATR ({ref_atr:.6g})")
        elif sl_dist > 5.0 * ltf_atr:
            warn.append(
                f"wide structural SL {sl_dist / ltf_atr:.1f}x LTF ATR "
                f"(within HTF-ATR band)"
            )
    return ("; ".join(hard) or None), ("; ".join(warn) or None)


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
        # Inverted geometry on a directional call: the SL/TP sit on the wrong
        # side of entry, so this is an untradeable proposal — downgrade it to
        # STAY_OUT instead of shipping a BUY/SELL with rrr=null (audit A4).
        if _geometry_inverted(proposal):
            note = (
                "Auto-downgraded to STAY_OUT: directional proposal has inverted "
                "SL/TP geometry (stop/target on the wrong side of entry)."
            )
            rationale = (
                f"{proposal.rationale} | {note}" if proposal.rationale else note
            )
            return proposal.model_copy(
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
        # Otherwise: an incomplete geometry (a missing leg). Drop a
        # model-claimed RRR we cannot recompute, but leave the action alone.
        if proposal.rrr is not None and proposal.action != "STAY_OUT":
            proposal = proposal.model_copy(update={"rrr": None})
    else:
        proposal = proposal.model_copy(update={"rrr": computed})

    hard, warn = _price_plausibility_flags(proposal, context)
    if hard:
        note = (
            f"Auto-downgraded to STAY_OUT: entry/SL implausible vs "
            f"last_price/ATR ({hard})."
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
    elif warn:
        # A good-but-far/tight limit/structural/scalp setup: keep it, but
        # surface the warning and cap confidence at "medium" (L-04) rather
        # than either silently deleting it or hard-flooring it to "low" — a
        # cap only ever lowers confidence, never raises it.
        note = f"Warning: {warn} — verify entry/SL vs current price before acting."
        rationale = f"{proposal.rationale} | {note}" if proposal.rationale else note
        capped_confidence = (
            "low" if proposal.setup_confidence == "low" else "medium"
        )
        proposal = proposal.model_copy(
            update={"setup_confidence": capped_confidence, "rationale": rationale}
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
    """bullish / bearish / mixed from EMA20/50/200 ordering + regime line.

    M2-03: the price gate is the EMA200 (the major regime line), NOT the EMA50.
    A healthy pullback INTO value — price below EMA50 but still above EMA200,
    EMAs themselves still stacked bullish — is the BEST entry, not a regime
    breakdown; the old `last_close > e50` gate flipped it to "mixed" exactly
    there. Gating on EMA200 keeps such a pullback "bullish" while still turning
    the label off once price loses the EMA200 (a genuine regime break the lagging
    EMA order alone would miss for many bars). Chosen over a separate
    `price_location` field to keep the change surgical and prompt-neutral.
    """
    try:
        e20, e50, e200 = last.get("ema20"), last.get("ema50"), last.get("ema200")
        if e20 is None or e50 is None or e200 is None:
            return "unknown"
        if e20 > e50 > e200 and (last_close is None or last_close > e200):
            return "bullish"
        if e20 < e50 < e200 and (last_close is None or last_close < e200):
            return "bearish"
        return "mixed"
    except TypeError:
        return "unknown"


def compact_tf_for_llm(slice_dict: dict[str, Any], *, recent_bars: int = 30) -> dict[str, Any]:
    candles = slice_dict.get("candles") or []
    indicators = slice_dict.get("indicators") or {}
    structure = slice_dict.get("structure") or {}
    last = (indicators.get("last") if isinstance(indicators, dict) else None) or {}
    tail = candles[-recent_bars:] if len(candles) > recent_bars else candles
    # Drop the quote-turnover `amount` from each recent candle (O1): the prompt
    # reads OHLC + vol only — momentum/volume come from read.rvol / vol_trend /
    # indicators_tail — so `amount` was the single biggest chunk of pure input-
    # token waste. OHLCV is preserved unchanged, so analysis quality is unaffected.
    recent_candles = [
        {k: v for k, v in c.items() if k != "amount"} if isinstance(c, dict) else c
        for c in tail
    ]
    # Read-labels (ema_stack, price_vs_ema20/vwap) must NOT repaint intra-candle
    # (Task 16 / M2-01): anchor them to the same CLOSED bar the indicator bundle
    # was computed against (`as_of_close`). recent_candles above deliberately
    # keep the live bar; only the derived labels use the closed close. Fall back
    # to the last candle's close for manually-built slices without the marker.
    last_close = indicators.get("as_of_close")
    if last_close is None:
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

    # Momentum/vola direction, not just a snapshot. EMA20/50 tails are
    # deliberately omitted here — the `ema_stack` label + `price_vs_ema20_pct`
    # below already cover EMA positioning, so raw EMA history would just be
    # redundant tokens. RSI/macd_hist stay: they carry real momentum direction
    # that a single last-value snapshot can't show.
    indicators_tail = {
        "rsi14": _series_tail(indicators.get("rsi14")),
        "macd_hist": _series_tail(indicators.get("macd_hist")),
        "atr14": _series_tail(indicators.get("atr14"), 6),
    }

    read = {
        "ema_stack": _ema_stack_label(last, last_close),
        "atr14": last.get("atr14"),
        "rvol": indicators.get("rvol"),
        "vol_trend": indicators.get("vol_trend"),
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

    # `indicators_last` (the full EMA/vwap/atr snapshot) is intentionally NOT
    # emitted: the prompt only ever reads `read.*`, `indicators_tail` and
    # `structure`, so shipping the raw last-dict was pure token cost plus a
    # double-read hazard against the derived `read` labels (audit A8).
    return {
        "tf": slice_dict.get("tf"),
        "recent_candles": recent_candles,
        # K2-03: recent_candles KEEP the still-forming live bar (current price
        # action) while every indicator/read-label was computed on closed bars.
        # This marker tells the model the last recent candle is not yet closed
        # and gives the timestamp of the last CLOSED bar as the analysis anchor.
        "bar_progress": {
            "last_bar_forming": bool(indicators.get("live_bar_dropped")),
            "closed_as_of": indicators.get("as_of_time"),
        },
        "indicators_tail": indicators_tail,
        "read": read,
        "structure": struct_out,
    }


def compact_daily_for_llm(slice_dict: dict[str, Any]) -> dict[str, Any]:
    """Ultra-compact daily REGIME anchor: ONLY read + last 3 swings per side.

    No recent_candles, no indicator tails — deliberately ~150 tokens. Mirrors the
    `read` computation of compact_tf_for_llm but drops everything the anchor
    doesn't need (the daily block sets regime bias, not entry timing)."""
    indicators = slice_dict.get("indicators") or {}
    structure = slice_dict.get("structure") or {}
    candles = slice_dict.get("candles") or []
    last = (indicators.get("last") if isinstance(indicators, dict) else None) or {}
    # Anchor the daily regime read to the CLOSED bar (Task 16) — a still-forming
    # daily candle must not repaint the regime label. Fall back to the last
    # candle for manually-built slices without the closed-bar marker.
    last_close = indicators.get("as_of_close")
    if last_close is None:
        last_close = candles[-1].get("close") if candles else None

    read: dict[str, Any] = {
        "ema_stack": _ema_stack_label(last, last_close),
        # Daily ATR feeds the plausibility reference band so a legitimate deep
        # daily-anchored pullback entry/stop isn't auto-nuked to STAY_OUT when
        # it exceeds the small LTF/HTF ATR band (audit B3/I2).
        "atr14": last.get("atr14"),
        "rvol": indicators.get("rvol"),
    }
    try:
        if last_close and last.get("ema20"):
            read["price_vs_ema20_pct"] = round(
                (last_close - last["ema20"]) / last["ema20"] * 100.0, 3
            )
    except (TypeError, ZeroDivisionError):
        pass

    swings = structure.get("swings") or {}
    return {
        "tf": slice_dict.get("tf"),
        "read": read,
        "recent_swing_highs": [
            {"price": s.get("price"), "time": s.get("time")}
            for s in (swings.get("highs") or [])[-3:]
        ],
        "recent_swing_lows": [
            {"price": s.get("price"), "time": s.get("time")}
            for s in (swings.get("lows") or [])[-3:]
        ],
    }


def _regime_alignment(daily_s: str, htf_s: str, ltf_s: str) -> str:
    """Coarse daily/htf/ltf ema_stack agreement label.

    "conflict" when the daily and htf regimes are both directional but
    opposite (a lower-timeframe trade would be against the daily anchor);
    "aligned_bull"/"aligned_bear" when htf+ltf agree and daily doesn't oppose;
    "mixed" otherwise.
    """
    directional = {"bullish", "bearish"}
    if daily_s in directional and htf_s in directional and daily_s != htf_s:
        return "conflict"
    if htf_s == "bullish" and ltf_s == "bullish" and daily_s != "bearish":
        return "aligned_bull"
    if htf_s == "bearish" and ltf_s == "bearish" and daily_s != "bullish":
        return "aligned_bear"
    return "mixed"


def _coherence_hint(
    daily_c: dict[str, Any], htf_c: dict[str, Any], ltf_c: dict[str, Any]
) -> dict[str, Any]:
    """Server-computed regime-coherence block so the model verifies against
    precomputed facts instead of re-deriving ema_stack agreement from arrays."""
    d = (daily_c.get("read") or {}).get("ema_stack", "unknown")
    h = (htf_c.get("read") or {}).get("ema_stack", "unknown")
    l = (ltf_c.get("read") or {}).get("ema_stack", "unknown")
    return {
        "ema_stack_daily": d,
        "ema_stack_htf": h,
        "ema_stack_ltf": l,
        "regime_alignment": _regime_alignment(d, h, l),
        "ltf_stretch_pct": (ltf_c.get("read") or {}).get("price_vs_ema20_pct"),
    }


def _price_dir_recent(candles: list[Any] | None, lookback: int) -> str | None:
    """Direction of the last `lookback` closes: 'up' / 'down' / 'flat'."""
    if not candles or len(candles) <= lookback:
        return None
    try:
        now = candles[-1].get("close")
        past = candles[-1 - lookback].get("close")
    except (AttributeError, TypeError, IndexError):
        return None
    if not isinstance(now, (int, float)) or not isinstance(past, (int, float)) or past == 0:
        return None
    chg = (now - past) / past
    if chg > 0.001:
        return "up"
    if chg < -0.001:
        return "down"
    return "flat"


def _oi_read_label(market: dict[str, Any], ltf_candles: list[Any] | None) -> str | None:
    """Precompute the price<->OI positioning label when OI is present.

    Pairs the ~1h price direction (4x 15m closes) with oi_change_pct_1h so the
    model consumes a ready signal (audit B5b) instead of re-deriving it. Returns
    None when OI or the change is absent (MEXC / HL cold-start) or price is flat.
    """
    oi = market.get("open_interest")
    oi_chg = market.get("oi_change_pct_1h")
    if oi is None or not isinstance(oi_chg, (int, float)):
        return None
    price_dir = _price_dir_recent(ltf_candles, 4)
    if price_dir not in ("up", "down"):
        return None
    oi_dir = "up" if oi_chg > 0 else ("down" if oi_chg < 0 else "flat")
    if oi_dir == "flat":
        return None
    table = {
        ("up", "up"): "price_up_oi_up_real_trend",
        ("up", "down"): "price_up_oi_down_short_covering",
        ("down", "up"): "price_down_oi_up_new_shorts",
        ("down", "down"): "price_down_oi_down_long_liquidation",
    }
    return table.get((price_dir, oi_dir))


def _sanitize_scanner_verdict(raw: Any) -> dict[str, Any] | None:
    """Keep only the fields the analyzer should confirm/refute, coerced to
    safe types. Returns None when nothing usable is present."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    bias = raw.get("bias")
    if isinstance(bias, str) and bias.strip().lower() in ("long", "short"):
        out["bias"] = bias.strip().lower()
    setup = raw.get("setup") or raw.get("setup_type")
    if isinstance(setup, str) and setup.strip():
        out["setup"] = setup.strip()
    kl = raw.get("key_level")
    if isinstance(kl, (int, float)):
        out["key_level"] = kl
    score = raw.get("score")
    if isinstance(score, (int, float)):
        out["score"] = score
    # S2-04: the screener's own rationale for the pick — without it the
    # analyzer re-derives blind and can't see WHY the coin was flagged.
    # Truncated to ~120 chars: it is LLM-origin free text, not a structured
    # field, so it's a hint for the analyzer prompt, not a contract value.
    reason = raw.get("reason")
    if isinstance(reason, str) and reason.strip():
        out["reason"] = reason.strip()[:120]
    return out or None


# The prompt (app/llm/prompts.py, steps 6-7) references ONLY these funding
# fields: fundingRate (the raw per-interval rate), fundingAnnualized and
# fundingExtreme. The rest of the API funding dict (symbol, maxFundingRate,
# minFundingRate, collectCycle, nextSettleTime, timestamp) is never read by the
# model, so the LLM copy keeps just these three (O1). The full dict still flows
# to the UI via snapshot_to_api_dict — this trims only the LLM context.
_LLM_FUNDING_KEYS = ("fundingRate", "fundingAnnualized", "fundingExtreme")


def _compact_funding_for_llm(funding: dict[str, Any] | None) -> dict[str, Any]:
    src = funding or {}
    return {k: src[k] for k in _LLM_FUNDING_KEYS if k in src}


def build_llm_context(
    market_api: dict[str, Any],
    account: dict[str, Any] | None,
    settings: Settings,
    scanner_verdict: dict[str, Any] | None = None,
    market_regime: dict[str, Any] | None = None,
    track_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account = account or {}
    # F-15 (privacy): default to false (opt-in) — a missing attribute must
    # fail closed toward NOT leaking account data, not toward sending it.
    include_acct = bool(getattr(settings, "include_account_in_llm", False))
    src_market = market_api.get("market") or {}
    daily_c = compact_daily_for_llm(market_api.get("daily") or {})
    htf_c = compact_tf_for_llm(market_api.get("htf") or {})
    ltf_c = compact_tf_for_llm(market_api.get("ltf") or {})
    # K2-05: `premium` is dead ballast (never read by the prompt) and is dropped.
    # OI fields are emitted ONLY when open_interest is actually present (null on
    # MEXC / HL cold-start), so the block is empty rather than a row of nulls.
    market_block: dict[str, Any] = {}
    if src_market.get("open_interest") is not None:
        market_block["open_interest"] = src_market.get("open_interest")
        market_block["oi_change_pct_1h"] = src_market.get("oi_change_pct_1h")
        market_block["oi_change_pct_4h"] = src_market.get("oi_change_pct_4h")
    # Precompute the price<->OI positioning label when OI is actually present
    # (null on MEXC / HL cold-start -> label omitted, no wasted tokens).
    oi_read = _oi_read_label(src_market, (market_api.get("ltf") or {}).get("candles"))
    if oi_read is not None:
        market_block["oi_read"] = oi_read
    # Order: daily (regime anchor) first, then htf (regime), then ltf (timing)
    # `contract` is intentionally NOT included (O1): the prompt sets leverage
    # from risk_policy.max_leverage and never reads contract.*; annotate_proposal
    # doesn't touch it either. The block flowed to the UI via a separate path
    # (snapshot_to_api_dict), so dropping it here only trims LLM input tokens.
    ctx: dict[str, Any] = {
        "symbol": market_api.get("symbol"),
        "last_price": market_api.get("last_price"),
        "funding": _compact_funding_for_llm(market_api.get("funding")),
        "market": market_block,
        "daily": daily_c,
        "htf": htf_c,
        "ltf": ltf_c,
        "coherence": _coherence_hint(daily_c, htf_c, ltf_c),
        "risk_policy": {
            "max_leverage": settings.max_leverage,
            "max_risk_pct": settings.max_risk_pct,
            "min_rrr": settings.min_rrr,
            "strict_rrr": settings.strict_rrr,
            "max_notional_usdt": settings.max_notional_usdt,
        },
        "note": (
            "Advisory only. Read daily (regime anchor), then htf (regime), then "
            "ltf (timing). Human must apply and pass risk gates before any order. "
            "Follow the system prompt's decision_policy for action vs STAY_OUT "
            "and for SIZE TO CONVICTION sizing."
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

    # Close the scanner->analyzer handoff loop (audit B1): thread the fast
    # screener's verdict into the deep context so the analyzer CONFIRMS or
    # REFUTES it (and, on STAY_OUT, names the failed gate) instead of
    # re-deriving cold and silently disagreeing.
    verdict = _sanitize_scanner_verdict(scanner_verdict)
    if verdict is not None:
        ctx["scanner_verdict"] = verdict

    # K2-02: BTC beta is the dominant factor for altcoins. When a cached BTC
    # regime anchor is supplied (omitted when analysing BTC itself, or when the
    # BTC fetch failed), surface it so the prompt can CAP setup_confidence on a
    # clearly opposite BTC regime (never a hard veto — Anti-Overtrading-konform).
    if market_regime:
        ctx["market_regime"] = market_regime

    # Task 21 (K2-01/F2-01): close the learn-loop — surface the KI's OWN recent
    # shadow-book track record (overall net + by_confidence + by_setup, each with
    # n and the Wilson LOWER bound) so the model can CALIBRATE its confidence by
    # its own hit rate on this setup type. Present only when the caller supplied
    # a block (gated on n >= journal_min_sample upstream). Advisory calibration
    # hint, NEVER a veto/threshold — see _TRACK_RECORD_RULE in prompts.py.
    if track_record:
        ctx["track_record"] = track_record
    return ctx


def build_original_thesis(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    """Task 21 (O2-06): extract the ORIGINAL proposal's core fields for the
    reevaluate context — the consistency anchor the model compares CURRENT
    structure against, instead of re-deriving the thesis cold.

    Returns None when there is no usable directional thesis (no proposal, a
    STAY_OUT, or a proposal without an entry level — none of those ever opened a
    position, so there is nothing to hold the current structure against).
    """
    if not isinstance(proposal, dict) or not proposal:
        return None
    action = str(proposal.get("action") or "").upper()
    if action == "STAY_OUT" or proposal.get("entry_price") is None:
        return None
    return {
        "action": proposal.get("action"),
        "setup_confidence": proposal.get("setup_confidence"),
        "chart_pattern": proposal.get("chart_pattern"),
        "entry_price": proposal.get("entry_price"),
        "stop_loss": proposal.get("stop_loss"),
        "tp1": proposal.get("tp1"),
        "rationale": proposal.get("rationale"),
    }


class LlmError(Exception):
    def __init__(self, message: str, *, raw: Any = None):
        super().__init__(message)
        self.raw = raw


# Back-compat alias used by older imports/tests
GrokError = LlmError


# --- Provider HTTP error categorization (credit/rate-limit UX) ---
# Applied on every provider HTTP-error path below (analyze AND reevaluate,
# all providers) so the frontend can show a clean, actionable German message
# instead of a raw "Claude HTTP 403: {...}" dump. Only the response BODY is
# ever inspected here — request headers (which carry the API key) never are,
# so a raw key/secret can't leak into the message.
# Tightened (not a bare "credit") so an unrelated substring like "credential"
# doesn't false-positive; still matches the real credits/billing phrasing
# providers actually send.
_CREDIT_ERROR_KEYWORDS = (
    "out of credit",
    "used all available credit",
    "no credits left",
    "insufficient credit",
    "spending limit",
    "spending_limit",
    # NICHT "permission denied": das ist die typische Key-Berechtigungs-/
    # Region-Phrase (L-11) — Aufladen wuerde daran nichts aendern; sie muss
    # in die generische 403-Meldung laufen, nicht in "Credits erschoepft".
    "insufficient_quota",
    "insufficient quota",
)
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
)


def _categorize_provider_http_error(
    provider_label: str, status_code: int, detail: Any
) -> str:
    """Turn a provider HTTP error response into a clean German LlmError
    message when it looks like an auth, credits/limit, or rate-limit
    condition; otherwise keep the existing detailed
    "{Provider} HTTP {code}: {detail}" message unchanged.

    401 (auth: bad/missing key) is intentionally split from 403 (billing):
    conflating them would tell a trader with a broken key to "top up
    credits" when the fix is actually to check the key.

    L-11: 403 alone is NOT sufficient evidence of "credits erschöpft" — some
    providers/proxies return 403 for key-permission or region-lockout
    problems that topping up credits won't fix. Only report the credits
    message for a 403 when the body actually carries a
    `_CREDIT_ERROR_KEYWORDS` signal; otherwise report a generic
    "Zugriff verweigert" message pointing at key permissions/region."""
    try:
        body_text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    except (TypeError, ValueError):
        body_text = str(detail)
    low = (body_text or "").lower()
    if status_code == 401:
        return (
            f"⚠ {provider_label}: API-Key ungültig oder fehlt — "
            "Key prüfen oder KI wechseln."
        )
    if status_code == 403:
        if any(k in low for k in _CREDIT_ERROR_KEYWORDS):
            return (
                f"⚠ {provider_label}: Credits erschöpft oder Limit erreicht — "
                "KI im Dropdown wechseln oder aufladen."
            )
        return (
            f"⚠ {provider_label}: Zugriff verweigert — "
            "Key-Berechtigungen/Region prüfen."
        )
    if any(k in low for k in _CREDIT_ERROR_KEYWORDS):
        return (
            f"⚠ {provider_label}: Credits erschöpft oder Limit erreicht — "
            "KI im Dropdown wechseln oder aufladen."
        )
    if status_code == 429 or any(k in low for k in _RATE_LIMIT_KEYWORDS):
        return f"⚠ {provider_label}: Rate-Limit — kurz warten oder KI wechseln."
    return f"{provider_label} HTTP {status_code}: {detail}"


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


def _parse_content_to_reevaluation(content: str, *, provider: str) -> ReevaluateProposal:
    if not content or not str(content).strip():
        raise LlmError(f"{provider} returned empty content")
    try:
        return parse_reevaluation(str(content))
    except json.JSONDecodeError as e:
        raise LlmError(f"{provider} output is not valid JSON: {e}", raw=content) from e
    except ValidationError as e:
        raise LlmError(
            f"{provider} JSON failed schema validation: {e}", raw=content
        ) from e


# L-09: transient-failure retry for the advisory provider POST. These calls
# are read-only/advisory (never place, move or close anything), so a single
# retry is safe and can't double-act. Only truly transient conditions retry —
# a substantive failure (4xx auth/schema, non-timeout connection errors)
# raises straight through on the first attempt, and even a transient one
# retries at most ONCE, so a persistently-down provider still fails in
# bounded time instead of silently doubling the caller's timeout budget.
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_RETRY_BACKOFF_SECONDS = 0.5


def _log_llm_metrics(
    *,
    provider: str,
    model: str | None,
    route: str,
    elapsed_ms: float,
    payload: dict[str, Any] | None,
) -> None:
    """Emit exactly ONE greppable `LLM_METRICS` INFO line per LLM call with
    latency + usage/cache/finish_reason, read defensively from either the
    OpenAI-shaped response (xai/openai/ollama: choices[0].finish_reason,
    usage.prompt_tokens/completion_tokens, usage.completion_tokens_details.
    reasoning_tokens, usage.prompt_tokens_details.cached_tokens) or the
    Anthropic-shaped response (claude: top-level stop_reason, usage.
    input_tokens/output_tokens/cache_read_input_tokens/
    cache_creation_input_tokens).

    Purely observational (O2-02/L2X-03/O2-04): never raises — a missing or
    unexpected `usage`/`choices` shape just logs fewer fields instead of
    breaking the advisory call that triggered it.
    """
    try:
        payload = payload or {}
        usage = payload.get("usage") or {}

        # Anthropic shape
        stop_reason = payload.get("stop_reason")
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_read_tokens = usage.get("cache_read_input_tokens")
        cache_creation_tokens = usage.get("cache_creation_input_tokens")

        # OpenAI-shaped (xai/openai/ollama)
        finish_reason = None
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice0 = choices[0] if isinstance(choices[0], dict) else {}
            finish_reason = choice0.get("finish_reason")
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        completion_details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = completion_details.get("reasoning_tokens")
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tokens = prompt_details.get("cached_tokens")

        log.info(
            "LLM_METRICS provider=%s model=%s route=%s elapsed_ms=%.0f "
            "input_tokens=%s output_tokens=%s prompt_tokens=%s "
            "completion_tokens=%s reasoning_tokens=%s cached_tokens=%s "
            "cache_read_input_tokens=%s cache_creation_input_tokens=%s "
            "finish_reason=%s stop_reason=%s",
            provider,
            model,
            route,
            elapsed_ms,
            input_tokens,
            output_tokens,
            prompt_tokens,
            completion_tokens,
            reasoning_tokens,
            cached_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            finish_reason,
            stop_reason,
        )
    except Exception:
        # Metrics logging must never break an advisory LLM call.
        log.debug("LLM_METRICS logging failed", exc_info=True)


async def _post_with_retry(
    post: Callable[[], Awaitable[httpx.Response]],
) -> httpx.Response:
    """Call `post()`; on a transient failure (429/502/503/504 response, or a
    client-side httpx.TimeoutException) wait a short backoff and retry
    exactly once. Any other outcome (a non-transient status code, or a
    non-timeout httpx error) is returned/raised immediately from the first
    attempt without retrying."""
    try:
        response = await post()
    except httpx.TimeoutException:
        await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        return await post()
    if response.status_code in _RETRYABLE_STATUS_CODES:
        await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        return await post()
    return response


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
    system_prompt = build_system_prompt(context)
    user_prompt = build_user_prompt(context)

    # Extended thinking for the deep-analysis call only (the scanner uses a
    # smaller/cheaper model and stays fast — no thinking there).
    #
    # settings.anthropic_model defaults to "claude-opus-4-8". On the current
    # Opus 4.6+ / Sonnet 4.6+ family the old `thinking.budget_tokens` field
    # is REJECTED (400) — the only supported "on" mode is adaptive thinking,
    # with depth controlled by `output_config.effort` instead of a token
    # count. "medium" effort approximates the modest amount of extra
    # reasoning a ~3000-token budget would have given on older models,
    # without jumping to the (slower/pricier) "high" default.
    # max_tokens is raised 1800 -> 16000: thinking + the JSON response share
    # the same max_tokens budget, so a smaller cap risks the reasoning phase
    # consuming the whole budget on a large context and truncating (or
    # emptying) the final JSON answer, which surfaces as a confusing
    # LlmError instead of a usable proposal. 16000 gives adaptive thinking
    # real headroom while still comfortably inside this non-streaming
    # request's 120s httpx timeout for these models.
    # O2: wrap the large static analyze system prompt as a cacheable block so
    # repeat analyzes within the 5-min TTL reuse cached input tokens (Anthropic
    # only; cache_control is GA, no beta header needed). The volatile per-coin
    # JSON context lives in the user turn AFTER this breakpoint, so ordering is
    # correct. Graceful by design: if the prompt is below the cache minimum the
    # API simply doesn't cache it — no error. Content is unchanged.
    system_block = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    body: dict[str, Any] = {
        "model": settings.anthropic_model,
        "max_tokens": 16000,
        "system": system_block,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
    }

    async def _post(payload: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=120.0) as client:
            return await client.post(url, headers=headers, json=payload)

    sent_body = body
    t0 = time.monotonic()
    try:
        r = await _post_with_retry(lambda: _post(body))
    except httpx.HTTPError as e:
        raise LlmError(f"Claude request failed: {e}") from e

    # Defensive fallback: if this API version/account/model combination
    # rejects the thinking/output_config request shape (e.g. a pinned older
    # model via ANTHROPIC_MODEL that doesn't support adaptive thinking),
    # retry once with the plain pre-thinking request rather than hard-
    # failing the whole analysis.
    if r.status_code == 400:
        low = r.text.lower()
        if "thinking" in low or "output_config" in low or "effort" in low:
            fallback_body: dict[str, Any] = {
                "model": settings.anthropic_model,
                "max_tokens": 1800,
                "system": system_block,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
            }
            sent_body = fallback_body
            try:
                r = await _post_with_retry(lambda: _post(fallback_body))
            except httpx.HTTPError as e:
                raise LlmError(f"Claude request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:800]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(_categorize_provider_http_error("Claude", r.status_code, detail), raw=detail)

    try:
        payload = r.json()
    except json.JSONDecodeError as e:
        raise LlmError("Claude returned non-JSON response") from e

    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider="Claude",
        model=sent_body.get("model"),
        route="analyze",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )

    if payload.get("stop_reason") == "max_tokens":
        log.warning(
            "Claude deep-analysis call hit max_tokens (%s); response may be truncated",
            sent_body.get("max_tokens"),
        )

    # content: [{ "type": "thinking", "thinking": "..." }, { "type": "text",
    # "text": "..." }, ...] when extended thinking is enabled — the thinking
    # block always precedes the final text block. Only "text" blocks are
    # joined here, so thinking content is never fed into the JSON parser.
    text_parts: list[str] = []
    try:
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        content = "\n".join(text_parts).strip()
    except (TypeError, AttributeError) as e:
        raise LlmError("Claude response missing content blocks", raw=payload) from e

    return _parse_content_to_proposal(content, provider="Claude", context=context)


def _strictify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Post-process a Pydantic `model_json_schema()` output in place so it
    satisfies xAI/OpenAI-style `strict: true` Structured Outputs.

    `model_json_schema()` does NOT set `additionalProperties: false` and
    does NOT list Optional fields in `required` (it just omits fields that
    have a default). grok's `strict: true` REQUIRES both — omitting either
    makes every call answer HTTP 400. This recurses into every dict/list
    reachable from the root (covering `properties`, `$defs`/`definitions`,
    `items`, `anyOf`/`oneOf`/`allOf` branches, ...) and, for every node that
    looks like an object schema (has a `properties` key), sets
    `additionalProperties: false` and `required` to ALL of that object's own
    property names. Optionality is therefore expressed the way Pydantic
    already expresses it for nullable fields — a `type`/`anyOf` branch that
    includes `"null"` — never by omission from `required`.
    """

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return schema


async def _call_xai(context: dict[str, Any], settings: Settings) -> TradeProposal:
    if not settings.xai_api_key:
        raise LlmError("xAI API key not configured (set XAI_API_KEY)")

    url = settings.xai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.xai_api_key}",
        "Content-Type": "application/json",
    }
    # O2-13: schema generated from the Pydantic model at call time, so a
    # future field addition to TradeProposal flows through automatically —
    # then strictified (see _strictify_schema) because grok's strict:true
    # needs additionalProperties:false + full `required` everywhere.
    schema = _strictify_schema(TradeProposal.model_json_schema())
    body: dict[str, Any] = {
        "model": settings.xai_model,
        # O2-12/L2X-11: grok-4 is always-on-reasoning — reasoning tokens are
        # billed against and consumed from max_tokens BEFORE the JSON answer
        # is emitted. 1800 let a normal reasoning pass silently exhaust the
        # whole budget, returning empty/truncated content on the money path.
        # 10000 is a runaway cap, not a target: actual cost tracks real token
        # usage, not this ceiling. Mirrors scanner.py's 8000 for the same
        # documented failure mode.
        "max_tokens": 10000,
        "messages": [
            {"role": "system", "content": build_system_prompt(context)},
            {"role": "user", "content": build_user_prompt(context)},
        ],
        "temperature": 0.0,
        # O2-13: Structured Outputs — grok enforces the schema/enums
        # server-side, so the parse layer's salvage/coercion becomes a pure
        # truncation net instead of the primary correctness backstop.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "trade_proposal", "schema": schema, "strict": True},
        },
        # O2-14: do NOT add "reasoning_effort" here — grok-4 rejects the
        # param with HTTP 400; only grok-3-mini accepts it. Don't add it
        # without first gating on the configured model.
    }

    async def _post(payload: dict[str, Any]) -> httpx.Response:
        # Was 90s — inverted vs. Claude's 120s despite grok being the faster
        # model; raised to give the larger max_tokens budget above room to
        # complete without a spurious client-side timeout.
        async with httpx.AsyncClient(timeout=120.0) as client:
            return await client.post(url, headers=headers, json=payload)

    sent_body = body
    t0 = time.monotonic()
    try:
        r = await _post_with_retry(lambda: _post(body))
    except httpx.HTTPError as e:
        raise LlmError(f"xAI request failed: {e}") from e

    # O2-13: json_schema/strict is the primary path, but stays a safety-
    # netted addition. If this account/model/proxy combination rejects the
    # json_schema request shape, retry once with the old plain json_object
    # mode rather than hard-failing the whole analysis (mirrors
    # _call_claude's thinking->plain 400-fallback above).
    if r.status_code == 400:
        fallback_body: dict[str, Any] = {**body, "response_format": {"type": "json_object"}}
        sent_body = fallback_body
        try:
            r = await _post_with_retry(lambda: _post(fallback_body))
        except httpx.HTTPError as e:
            raise LlmError(f"xAI request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:500]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(_categorize_provider_http_error("xAI", r.status_code, detail), raw=detail)

    try:
        payload = r.json()
        choice0 = payload["choices"][0]
        content = choice0["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise LlmError("xAI response missing choices content", raw=getattr(r, "text", None)) from e

    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider="xAI",
        model=sent_body.get("model"),
        route="analyze",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )

    finish_reason = choice0.get("finish_reason") if isinstance(choice0, dict) else None
    if finish_reason == "length":
        # Mirrors the Claude stop_reason==max_tokens warning above: make a
        # truncated/empty response visible instead of a silent bad proposal.
        log.warning(
            "xAI analyze call hit max_tokens (finish_reason=length, max_tokens=%s); "
            "response may be truncated",
            sent_body.get("max_tokens"),
        )
        if not str(content or "").strip():
            raise LlmError(
                "xAI: Antwort abgeschnitten — Token-Budget erschöpft "
                f"(finish_reason=length, max_tokens={sent_body['max_tokens']})"
            )

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
            {"role": "system", "content": build_system_prompt(context)},
            {"role": "user", "content": build_user_prompt(context)},
        ],
        "temperature": 0.0,
    }
    if json_response_format:
        body["response_format"] = {"type": "json_object"}

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, headers=headers, json=body)

    t0 = time.monotonic()
    try:
        r = await _post_with_retry(_post)
    except httpx.HTTPError as e:
        raise LlmError(f"{provider_label} request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:500]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(_categorize_provider_http_error(provider_label, r.status_code, detail), raw=detail)

    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise LlmError(
            f"{provider_label} response missing choices content",
            raw=getattr(r, "text", None),
        ) from e

    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider=provider_label,
        model=model,
        route="analyze",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )

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


# --- Reevaluate (advisory review of an ALREADY OPEN position) ---
# Mirrors the analyze_with_llm dispatch above, but talks the reevaluate
# system/user prompts and parses into ReevaluateProposal. Never places,
# modifies or closes anything — advisory JSON only, same as analyze.


async def _call_claude_reevaluate(
    context: dict[str, Any], settings: Settings
) -> ReevaluateProposal:
    key = (settings.anthropic_api_key or "").strip()
    if not key:
        raise LlmError("Claude API key not configured (set ANTHROPIC_API_KEY / CLAUDE_API_KEY)")

    url = settings.anthropic_base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": settings.anthropic_version,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": settings.anthropic_model,
        "max_tokens": 1200,
        "system": build_reevaluate_system_prompt(context),
        "messages": [
            {"role": "user", "content": build_reevaluate_user_prompt(context)},
        ],
    }

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=120.0) as client:
            return await client.post(url, headers=headers, json=body)

    t0 = time.monotonic()
    try:
        r = await _post_with_retry(_post)
    except httpx.HTTPError as e:
        raise LlmError(f"Claude request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:800]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(_categorize_provider_http_error("Claude", r.status_code, detail), raw=detail)

    try:
        payload = r.json()
    except json.JSONDecodeError as e:
        raise LlmError("Claude returned non-JSON response") from e

    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider="Claude",
        model=body.get("model"),
        route="reevaluate",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )

    text_parts: list[str] = []
    try:
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        content = "\n".join(text_parts).strip()
    except (TypeError, AttributeError) as e:
        raise LlmError("Claude response missing content blocks", raw=payload) from e

    return _parse_content_to_reevaluation(content, provider="Claude")


async def _call_xai_reevaluate(
    context: dict[str, Any], settings: Settings
) -> ReevaluateProposal:
    if not settings.xai_api_key:
        raise LlmError("xAI API key not configured (set XAI_API_KEY)")

    url = settings.xai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.xai_api_key}",
        "Content-Type": "application/json",
    }
    # O2-13: same schema-from-model + strictify treatment as _call_xai above.
    schema = _strictify_schema(ReevaluateProposal.model_json_schema())
    body: dict[str, Any] = {
        "model": settings.xai_model,
        # O2-12/L2X-11: same always-on-reasoning risk as _call_xai above —
        # 1200 could be silently consumed by reasoning tokens before any
        # answer. 4000 is a runaway cap; real cost tracks actual usage.
        "max_tokens": 4000,
        "messages": [
            {"role": "system", "content": build_reevaluate_system_prompt(context)},
            {"role": "user", "content": build_reevaluate_user_prompt(context)},
        ],
        "temperature": 0.0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "reevaluation", "schema": schema, "strict": True},
        },
        # O2-14: do NOT add "reasoning_effort" here — grok-4 rejects the
        # param with HTTP 400; only grok-3-mini accepts it. Don't add it
        # without first gating on the configured model.
    }

    async def _post(payload: dict[str, Any]) -> httpx.Response:
        # Was 90s — inverted vs. Claude's 120s despite grok being the faster
        # model; raised to match the larger max_tokens budget above.
        async with httpx.AsyncClient(timeout=120.0) as client:
            return await client.post(url, headers=headers, json=payload)

    sent_body = body
    t0 = time.monotonic()
    try:
        r = await _post_with_retry(lambda: _post(body))
    except httpx.HTTPError as e:
        raise LlmError(f"xAI request failed: {e}") from e

    # O2-13: same 400-fallback wiring as _call_xai above — json_schema is
    # the primary path, json_object stays the safety net.
    if r.status_code == 400:
        fallback_body: dict[str, Any] = {**body, "response_format": {"type": "json_object"}}
        sent_body = fallback_body
        try:
            r = await _post_with_retry(lambda: _post(fallback_body))
        except httpx.HTTPError as e:
            raise LlmError(f"xAI request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:500]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(_categorize_provider_http_error("xAI", r.status_code, detail), raw=detail)

    try:
        payload = r.json()
        choice0 = payload["choices"][0]
        content = choice0["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise LlmError("xAI response missing choices content", raw=getattr(r, "text", None)) from e

    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider="xAI",
        model=sent_body.get("model"),
        route="reevaluate",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )

    finish_reason = choice0.get("finish_reason") if isinstance(choice0, dict) else None
    if finish_reason == "length":
        log.warning(
            "xAI reevaluate call hit max_tokens (finish_reason=length, max_tokens=%s); "
            "response may be truncated",
            sent_body.get("max_tokens"),
        )
        if not str(content or "").strip():
            raise LlmError(
                "xAI: Antwort abgeschnitten — Token-Budget erschöpft "
                f"(finish_reason=length, max_tokens={sent_body['max_tokens']})"
            )

    return _parse_content_to_reevaluation(str(content), provider="xAI")


async def _call_openai_compat_reevaluate(
    context: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    model: str,
    provider_label: str,
    timeout: float = 120.0,
    json_response_format: bool = True,
) -> ReevaluateProposal:
    """Chat-completions call for OpenAI-compatible APIs (OpenAI, Ollama)."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": build_reevaluate_system_prompt(context)},
            {"role": "user", "content": build_reevaluate_user_prompt(context)},
        ],
        "temperature": 0.0,
    }
    if json_response_format:
        body["response_format"] = {"type": "json_object"}

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, headers=headers, json=body)

    t0 = time.monotonic()
    try:
        r = await _post_with_retry(_post)
    except httpx.HTTPError as e:
        raise LlmError(f"{provider_label} request failed: {e}") from e

    if r.status_code >= 400:
        detail: Any = r.text[:500]
        try:
            detail = r.json()
        except Exception:
            pass
        raise LlmError(_categorize_provider_http_error(provider_label, r.status_code, detail), raw=detail)

    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise LlmError(
            f"{provider_label} response missing choices content",
            raw=getattr(r, "text", None),
        ) from e

    elapsed_ms = (time.monotonic() - t0) * 1000
    _log_llm_metrics(
        provider=provider_label,
        model=model,
        route="reevaluate",
        elapsed_ms=elapsed_ms,
        payload=payload,
    )

    return _parse_content_to_reevaluation(str(content), provider=provider_label)


async def _call_openai_reevaluate(
    context: dict[str, Any], settings: Settings
) -> ReevaluateProposal:
    if not settings.openai_api_key:
        raise LlmError("OpenAI API key not configured (set OPENAI_API_KEY)")
    return await _call_openai_compat_reevaluate(
        context,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        provider_label="Codex",
    )


async def _call_ollama_reevaluate(
    context: dict[str, Any], settings: Settings
) -> ReevaluateProposal:
    return await _call_openai_compat_reevaluate(
        context,
        base_url=settings.ollama_base_url,
        api_key="",
        model=settings.ollama_model,
        provider_label="Ollama",
        timeout=300.0,
        json_response_format=False,
    )


async def reevaluate_with_llm(
    context: dict[str, Any], settings: Settings
) -> ReevaluateProposal:
    """Dispatch by LLM_PROVIDER: claude | xai | openai (Codex) | ollama.

    Advisory only — reviews an already-open position; never places, moves or
    closes anything itself.
    """
    provider = (settings.llm_provider or "claude").strip().lower()
    if provider in ("claude", "anthropic"):
        return await _call_claude_reevaluate(context, settings)
    if provider in ("xai", "grok"):
        return await _call_xai_reevaluate(context, settings)
    if provider in ("openai", "codex"):
        return await _call_openai_reevaluate(context, settings)
    if provider in ("ollama", "local"):
        return await _call_ollama_reevaluate(context, settings)
    raise LlmError(
        f"Unknown LLM_PROVIDER={provider!r} (use claude, xai, openai or ollama)"
    )
