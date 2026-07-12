"""Build market snapshot for API and LLM context (public MEXC data only)."""

from __future__ import annotations

import asyncio
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


async def build_market_snapshot(
    symbol: str,
    ltf: str,
    htf: str,
    client: MarketClient,
    *,
    limit_hint: int = 500,
) -> MarketSnapshot:
    """Fetch public market data and compute LTF/HTF indicators + structure.

    Does not require private API keys.
    """
    # Fetch all public market data in parallel — these are independent upstream
    # calls; running them sequentially was the main source of chart lag on every
    # coin switch and poll. The ctx cache keeps ticker+funding from double-hitting.
    ticker, funding, contract, ltf_candles, htf_candles, extras = await asyncio.gather(
        client.ticker(symbol),
        client.funding_rate(symbol),
        client.contract_meta(symbol),
        client.klines(symbol, ltf, limit_hint=limit_hint),
        client.klines(symbol, htf, limit_hint=limit_hint),
        _fetch_market_extras(client, symbol),
    )

    ltf_slice = build_tf_slice(ltf, ltf_candles)
    htf_slice = build_tf_slice(htf, htf_candles)

    return MarketSnapshot(
        symbol=symbol,
        last_price=ticker.last_price,
        funding=_funding_public(funding),
        contract=_contract_public(contract),
        ltf=ltf_slice,
        htf=htf_slice,
        market=extras,
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
    }
