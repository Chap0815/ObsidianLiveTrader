"""Market scanner: a cheap/fast model screens many coins for setups.

Token split by design: the scanner sends COMPACT per-coin summaries (no candle
arrays) to a small model (SCANNER_MODEL, default Sonnet) in ONE batched call.
The expensive deep analysis per coin stays with LLM_PROVIDER (e.g. Opus) and
runs only when the user opens a coin from the result list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from itertools import zip_longest
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

log = logging.getLogger("app.llm.scanner")

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

Scoring: 0-10 as an ABSOLUTE quality bar, not a relative ranking. Ground the
scale on these anchors instead of picking a number in the abstract:
  - 5 = valid-but-marginal: clears both hard rejects but only one weak
    confluence, or a level that's close-but-not-clean. Worth a second look,
    not a conviction pick.
  - 6 = takeable: "a disciplined analyst would actually take this now" — at
    least 2 confluences agree (regime + momentum + location + funding/OI).
  - 8 = A+: confluences stack cleanly with no meaningful conflict anywhere in
    the read (regime, momentum, location, funding/OI all agree) — this is
    roughly the deep analyzer's own "high" target-confidence tier, so hand it
    off as such.
  - 10 = reserved for the rare textbook case; do not hand it out for a merely
    good setup.
Only include coins with score >= 5 that clear both hard rejects above
(against-daily is a score cap, not a reject). Maximum 6 results, sorted by
score descending. Returning an empty list is the correct answer in a quiet
market.

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
    # bar_progress describes recent_candles, which we just dropped for screening.
    c.pop("bar_progress", None)
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
                # paced=True: this whole-universe klines fan-out (up to
                # universe_size×3 calls) is THE burst that trips Hyperliquid 429.
                # Route it through the client's read-rate token bucket so it stays
                # under the per-IP limit; interactive/monitor reads stay unpaced.
                ltf_candles, htf_candles, daily_candles = await asyncio.gather(
                    client.klines(sym, tf, limit_hint=kline_limit, paced=True),
                    client.klines(sym, htf, limit_hint=kline_limit, paced=True),
                    _fetch_daily_candles(client, sym, daily, kline_limit, paced=True),
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
        # every coin in the scan universe (up to scanner_universe_size).
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


# ── Task 24 (S2-01): candidate universe ─────────────────────────────────────
# Classic screened top-N-by-turnover — a blue-chip-chop filter that misses the
# runners and wastes slots on edge-less high-caps. The prefilter universe is a
# momentum/flow-ranked UNION gated by a turnover LIQUIDITY FLOOR (no fixed
# blue-chip slots): a coin enters because it MOVES or carries OI flow, not
# because it's big. Turnover is a floor + a top-up-to-size tail only.
#
# Honest data note: neither exchange's batch market_overview payload carries a
# 1h/4h price-change or an OI-Δ — HL's assetCtx exposes prevDayPx (24h) +
# openInterest (level); MEXC's ticker exposes riseFallRate (24h) + holdVol
# (level). So the universe ranks on the 24h |price-change| (runner proxy) and,
# where present, |OI-Δ|; the ATR%/RRR/OI-confluence multi-factor scoring runs
# in the prefilter stage below, where per-coin klines exist. When a ranking
# field is absent (degraded/cold data) that dimension simply contributes
# nothing and the turnover top-up keeps the universe non-empty.


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _rank_top(
    rows: list[dict[str, Any]], key: Any, n: int
) -> list[dict[str, Any]]:
    scored = [(k, r) for r in rows if (k := key(r)) is not None]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:n]]


def select_scan_universe(
    overview: list[dict[str, Any]], settings: Settings
) -> list[dict[str, Any]]:
    """Build the scanner candidate universe.

    classic: byte-for-byte the old behavior — the first `scanner_max_coins`
    rows of the turnover-sorted overview, order untouched.

    prefilter: a de-duplicated UNION of top-N-per-dimension rankings
    (|price-change|, |OI-Δ|, |funding|) over the turnover-floored pool, topped
    up by turnover to `scanner_universe_size` so it's never thin/empty."""
    if (settings.scanner_mode or "classic") == "classic":
        return list(overview[: max(1, settings.scanner_max_coins)])

    floor = settings.scanner_turnover_floor_usd
    liquid = [r for r in overview if (_num(r.get("volume24")) or 0.0) >= floor]
    # Fail-safe: if the floor empties the pool (misconfigured / illiquid market)
    # fall back to the full overview rather than returning nothing to scan.
    pool = liquid or list(overview)
    top_n = settings.scanner_rank_top_n

    by_momentum = _rank_top(
        pool, lambda r: abs(v) if (v := _num(r.get("price_change_pct"))) is not None else None, top_n
    )
    by_oi = _rank_top(
        pool, lambda r: abs(v) if (v := _num(r.get("oi_change_pct_1h"))) is not None else None, top_n
    )
    by_funding = _rank_top(
        pool, lambda r: abs(v) if (v := _num(r.get("funding"))) is not None else None, top_n
    )
    by_turnover = _rank_top(pool, lambda r: _num(r.get("volume24")), len(pool))

    union: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(r: dict[str, Any]) -> None:
        sym = str(r.get("symbol") or "")
        if sym and sym not in seen:
            seen.add(sym)
            union.append(r)

    # INTERLEAVE the signal dimensions round-robin (rank-1 of each, then rank-2,
    # …) so a stage-1 front-slice (select_prefilter_stage1) keeps dimension
    # diversity instead of collapsing to a pure 24h-momentum prefix — that prefix
    # is anti-correlated with _prefilter_score's stretch/exhaustion penalties and
    # would drop the coil/range-fade setups stage-2 exists to find. Turnover is
    # only the top-up TAIL (deprioritized blue-chip liquidity), appended last.
    for tier in zip_longest(by_momentum, by_oi, by_funding):
        for r in tier:
            if r is not None:
                _add(r)
    for r in by_turnover:
        _add(r)
    result = union[: max(1, settings.scanner_universe_size)]
    # Observability (T24): which dimensions actually contributed. On batch
    # payloads OI-Δ/1h/4h are typically absent -> by_oi empty -> the universe is
    # effectively momentum(24h)+turnover; this line makes that visible instead of
    # a silent turnover-only collapse.
    log.info(
        "SCANNER_UNIVERSE mode=prefilter pool=%d momentum=%d oi=%d funding=%d -> universe=%d",
        len(pool), len(by_momentum), len(by_oi), len(by_funding), len(result),
    )
    return result


