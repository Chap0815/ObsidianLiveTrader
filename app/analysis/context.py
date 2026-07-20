"""Build market snapshot for API and LLM context (public MEXC data only)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from app.analysis.indicators import indicator_bundle
from app.analysis.structure import key_levels
from app.models import Candle, ContractMeta, FundingRate, MarketSnapshot, TimeframeSlice


class MarketClient(Protocol):
    async def ticker(self, symbol: str): ...
    async def funding_rate(self, symbol: str): ...
    async def contract_meta(self, symbol: str): ...
    async def klines(self, symbol: str, interval: str, limit_hint: int = 200): ...


def _contract_public(meta: ContractMeta) -> dict[str, Any]:
    """CamelCase contract filters for API JSON (matches plan example)."""
    return {
        "symbol": meta.symbol,
        "contractSize": meta.contract_size,
        "priceUnit": meta.price_unit,
        "volUnit": meta.vol_unit,
        "minVol": meta.min_vol,
        "maxVol": meta.max_vol,
        "maxLeverage": meta.max_leverage,
        "minLeverage": meta.min_leverage,
        "apiAllowed": meta.api_allowed,
    }


# |funding| beyond this per-interval rate is treated as a meaningful
# crowded-side signal — matches the "0.01% per interval" threshold already
# used by the analysis prompt (app/llm/prompts.py step 7).
#
# Hyperliquid settles funding HOURLY (see _funding_annualized below), and
# typical hourly rates run well under the old 0.03% mark — a threshold
# calibrated for MEXC's 8h interval effectively never fired for Hyperliquid,
# so crowded_long/crowded_short almost never triggered. 0.0001 (0.01% per
# hour) annualizes to ~87.6% APR ((24*365)*0.0001), a level that's genuinely
# unusual and worth flagging as crowded for hourly-settled funding.
_FUNDING_EXTREME_THRESHOLD = 0.0001


def _funding_annualized(rate: float, collect_cycle: int | None) -> float:
    """Annualize a per-settlement funding rate.

    `collect_cycle` is the number of HOURS between funding settlements
    (MEXC's `collectCycle` field, typically 8). When it's unknown/absent —
    e.g. Hyperliquid's client (app/hyperliquid/client.py) never sets
    `collect_cycle`, and Hyperliquid pays/charges funding hourly per its
    docs — we assume a 1-hour settlement interval, i.e. the rate is already
    hourly. periods/year = (24 * 365) / cycle_hours.
    """
    cycle_hours = collect_cycle if collect_cycle and collect_cycle > 0 else 1
    periods_per_year = (24.0 * 365.0) / cycle_hours
    return round(rate * periods_per_year, 6)


def _funding_extreme(rate: float) -> str:
    if rate > _FUNDING_EXTREME_THRESHOLD:
        return "crowded_long"
    if rate < -_FUNDING_EXTREME_THRESHOLD:
        return "crowded_short"
    return "neutral"


def _funding_public(fr: FundingRate) -> dict[str, Any]:
    return {
        "symbol": fr.symbol,
        "fundingRate": fr.funding_rate,
        "maxFundingRate": fr.max_funding_rate,
        "minFundingRate": fr.min_funding_rate,
        "collectCycle": fr.collect_cycle,
        "nextSettleTime": fr.next_settle_time,
        "timestamp": fr.timestamp,
        "fundingAnnualized": _funding_annualized(fr.funding_rate, fr.collect_cycle),
        "fundingExtreme": _funding_extreme(fr.funding_rate),
    }


def _candles_public(candles: list[Candle]) -> list[dict[str, Any]]:
    return [
        {
            "time": c.time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "vol": c.vol,
            "amount": c.amount,
        }
        for c in candles
    ]


def _structure_public(candles: list[Candle]) -> dict[str, Any]:
    levels = key_levels(candles)
    return {
        "support": levels.support,
        "resistance": levels.resistance,
        "range_high": levels.range_high,
        "range_low": levels.range_low,
        "last_price": levels.last_price,
        "major_pools": [
            {
                "index": p.index,
                "time": p.time,
                "price": p.price,
                "kind": p.kind,
            }
            for p in levels.major_pools
        ],
        "swings": {
            "highs": [
                {"index": s.index, "time": s.time, "price": s.price, "kind": s.kind}
                for s in levels.swings.highs
            ],
            "lows": [
                {"index": s.index, "time": s.time, "price": s.price, "kind": s.kind}
                for s in levels.swings.lows
            ],
        },
    }


def build_tf_slice(tf: str, candles: list[Candle]) -> TimeframeSlice:
    return TimeframeSlice(
        tf=tf,
        candles=candles,
        indicators=indicator_bundle(candles),
        structure=_structure_public(candles),
    )


_DEFAULT_MARKET_EXTRAS: dict[str, Any] = {
    "open_interest": None,
    "premium": None,
    "prev_day_px": None,
    "oi_change_pct_1h": None,
    "oi_change_pct_4h": None,
}


# Daily candles change slowly; build_market_snapshot runs on every poll and coin
# switch, so cache the 1D fetch per (symbol, interval) for 5 min to cut one
# klines() call per poll. In-memory, process-local; clear_daily_cache() for tests.
#
# The cache is DEPTH-AWARE (audit B1): the entry stores the depth (`limit_hint`)
# the series was fetched with. The scanner asks for a shallow anchor and the deep
# analysis asks for a deeper one (EMA200 needs >= 200 candles). Keying only on
# (symbol, interval) meant a shallow scan fetch was served back to the deeper
# analysis request within the TTL, so daily.ema_stack silently went "unknown" on
# the normal scan->analyze path. We now refetch when the cached series is
# shallower than requested, and keep the deepest result.
_DAILY_TTL_S: float = 300.0
_daily_cache: dict[tuple[str, str], tuple[float, list[Candle], int]] = {}


def clear_daily_cache() -> None:
    _daily_cache.clear()


async def _fetch_daily_candles(
    client: Any, symbol: str, daily: str, limit_hint: int, *, ttl: float = _DAILY_TTL_S
) -> list[Candle]:
    """Daily anchor fetch. ADVISORY: must NEVER break the snapshot — a failed
    1D fetch returns [] (daily_slice=None, prompt falls back to htf). Errors
    are NOT cached so a transient failure doesn't stick for 5 minutes; an
    empty-but-successful result (new coin) IS cached.

    DEPTH-AWARE: a cached series is only reused when it was fetched at least as
    deep as the current request. A shallow scan fetch is therefore NEVER served
    to a deeper analysis request (which would leave EMA200/ema_stack "unknown")."""
    key = (symbol, daily)
    now = time.time()
    hit = _daily_cache.get(key)
    if hit is not None and (now - hit[0]) < ttl and hit[2] >= limit_hint:
        return hit[1]
    try:
        candles = await client.klines(symbol, daily, limit_hint=limit_hint)
    except Exception:
        return []  # do not cache errors
    _daily_cache[key] = (now, candles, limit_hint)
    return candles


async def _fetch_market_extras(client: Any, symbol: str) -> dict[str, Any]:
    """OI/premium extras if the client exposes market_extras(); else None-filled.

    MEXC has no open interest in ticker() and does NOT implement market_extras,
    so this returns the all-None default. Any fetch error is swallowed to a
    None-dict — OI is advisory context and must never break the snapshot.
    """
    fn = getattr(client, "market_extras", None)
    if fn is None:
        return dict(_DEFAULT_MARKET_EXTRAS)
    try:
        extras = await fn(symbol)
    except Exception:
        return dict(_DEFAULT_MARKET_EXTRAS)
    return {**_DEFAULT_MARKET_EXTRAS, **(extras or {})}


# --- BTC market-regime anchor (K2-02) ---------------------------------------
# Every altcoin is dominated by BTC beta, yet each coin is analysed in isolation.
# We fetch a tiny BTC regime block (daily + htf ema_stack + htf stretch) ONCE and
# reuse it across every altcoin analyze within the TTL, so it never adds a serial
# per-coin upstream cost. The daily/htf klines themselves also flow through the
# shared _daily_cache, so a warm cache costs zero upstream calls. Any failure
# degrades to None (block omitted) — it must NEVER break analyze.
_BTC_REGIME_SYMBOL = "BTC_USDT"
_BTC_REGIME_TTL_S: float = 300.0
_btc_regime_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}


def clear_btc_regime_cache() -> None:
    _btc_regime_cache.clear()


def is_btc_symbol(symbol: str | None) -> bool:
    """True for BTC itself — the regime block is self-referential there (K2-02)."""
    base = str(symbol or "").upper().split("_", 1)[0]
    return base in ("BTC", "XBT")


def _stack_and_stretch(candles: list[Candle]) -> tuple[str, float | None]:
    """(ema_stack label, price_vs_ema20_pct) for a candle series, or ('unknown', None)."""
    # Local import avoids a module-level app.analysis <- app.llm layering cycle;
    # reusing _ema_stack_label keeps the M2-03 pullback fix consistent for BTC too.
    from app.llm.client import _ema_stack_label

    if not candles:
        return "unknown", None
    bundle = indicator_bundle(candles)
    last = bundle.get("last") or {}
    # Task 16: the bundle already dropped the live bar, so the BTC-regime stack
    # and stretch must be measured against the same CLOSED close (`as_of_close`),
    # not the still-forming last candle, to avoid an intra-candle regime repaint.
    last_close = bundle.get("as_of_close")
    if last_close is None:
        last_close = candles[-1].close
    stack = _ema_stack_label(last, last_close)
    stretch: float | None = None
    e20 = last.get("ema20")
    try:
        if last_close and e20:
            stretch = round((last_close - e20) / e20 * 100.0, 3)
    except (TypeError, ZeroDivisionError):
        stretch = None
    return stack, stretch


# Block 2/TP2 Task P1: regime-tag buckets. Trend bucket maps fetch_btc_regime's
# btc_daily_stack (itself "bullish"/"bearish"/"mixed"/"unknown" from
# _ema_stack_label) onto the plan's up/down/chop vocabulary. Vol bucket splits
# ATR% (ATR14 / last_price * 100, LTF) into low/normal/high. Thresholds are a
# deliberate, documented choice (not derived from data) sized for typical LTF
# (15m-1H) crypto perp ATR% — scanner.py's own "tradeable" floor is 0.3%, well
# inside the low bucket here. Advisory-only labels; never used in any gate.
_REGIME_TREND_MAP = {"bullish": "up", "bearish": "down", "mixed": "chop"}
_VOL_BUCKET_LOW_PCT = 1.0   # atr_pct below this -> "low"
_VOL_BUCKET_HIGH_PCT = 3.0  # atr_pct at/above this -> "high"; between -> "normal"


def regime_tag(btc_regime: dict[str, Any] | None, atr_pct: float | None) -> str:
    """Compact per-trade regime tag: '<btcTrend>/<vol>', e.g. "btcUp/volNormal".

    PURE + deterministic + fail-safe: ANY missing/invalid input collapses the
    WHOLE tag to "unknown" (never a partial tag, never a crash) so it never
    blocks or alters an analyze, and journal_stats' by_regime GROUP BY always
    sees a clean, small vocabulary of keys.

    Trend bucket: btc_regime["btc_daily_stack"] (bullish/bearish/mixed/unknown)
    -> up/down/chop; any other/missing value -> unknown.
    Vol bucket: atr_pct < 1.0 -> low; 1.0 <= atr_pct < 3.0 -> normal;
    atr_pct >= 3.0 -> high. A non-numeric/negative/NaN atr_pct -> unknown.

    Advisory only (Block 2/TP2 §3.B): purely a persisted label for later
    journal segmentation; it never feeds a gate, sizing, or the LLM decision.
    """
    if not isinstance(btc_regime, dict):
        return "unknown"
    trend = _REGIME_TREND_MAP.get(btc_regime.get("btc_daily_stack"))
    if trend is None:
        return "unknown"
    if atr_pct is None or isinstance(atr_pct, bool):
        return "unknown"
    try:
        pct = float(atr_pct)
    except (TypeError, ValueError):
        return "unknown"
    if pct != pct or pct < 0:  # NaN / negative guard
        return "unknown"
    if pct < _VOL_BUCKET_LOW_PCT:
        vol = "low"
    elif pct < _VOL_BUCKET_HIGH_PCT:
        vol = "normal"
    else:
        vol = "high"
    return f"btc{trend.capitalize()}/vol{vol.capitalize()}"


async def fetch_btc_regime(
    client: Any,
    *,
    htf: str = "1H",
    daily: str = "1D",
    htf_limit_hint: int = 260,
    daily_limit_hint: int = 260,
    ttl: float = _BTC_REGIME_TTL_S,
) -> dict[str, Any] | None:
    """Compact BTC regime anchor: btc_daily_stack, btc_htf_stack,
    btc_price_vs_ema20_pct (1H stretch). Cached ~5 min and reused across all
    altcoin analyses. Returns None on any failure/empty data (block omitted)."""
    key = (htf, daily)
    now = time.time()
    hit = _btc_regime_cache.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    try:
        htf_candles = await _fetch_daily_candles(
            client, _BTC_REGIME_SYMBOL, htf, htf_limit_hint
        )
        daily_candles = await _fetch_daily_candles(
            client, _BTC_REGIME_SYMBOL, daily, daily_limit_hint
        )
    except Exception:
        return None  # never cache/raise — degrade gracefully
    if not htf_candles or not daily_candles:
        return None
    daily_stack, _ = _stack_and_stretch(daily_candles)
    htf_stack, htf_stretch = _stack_and_stretch(htf_candles)
    block: dict[str, Any] = {
        "btc_daily_stack": daily_stack,
        "btc_htf_stack": htf_stack,
        "btc_price_vs_ema20_pct": htf_stretch,
    }
    _btc_regime_cache[key] = (now, block)
    return block


async def build_market_snapshot(
    symbol: str,
    ltf: str,
    htf: str,
    client: MarketClient,
    *,
    limit_hint: int = 500,
    daily: str = "1D",
    daily_limit_hint: int = 260,
) -> MarketSnapshot:
    """Fetch public market data and compute LTF/HTF/Daily indicators + structure.

    Does not require private API keys.
    """
    # Fetch all public market data in parallel — these are independent upstream
    # calls; running them sequentially was the main source of chart lag on every
    # coin switch and poll. The ctx cache keeps ticker+funding from double-hitting.
    (
        ticker, funding, contract, ltf_candles, htf_candles, daily_candles, extras
    ) = await asyncio.gather(
        client.ticker(symbol),
        client.funding_rate(symbol),
        client.contract_meta(symbol),
        client.klines(symbol, ltf, limit_hint=limit_hint),
        client.klines(symbol, htf, limit_hint=limit_hint),
        _fetch_daily_candles(client, symbol, daily, daily_limit_hint),
        _fetch_market_extras(client, symbol),
    )

    ltf_slice = build_tf_slice(ltf, ltf_candles)
    htf_slice = build_tf_slice(htf, htf_candles)
    daily_slice = build_tf_slice(daily, daily_candles) if daily_candles else None

    return MarketSnapshot(
        symbol=symbol,
        last_price=ticker.last_price,
        funding=_funding_public(funding),
        contract=_contract_public(contract),
        ltf=ltf_slice,
        htf=htf_slice,
        market=extras,
        daily=daily_slice,
    )


def snapshot_to_api_dict(snap: MarketSnapshot) -> dict[str, Any]:
    """JSON shape for GET /api/market/{symbol}."""
    def slice_dict(s: TimeframeSlice) -> dict[str, Any]:
        return {
            "tf": s.tf,
            "candles": _candles_public(s.candles),
            "indicators": s.indicators,
            "structure": s.structure,
        }

    return {
        "symbol": snap.symbol,
        "last_price": snap.last_price,
        "funding": snap.funding,
        "contract": snap.contract,
        "ltf": slice_dict(snap.ltf),
        "htf": slice_dict(snap.htf),
        "market": snap.market or {},
        "daily": slice_dict(snap.daily) if snap.daily else None,
    }
