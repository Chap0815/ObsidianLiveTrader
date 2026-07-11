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


def _funding_public(fr: FundingRate) -> dict[str, Any]:
    return {
        "symbol": fr.symbol,
        "fundingRate": fr.funding_rate,
        "maxFundingRate": fr.max_funding_rate,
        "minFundingRate": fr.min_funding_rate,
        "collectCycle": fr.collect_cycle,
        "nextSettleTime": fr.next_settle_time,
        "timestamp": fr.timestamp,
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
    ticker, funding, contract, ltf_candles, htf_candles = await asyncio.gather(
        client.ticker(symbol),
        client.funding_rate(symbol),
        client.contract_meta(symbol),
        client.klines(symbol, ltf, limit_hint=limit_hint),
        client.klines(symbol, htf, limit_hint=limit_hint),
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
    }