def select_prefilter_stage1(
    universe: list[dict[str, Any]], settings: Settings
) -> list[dict[str, Any]]:
    """Stage-1 (cheap, no klines) cut of the rank-ordered universe.

    select_scan_universe interleaves the signal dimensions strongest-first, so
    taking the top stage1_k here means build_scan_contexts fetches klines for
    only those candidates instead of the whole universe — the big 429-inducing
    burst. The klines-based stage-2 (prefilter_contexts) then picks the final
    top-K from what survives.

    NEVER cuts below scanner_prefilter_top_k, so stage-2 never gets fewer
    candidates than it keeps (the intelligent klines filter must not degrade to a
    no-op; at the floor edge it becomes a pass-through, never a shrink).
    Pass-through in classic mode or when the effective k >= len(universe), so the
    change is fully reversible via config."""
    if (settings.scanner_mode or "classic") == "classic":
        return universe
    k = max(
        int(settings.scanner_prefilter_stage1_k),
        int(settings.scanner_prefilter_top_k),
    )
    return universe[:k] if k < len(universe) else universe


# ── Task 24 (S2-08): deterministic rules-prefilter ──────────────────────────
# Cheap (no LLM, no extra fetches) scoring of the ~50 universe contexts that
# build_scan_contexts already produced, to send only the top-K to the LLM.
#
# INCLUSIVE by design (audit constraint): it RANKS and takes the top-K, it does
# NOT hard-reject on any single metric. Every component is an additive nudge, so
# a coin that's strong on one dimension the others miss still ranks and can
# survive. Risk (documented): a coin the LLM would have scored high on a
# dimension this prefilter under-weights could still be cut when the universe is
# crowded — mitigated by a generous top-K (default 8) and never single-gating.


def _rough_rrr(
    bias: str, price: float | None, struct: dict[str, Any], atr: float | None
) -> float | None:
    """Coarse reward/risk to the nearest OPPOSING structure vs a ~1.5×ATR stop."""
    p = _num(price)
    a = _num(atr)
    if p is None or a is None or p <= 0 or a <= 0:
        return None
    if bias == "long":
        tgt = _num(struct.get("resistance"))
        if tgt is None or tgt <= p:
            tgt = _num(struct.get("range_high"))
        if tgt is None or tgt <= p:
            return None
        reward = tgt - p
    else:
        tgt = _num(struct.get("support"))
        if tgt is None or tgt >= p:
            tgt = _num(struct.get("range_low"))
        if tgt is None or tgt >= p:
            return None
        reward = p - tgt
    return reward / (a * 1.5)


def _prefilter_score(ctx: dict[str, Any]) -> float:
    """Deterministic 0-ish..N quality nudge sum (higher = better candidate)."""
    ltf = ctx.get("ltf") or {}
    htf = ctx.get("htf") or {}
    read = ltf.get("read") or {}
    hread = htf.get("read") or {}
    tails = ltf.get("indicators_tail") or {}
    struct = ltf.get("structure") or {}

    score = 0.0
    # 1) Regime alignment across ltf/htf/1D (either direction).
    stacks = [read.get("ema_stack"), hread.get("ema_stack"), ctx.get("daily_stack")]
    bull = sum(1 for s in stacks if s == "bullish")
    bear = sum(1 for s in stacks if s == "bearish")
    score += max(bull, bear) * 1.5
    bias = "long" if bull >= bear else "short"

    # 2) Momentum tail (RSI/MACD) supporting the dominant bias; exhaustion penalty.
    rsi_t = tails.get("rsi14") or []
    macd_t = tails.get("macd_hist") or []
    r = _num(rsi_t[-1]) if rsi_t else None
    m = _num(macd_t[-1]) if macd_t else None
    if r is not None and m is not None:
        if bias == "long" and m > 0 and 45 <= r <= 70:
            score += 1.5
        elif bias == "short" and m < 0 and 30 <= r <= 55:
            score += 1.5
        if r >= 78 or r <= 22:  # over-extended momentum = poor fresh entry
            score -= 1.0

    # 3) Location / stretch: near value is fresh, far is a no-chase risk.
    stretch = _num(read.get("price_vs_ema20_pct"))
    if stretch is not None:
        if abs(stretch) <= 2.0:
            score += 1.0
        elif abs(stretch) >= 6.0:
            score -= 1.5
    # ATR% present & tradeable range (not dead-flat).
    atr = _num(read.get("atr14"))
    price = _num(ctx.get("last_price"))
    if atr is not None and price and price > 0 and (atr / price * 100.0) >= 0.3:
        score += 0.5

    # 4) Rough RRR to nearest opposing structure.
    rrr = _rough_rrr(bias, price, struct, atr)
    if rrr is not None:
        if rrr >= 1.5:
            score += 1.5
        elif rrr < 1.0:
            score -= 1.0

    # 5) Funding / OI confluence (tiebreakers).
    fe = ctx.get("funding_extreme")
    if fe == "neutral":
        score += 0.25
    elif (bias == "long" and fe == "crowded_short") or (
        bias == "short" and fe == "crowded_long"
    ):
        score += 0.75  # crowded AGAINST us = squeeze fuel our way
    elif (bias == "long" and fe == "crowded_long") or (
        bias == "short" and fe == "crowded_short"
    ):
        score -= 0.5  # crowded WITH us = squeeze risk against us
    oi = ctx.get("oi_read")
    if isinstance(oi, str):
        if (bias == "long" and oi.startswith("price_up_oi_up")) or (
            bias == "short" and oi.startswith("price_down_oi_up")
        ):
            score += 0.75

    return score


def prefilter_contexts(
    contexts: list[dict[str, Any]], settings: Settings
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rank contexts by _prefilter_score and keep the top-K (prefilter mode).

    Returns (kept, dropped). classic mode is a pass-through (no reduction).
    Deterministic: ties break on original position, so identical input always
    yields identical output/order."""
    if (settings.scanner_mode or "classic") == "classic":
        return list(contexts), []
    k = max(1, settings.scanner_prefilter_top_k)
    scored = [(_prefilter_score(c), i, c) for i, c in enumerate(contexts)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    kept = [c for _, _, c in scored[:k]]
    dropped = [c for _, _, c in scored[k:]]
    return kept, dropped


def _merge_scan_results(results: list[ScanResult]) -> list[ScanResult]:
    """Merge chunked results: dedupe by symbol (highest score wins), sort desc,
    cap at 6 — mirrors parse_scan_results' final shaping. Task 22's grounded
    rubric makes the per-chunk scores absolute, so cross-chunk merge is valid."""
    best: dict[str, ScanResult] = {}
    for r in results:
        key = r.symbol.upper()
        if key not in best or r.score > best[key].score:
            best[key] = r
    out = sorted(best.values(), key=lambda r: r.score, reverse=True)
    return out[:6]


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
        # Task 15 (P2-03/S2-03): on Sonnet 5 / the current Opus/Sonnet 4.6+
        # family, omitting `thinking` leaves adaptive thinking ON by default
        # — it shares this call's max_tokens budget with the JSON answer, so
        # a scan can silently truncate mid-array. The scanner wants a fast,
        # cheap screen, not extended reasoning, so thinking is explicitly
        # disabled. max_tokens raised 4096->6000 for headroom, consistent
        # with the OpenAI-compat scanner path's budget (S2-02).
        "max_tokens": 6000,
        "thinking": {"type": "disabled"},
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


async def _call_scanner_llm(
    system: str, user: str, settings: Settings
) -> tuple[str, str]:
    """Route one screener call to the configured provider. Returns (text, model).

    Cheap model first (token split), fall back to whatever is configured."""
    if settings.claude_ready:
        model = settings.scanner_model
        text = await _anthropic_text(system, user, model, settings, provider_label="Claude")
    elif settings.xai_ready:
        model = settings.xai_model
        text = await _openai_compat_text(
            system, user, model, settings.xai_base_url, settings.xai_api_key,
            provider_label="xAI",
        )
    elif settings.openai_ready:
        model = settings.openai_model
        text = await _openai_compat_text(
            system, user, model, settings.openai_base_url, settings.openai_api_key,
            provider_label="Codex",
        )
    elif settings.ollama_ready:
        model = settings.ollama_model
        text = await _openai_compat_text(
            system, user, model, settings.ollama_base_url, "",
            timeout=300.0, provider_label="Ollama",
        )
    else:
        raise LlmError("No LLM configured for the scanner")
    return text, model


def _parse_scan_or_raise(
    text: str, allowed: set[str], model: str
) -> list[ScanResult]:
    try:
        return parse_scan_results(
            text, allowed_symbols=allowed, min_score=SCANNER_MIN_SCORE
        )
    except json.JSONDecodeError as e:
        preview = (text or "")[:200].replace("\n", " ")
        raise LlmError(
            f"Scanner-Antwort ist kein JSON ({e}). Modell {model} lieferte: "
            f"{preview!r}",
            raw=text,
        ) from e


async def scan_with_llm(
    contexts: list[dict[str, Any]], settings: Settings
) -> tuple[list[ScanResult], str]:
    """Screener call(s). Returns (results, model_used).

    Classic mode is ALWAYS a single batched call (byte-for-byte the old path).
    In prefilter mode, if more than `scanner_llm_chunk_max` candidates remain
    (a safety net — the prefilter normally sends ~top-K ≤ 8) the set is split
    into two balanced chunks, each screened separately, and the results merged
    (S2-02). Task 22's grounded rubric makes per-chunk scores absolute, so a
    cross-chunk merge stays calibrated."""
    if not contexts:
        return [], "none"

    def _allowed(cs: list[dict[str, Any]]) -> set[str]:
        # Server-side allowlist: only symbols we actually sent may come back
        # (F-22) — a hallucinated-but-valid-looking symbol is dropped.
        return {str(c.get("symbol") or "").upper() for c in cs if c.get("symbol")}

    mode = settings.scanner_mode or "classic"
    if mode == "prefilter" and len(contexts) > settings.scanner_llm_chunk_max:
        mid = (len(contexts) + 1) // 2
        chunks = [contexts[:mid], contexts[mid:]]
        # Run the two screener chunks CONCURRENTLY — they are independent LLM
        # calls, so a sequential await would double the scan's LLM-latency budget
        # and (stacked on the paced klines phase) could push a slow scan past the
        # frontend's request-abort window.
        chunk_calls = await asyncio.gather(
            *(
                _call_scanner_llm(
                    SCANNER_SYSTEM_PROMPT, _scan_user_prompt(chunk), settings
                )
                for chunk in chunks
            )
        )
        merged: list[ScanResult] = []
        model_used = "none"
        for chunk, (text, model_used) in zip(chunks, chunk_calls):
            merged.extend(_parse_scan_or_raise(text, _allowed(chunk), model_used))
        return _merge_scan_results(merged), model_used

    text, model = await _call_scanner_llm(
        SCANNER_SYSTEM_PROMPT, _scan_user_prompt(contexts), settings
    )
    return _parse_scan_or_raise(text, _allowed(contexts), model), model
