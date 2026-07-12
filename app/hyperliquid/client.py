"""Hyperliquid (testnet/mainnet) adapter with MEXC-like surface for OrderService.

Size = coin amount (e.g. 0.001 BTC). contract_size is always 1.0 so risk =
vol * abs(entry - stop) in USDC terms for linear perps.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from app.hyperliquid.errors import HyperliquidError
from app.models import Candle, ContractMeta, FundingRate, Ticker

INTERVAL_MAP = {
    "5m": "5m",
    "15m": "15m",
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
}


def _opt_f(v: Any) -> float | None:
    """float(v) or None — for optional numeric ctx fields (openInterest, premium)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_hl_coin(symbol: str) -> str:
    """BTC_USDT / BTC-USDT / BTC → BTC."""
    s = (symbol or "").strip().upper().replace("-", "_")
    if "_" in s:
        s = s.split("_")[0]
    return s


# Hyperliquid perp min order value (USDC notional)
HL_MIN_NOTIONAL_USD = 10.0


def round_hl_price(px: float, sz_decimals: int) -> float:
    """Round price to Hyperliquid tick rules.

    Perp prices: max 5 significant figures AND max (6 - szDecimals) decimals.
    Integer prices are always allowed regardless of significant figures.
    """
    if px is None or px <= 0:
        return px
    max_dec = max(0, 6 - int(sz_decimals or 0))
    if px >= 100_000:
        # 6+ integer digits: integer prices always allowed
        return float(round(px))
    rounded_sig = float(f"{px:.5g}")
    return round(rounded_sig, max_dec)


def external_oid_to_cloid(external_oid: str):
    """Map our ``mlt-…`` externalOid to a valid Hyperliquid Cloid.

    HL Cloid = "0x" + 32 hex chars (16 bytes). Our externalOid is not valid
    hex, so derive a deterministic 128-bit id via SHA-256. Deterministic means
    timeout-recovery can recompute the SAME cloid from the same externalOid and
    look the order up by it.
    """
    from hyperliquid.utils.types import Cloid

    oid = (external_oid or "").strip()
    if not oid:
        return None
    digest = hashlib.sha256(oid.encode("utf-8")).hexdigest()[:32]
    return Cloid.from_str("0x" + digest)


class HyperliquidClient:
    exchange_id = "hyperliquid"

    def __init__(
        self,
        *,
        private_key: str = "",
        account_address: str = "",
        testnet: bool = True,
        base_url: str | None = None,
        market_slippage_pct: float = 0.5,
    ):
        from hyperliquid.utils import constants

        self.testnet = testnet
        # Max adverse fill for market orders (fraction for SDK)
        self.market_slippage = max(0.0005, float(market_slippage_pct) / 100.0)
        self.base_url = (
            base_url
            or (constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL)
        ).rstrip("/")
        self.private_key = (private_key or "").strip()
        self.account_address = (account_address or "").strip()
        self._info = None
        self._exchange = None
        self._meta_cache: dict[str, Any] | None = None
        self._asset_index: dict[str, int] = {}
        # Short-TTL cache for meta_and_asset_ctxs (whole-universe fetch used for
        # funding). ticker() and funding_rate() both need it — without this each
        # /api/market call hit the upstream 2× on top of every poll.
        self._ctx_cache: tuple[float, Any] | None = None
        # OI history per coin: list of (unix_ts, open_interest), pruned to ~4.5h.
        # Feeds oi_change_pct_1h / _4h in market_extras(). In-memory only —
        # resets on restart (advisory context, not persisted state).
        self._oi_history: dict[str, list[tuple[float, float]]] = {}

    def _meta_ctxs_sync(self, ttl: float = 2.0):
        """meta_and_asset_ctxs() with a short TTL cache (runs inside a thread)."""
        now = time.time()
        if self._ctx_cache is not None and (now - self._ctx_cache[0]) < ttl:
            return self._ctx_cache[1]
        ctx = self._get_info().meta_and_asset_ctxs()
        self._ctx_cache = (now, ctx)
        return ctx

    def _record_oi_and_change(
        self, coin: str, oi: float | None, *, now: float | None = None
    ) -> tuple[float | None, float | None]:
        """Append the current OI to this coin's history and return the % change
        vs ~1h and ~4h ago. Returns (None, None) until enough history exists."""
        if oi is None or oi <= 0:
            return None, None
        now = time.time() if now is None else float(now)
        hist = self._oi_history.setdefault(coin, [])
        # Sample-throttle (Plan-Review): max 1 sample/minute — the 5s market
        # poll would otherwise grow ~3200 tuples/coin in the 4.5h window. If
        # the newest sample is <60s old, skip the append and just compute.
        if not hist or (now - hist[-1][0]) >= 60.0:
            hist.append((now, float(oi)))
        cutoff = now - 4.5 * 3600.0
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        return (
            self._oi_change_over(hist, now, 3600.0),
            self._oi_change_over(hist, now, 4 * 3600.0),
        )

    @staticmethod
    def _oi_change_over(
        hist: list[tuple[float, float]], now: float, lookback_s: float
    ) -> float | None:
        """% change of the latest OI vs the sample ~lookback_s ago.

        Uses the last sample at/before the target time. If none is old enough,
        falls back to the oldest sample ONLY when it already spans >= 50% of the
        lookback (avoids a misleading '1h change' computed over 2 minutes)."""
        if not hist:
            return None
        target = now - lookback_s
        ref = None
        for ts, val in hist:
            if ts <= target:
                ref = (ts, val)
            else:
                break
        if ref is None:
            oldest_ts, oldest_val = hist[0]
            if now - oldest_ts < 0.5 * lookback_s:
                return None
            ref = (oldest_ts, oldest_val)
        ref_val = ref[1]
        cur = hist[-1][1]
        if ref_val <= 0:
            return None
        return round((cur - ref_val) / ref_val * 100.0, 3)

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info

            self._info = Info(self.base_url, skip_ws=True)
        return self._info

    def _get_exchange(self):
        if self._exchange is None:
            if not self.private_key:
                raise HyperliquidError("HL_PRIVATE_KEY not configured")
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(self.private_key)
            addr = self.account_address or wallet.address
            self.account_address = addr
            self._exchange = Exchange(
                wallet, self.base_url, account_address=addr
            )
        return self._exchange

    def _resolve_address(self) -> str:
        addr = self.account_address
        if not addr and self.private_key:
            from eth_account import Account

            addr = Account.from_key(self.private_key).address
            self.account_address = addr
        return addr or ""

    async def aclose(self) -> None:
        self._info = None
        self._exchange = None

    async def _to_thread(self, fn, *args, **kwargs):
        """Run SDK call in a thread; ALL failures become HyperliquidError.

        The SDK raises its own ServerError/ClientError (e.g. testnet 502) —
        without this wrap they escape the ExchangeError handlers as HTTP 500.
        """
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"hyperliquid api: {e}") from e

    def _load_meta_sync(self) -> dict[str, Any]:
        if self._meta_cache is not None:
            return self._meta_cache
        info = self._get_info()
        meta = info.meta()
        self._meta_cache = meta
        self._asset_index = {
            str(a["name"]).upper(): i
            for i, a in enumerate(meta.get("universe") or [])
        }
        return meta

    def _asset_row(self, coin: str) -> dict[str, Any]:
        meta = self._load_meta_sync()
        coin = to_hl_coin(coin)
        for row in meta.get("universe") or []:
            if str(row.get("name", "")).upper() == coin:
                return row
        raise HyperliquidError(f"Unknown Hyperliquid coin: {coin}")

    async def ping(self) -> int:
        def _p():
            self._get_info().all_mids()
            return int(time.time() * 1000)

        return await self._to_thread(_p)

    async def list_symbols(self) -> list[str]:
        """All tradeable perp coins (for the UI symbol dropdown)."""

        def _s():
            meta = self._load_meta_sync()
            out = []
            for a in meta.get("universe") or []:
                name = str(a.get("name") or "").upper()
                if not name or a.get("isDelisted"):
                    continue
                out.append(name)
            return sorted(out)

        return await self._to_thread(_s)

    async def market_overview(self, limit: int = 12) -> list[dict[str, Any]]:
        """Top perps by 24h notional volume with funding/mark (for the scanner)."""

        def _o():
            info = self._get_info()
            meta_ctx = info.meta_and_asset_ctxs()
            universe = (meta_ctx[0] or {}).get("universe") or []
            ctxs = meta_ctx[1] if meta_ctx and len(meta_ctx) > 1 else []
            rows: list[dict[str, Any]] = []
            for i, u in enumerate(universe):
                name = str(u.get("name") or "").upper()
                if not name or u.get("isDelisted"):
                    continue
                ctx = ctxs[i] if i < len(ctxs) else {}
                rows.append(
                    {
                        "symbol": name,
                        "volume24": float(ctx.get("dayNtlVlm") or 0),
                        "funding": float(ctx.get("funding") or 0),
                        "last": float(ctx.get("markPx") or 0),
                    }
                )
            rows.sort(key=lambda r: r["volume24"], reverse=True)
            return rows[: max(1, int(limit))]

        return await self._to_thread(_o)

    async def contract_meta(self, symbol: str) -> ContractMeta:
        def _m():
            row = self._asset_row(symbol)
            coin = str(row["name"]).upper()
            sz_dec = int(row.get("szDecimals") or 0)
            vol_unit = 10 ** (-sz_dec) if sz_dec > 0 else 1.0
            max_lev = int(row.get("maxLeverage") or 50)
            return ContractMeta(
                symbol=coin,
                contract_size=1.0,  # size is in coins
                # I3: HL has no fixed tick size (5-sig-fig rounding instead), so
                # price_unit=0.0 is a deliberate boundary, not a TODO. The
                # gate's tick-size guard is a no-op for this field; drift is
                # covered by AUTO_FLATTEN, and order placement rounds via
                # round_hl_price() rather than this unit.
                price_unit=0.0,  # filled from mid later if needed
                vol_unit=vol_unit,
                min_vol=vol_unit,
                max_vol=1_000_000.0,
                max_leverage=max_lev,
                min_leverage=1,
                min_notional=HL_MIN_NOTIONAL_USD,
                api_allowed=True,
                price_scale=None,
                vol_scale=sz_dec,
                base_coin=coin,
                quote_coin="USDC",
                state=0,
            )

        return await self._to_thread(_m)

    async def ticker(self, symbol: str) -> Ticker:
        def _t():
            coin = to_hl_coin(symbol)
            info = self._get_info()
            mids = info.all_mids()
            if coin not in mids:
                raise HyperliquidError(f"No mid price for {coin}")
            mid = float(mids[coin])
            # funding from metaAndAssetCtxs if available
            funding = None
            try:
                meta_ctx = self._meta_ctxs_sync()
                universe = meta_ctx[0].get("universe") if meta_ctx else []
                ctxs = meta_ctx[1] if meta_ctx and len(meta_ctx) > 1 else []
                for i, u in enumerate(universe or []):
                    if str(u.get("name", "")).upper() == coin and i < len(ctxs):
                        funding = float(ctxs[i].get("funding") or 0)
                        break
            except Exception:
                pass
            return Ticker(
                symbol=coin,
                last_price=mid,
                bid1=mid,
                ask1=mid,
                fair_price=mid,
                index_price=mid,
                funding_rate=funding,
                timestamp=int(time.time() * 1000),
            )

        return await self._to_thread(_t)

    async def funding_rate(self, symbol: str) -> FundingRate:
        t = await self.ticker(symbol)
        return FundingRate(
            symbol=to_hl_coin(symbol),
            funding_rate=float(t.funding_rate or 0),
            timestamp=t.timestamp,
        )

    async def market_extras(self, symbol: str) -> dict[str, Any]:
        """Open interest / premium context from meta_and_asset_ctxs.

        Reads the SAME short-TTL ctx cache as ticker()/funding_rate() so this
        adds no extra upstream hit. oi_change_pct_* come from an in-memory
        per-coin OI history (_record_oi_and_change) and are None until enough
        history has accumulated. MEXC has no equivalent and does NOT implement
        this method (see app/analysis/context._fetch_market_extras).
        """

        def _x():
            coin = to_hl_coin(symbol)
            meta_ctx = self._meta_ctxs_sync()
            universe = meta_ctx[0].get("universe") if meta_ctx else []
            ctxs = meta_ctx[1] if meta_ctx and len(meta_ctx) > 1 else []
            oi = premium = prev_day_px = None
            for i, u in enumerate(universe or []):
                if str(u.get("name", "")).upper() == coin and i < len(ctxs):
                    c = ctxs[i] or {}
                    oi = _opt_f(c.get("openInterest"))
                    premium = _opt_f(c.get("premium"))
                    prev_day_px = _opt_f(c.get("prevDayPx"))
                    break
            ch1, ch4 = self._record_oi_and_change(coin, oi)
            return {
                "open_interest": oi,
                "premium": premium,
                "prev_day_px": prev_day_px,
                "oi_change_pct_1h": ch1,
                "oi_change_pct_4h": ch4,
            }

        return await self._to_thread(_x)

    async def klines(
        self, symbol: str, interval: str, limit_hint: int = 200
    ) -> list[Candle]:
        def _k():
            coin = to_hl_coin(symbol)
            iv = INTERVAL_MAP.get(interval, interval)
            # map UI intervals to HL
            hl_iv = {
                "Min5": "5m",
                "Min15": "15m",
                "Min60": "1h",
                "Hour4": "4h",
                "Day1": "1d",
                "5m": "5m",
                "15m": "15m",
                "1H": "1h",
                "4H": "4h",
                "1D": "1d",
                "1h": "1h",
                "4h": "4h",
                "1d": "1d",
            }.get(iv, "15m")
            end = int(time.time() * 1000)
            # bar ms
            bar_ms = {
                "5m": 5 * 60_000,
                "15m": 15 * 60_000,
                "1h": 60 * 60_000,
                "4h": 4 * 60 * 60_000,
                "1d": 24 * 60 * 60_000,
            }.get(hl_iv, 15 * 60_000)
            start = end - bar_ms * max(int(limit_hint), 10)
            info = self._get_info()
            raw = info.candles_snapshot(coin, hl_iv, start, end)
            out: list[Candle] = []
            for r in raw or []:
                out.append(
                    Candle(
                        time=int(r.get("t") or 0),
                        open=float(r.get("o") or 0),
                        high=float(r.get("h") or 0),
                        low=float(r.get("l") or 0),
                        close=float(r.get("c") or 0),
                        vol=float(r.get("v") or 0),
                        amount=0.0,
                    )
                )
            if limit_hint > 0 and len(out) > limit_hint:
                out = out[-limit_hint:]
            return out

        return await self._to_thread(_k)

    async def assets(self) -> list[dict[str, Any]]:
        def _a():
            if not self.account_address and not self.private_key:
                return []
            info = self._get_info()
            # ensure address
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
                self.account_address = addr
            state = info.user_state(addr)
            margin = state.get("marginSummary") or state.get("crossMarginSummary") or {}
            equity = float(margin.get("accountValue") or 0)
            withdrawable = float(state.get("withdrawable") or 0)
            used = float(margin.get("totalMarginUsed") or 0)
            # Prefer exchange-reported withdrawable; fall back to equity - IM
            available = withdrawable if withdrawable > 0 else max(equity - used, 0.0)
            return [
                {
                    "currency": "USDT",  # normalized label; HL margin ccy is USDC
                    "equity": equity,
                    "availableBalance": available,
                    "cashBalance": withdrawable,
                    "unrealized": float(margin.get("totalNtlPos") or 0),
                }
            ]

        try:
            return await self._to_thread(_a)
        except Exception as e:
            raise HyperliquidError(f"user_state failed: {e}") from e

    async def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        def _p():
            if not self.account_address and not self.private_key:
                return []
            info = self._get_info()
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
            state = info.user_state(addr)
            rows = []
            coin_f = to_hl_coin(symbol) if symbol else None
            for ap in state.get("assetPositions") or []:
                pos = ap.get("position") or {}
                coin = str(pos.get("coin") or "").upper()
                if coin_f and coin != coin_f:
                    continue
                szi = float(pos.get("szi") or 0)
                if abs(szi) < 1e-12:
                    continue
                side = "long" if szi > 0 else "short"
                rows.append(
                    {
                        "positionId": coin,
                        "symbol": coin,
                        "positionType": 1 if side == "long" else 2,
                        "holdVol": abs(szi),
                        "holdAvgPrice": float(pos.get("entryPx") or 0),
                        "openAvgPrice": float(pos.get("entryPx") or 0),
                        "leverage": (pos.get("leverage") or {}).get("value")
                        if isinstance(pos.get("leverage"), dict)
                        else pos.get("leverage"),
                        "openType": 2
                        if isinstance(pos.get("leverage"), dict)
                        and pos.get("leverage", {}).get("type") == "cross"
                        else 1,
                        "unRealizedPnl": float(pos.get("unrealizedPnl") or 0),
                        "liquidatePrice": float(pos.get("liquidationPx") or 0)
                        if pos.get("liquidationPx") not in (None, "")
                        else None,
                        "im": float(pos.get("marginUsed") or 0),
                    }
                )
            return rows

        try:
            return await self._to_thread(_p)
        except Exception as e:
            raise HyperliquidError(f"positions failed: {e}") from e

    async def user_fills(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent executions of the account (userFills), newest first.

        Read-only /info call — also shows fills placed outside this app.
        Normalized shape for the UI: symbol, px, sz, side, time(ms), dir,
        closed_pnl, oid, fee.
        """

        def _f():
            if not self.account_address and not self.private_key:
                return []
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
                self.account_address = addr
            info = self._get_info()
            fills = info.user_fills(addr) or []
            coin_f = to_hl_coin(symbol) if symbol else None
            out: list[dict[str, Any]] = []
            for f in fills:
                coin = str(f.get("coin") or "").upper()
                if coin_f and coin != coin_f:
                    continue
                try:
                    out.append(
                        {
                            "symbol": coin,
                            "px": float(f.get("px") or 0),
                            "sz": float(f.get("sz") or 0),
                            "side": "buy" if str(f.get("side")) == "B" else "sell",
                            "time": int(f.get("time") or 0),
                            "dir": str(f.get("dir") or ""),
                            "closed_pnl": float(f["closedPnl"])
                            if f.get("closedPnl") not in (None, "")
                            else None,
                            "oid": f.get("oid"),
                            "fee": float(f.get("fee") or 0),
                        }
                    )
                except (TypeError, ValueError):
                    continue
            out.sort(key=lambda r: r["time"], reverse=True)
            return out[: max(1, int(limit))]

        try:
            return await self._to_thread(_f)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"user_fills failed: {e}") from e

    async def account_snapshot(self) -> dict[str, Any]:
        from app.mexc.client import map_account_snapshot

        assets = await self.assets()
        positions = await self.positions()
        return map_account_snapshot(assets, positions)

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        open_type: int,
        position_type: int | None = None,
        position_id: int | None = None,
    ) -> dict[str, Any]:
        def _l():
            ex = self._get_exchange()
            coin = to_hl_coin(symbol)
            is_cross = int(open_type) == 2
            return ex.update_leverage(int(leverage), coin, is_cross)

        try:
            return await self._to_thread(_l)
        except Exception as e:
            raise HyperliquidError(f"set_leverage failed: {e}") from e

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """Accept internal/MEXC-shaped body and map to HL SDK order/market_open."""

        def _place():
            ex = self._get_exchange()
            coin = to_hl_coin(str(body.get("symbol") or ""))
            side_raw = body.get("side")
            # MEXC open sides only: 1 long open, 3 short open; also long/short.
            # Reject close sides (2/4) and unknown values — bool(2) would wrongly buy.
            if side_raw in (1, "1", "long", "LONG"):
                is_buy = True
            elif side_raw in (3, "3", "short", "SHORT"):
                is_buy = False
            else:
                raise HyperliquidError(
                    f"unsupported order side {side_raw!r} "
                    "(use 1/long or 3/short; close via close_position_market)"
                )
            sz = float(body.get("vol") or body.get("sz") or 0)
            if sz <= 0:
                raise HyperliquidError("size/vol must be > 0")
            otype = body.get("type")
            is_market = otype in (5, "5", "market", "Market")
            reduce_only = bool(body.get("reduceOnly") or body.get("r") or False)
            px = float(body.get("price") or 0)

            # Tick rounding per HL rules (5 sig figs / max decimals via szDecimals)
            row = self._asset_row(coin)
            sz_dec = int(row.get("szDecimals") or 0)
            if px > 0:
                px = round_hl_price(px, sz_dec)

            sl = body.get("stopLossPrice")
            tp = body.get("takeProfitPrice")
            sl_px = round_hl_price(float(sl), sz_dec) if sl and float(sl) > 0 else None
            tp_px = round_hl_price(float(tp), sz_dec) if tp and float(tp) > 0 else None

            # Stamp OUR externalOid onto the exchange order as a Cloid so a
            # transport timeout can recover the real (possibly filled) order and
            # never bait a re-confirm into a double position.
            entry_cloid = external_oid_to_cloid(str(body.get("externalOid") or ""))

            if is_market:
                # Cap adverse fill: SDK crosses at mid*(1±slippage)
                result = ex.market_open(
                    coin,
                    is_buy,
                    sz,
                    px if px > 0 else None,
                    self.market_slippage,
                    cloid=entry_cloid,
                )
            else:
                if px <= 0:
                    raise HyperliquidError("limit order requires price > 0")
                result = ex.order(
                    coin,
                    is_buy,
                    sz,
                    px,
                    {"limit": {"tif": "Gtc"}},
                    reduce_only=reduce_only,
                    cloid=entry_cloid,
                )
            entry_err = _status_error(result)
            if entry_err:
                raise HyperliquidError(f"order rejected: {entry_err}", raw=result)

            # Attach TP/SL as reduce-only trigger orders.
            # Each result is checked for a real oid — no blind echo.
            trigger_errors: list[str] = []
            sl_trigger_oid = None
            tp_trigger_oid = None

            def _place_trigger(trigger_px: float, tpsl: str, trig_sz: float):
                return ex.order(
                    coin,
                    not is_buy,  # close direction
                    trig_sz,
                    trigger_px,
                    {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": trigger_px,
                            "tpsl": tpsl,
                        }
                    },
                    reduce_only=True,
                )

            if sl_px is not None:
                try:
                    sl_res = _place_trigger(sl_px, "sl", sz)
                    sl_trigger_oid = _extract_oid(sl_res)
                    err = _status_error(sl_res)
                    if sl_trigger_oid is None or err:
                        trigger_errors.append(f"sl: {err or 'no oid in response'}")
                        sl_trigger_oid = None
                except Exception as te:
                    trigger_errors.append(f"sl: {te}")

            # Optional TP ladder (scale-out): split the reduce-only TP across two
            # rungs (tp1 at tp_px, tp2 at tp2_px) instead of one. Falls back to a
            # single TP if the split would round a rung to zero size.
            tp2 = body.get("takeProfitPrice2")
            tp2_px = (
                round_hl_price(float(tp2), sz_dec) if tp2 and float(tp2) > 0 else None
            )
            share = float(body.get("tp1Share") or 0)
            vol_unit = 10 ** (-sz_dec) if sz_dec > 0 else 1.0
            do_ladder = tp_px is not None and tp2_px is not None and 0.0 < share < 1.0
            tp_sz1 = tp_sz2 = None
            if do_ladder:
                from app.risk.sizing import round_down_to_unit

                tp_sz1 = round_down_to_unit(sz * share, vol_unit)
                tp_sz2 = round_down_to_unit(sz - tp_sz1, vol_unit)
                if tp_sz1 <= 0 or tp_sz2 <= 0:
                    do_ladder = False  # too small to split -> single TP fallback

            tp_trigger_oid2 = None
            if tp_px is not None:
                try:
                    tp_res = _place_trigger(tp_px, "tp", tp_sz1 if do_ladder else sz)
                    tp_trigger_oid = _extract_oid(tp_res)
                    err = _status_error(tp_res)
                    if tp_trigger_oid is None or err:
                        trigger_errors.append(f"tp: {err or 'no oid in response'}")
                        tp_trigger_oid = None
                except Exception as te:
                    trigger_errors.append(f"tp: {te}")
            if do_ladder and tp2_px is not None:
                try:
                    tp2_res = _place_trigger(tp2_px, "tp", tp_sz2)
                    tp_trigger_oid2 = _extract_oid(tp2_res)
                    err = _status_error(tp2_res)
                    if tp_trigger_oid2 is None or err:
                        trigger_errors.append(f"tp2: {err or 'no oid in response'}")
                        tp_trigger_oid2 = None
                except Exception as te:
                    trigger_errors.append(f"tp2: {te}")

            return {
                "orderId": _extract_oid(result),
                "response": result,
                "slTriggerOid": sl_trigger_oid,
                "tpTriggerOid": tp_trigger_oid,
                "tpTriggerOid2": tp_trigger_oid2,
                "triggerErrors": trigger_errors,
                "requestedStopLoss": sl_px,
                "requestedTakeProfit": tp_px,
                "requestedTakeProfit2": tp2_px if do_ladder else None,
                "symbol": coin,
                "exchange": "hyperliquid",
            }

        try:
            return await self._to_thread(_place)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"place_order failed: {e}", raw=str(e)) from e

    async def place_stop_order(
        self,
        symbol: str,
        *,
        position_side: str,
        vol: float,
        trigger_px: float,
        tpsl: str = "sl",
        reduce_only: bool = True,
    ) -> dict[str, Any]:
        """Place ONE reduce-only trigger (SL/TP) for an ALREADY-OPEN position.

        position_side is the side of the open position; the trigger CLOSES it,
        so its direction is the opposite. Used by OrderService.modify_stop_loss
        to add a fresh protective stop BEFORE the old one is cancelled — the
        position is therefore never unprotected during a SL move.
        """

        def _place():
            ex = self._get_exchange()
            coin = to_hl_coin(symbol)
            pos = (position_side or "").lower()
            if pos not in ("long", "short"):
                raise HyperliquidError(
                    f"position_side must be long/short, got {position_side!r}"
                )
            sz = float(vol or 0)
            if sz <= 0:
                raise HyperliquidError("vol must be > 0")
            # long closes by selling (is_buy False); short closes by buying.
            is_buy_close = pos == "short"
            row = self._asset_row(coin)
            sz_dec = int(row.get("szDecimals") or 0)
            trg = round_hl_price(float(trigger_px), sz_dec)
            if trg <= 0:
                raise HyperliquidError("trigger_px must be > 0")
            result = ex.order(
                coin,
                is_buy_close,
                sz,
                trg,
                {"trigger": {"isMarket": True, "triggerPx": trg, "tpsl": tpsl}},
                reduce_only=bool(reduce_only),
            )
            err = _status_error(result)
            oid = _extract_oid(result)
            if err or oid is None:
                return {
                    "orderId": None,
                    "response": result,
                    "error": err or "no oid in response",
                    "requestedTrigger": trg,
                    "symbol": coin,
                }
            return {
                "orderId": oid,
                "response": result,
                "error": None,
                "requestedTrigger": trg,
                "symbol": coin,
            }

        try:
            return await self._to_thread(_place)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"place_stop_order failed: {e}", raw=str(e)) from e

    async def cancel_order(
        self, body: dict[str, Any] | list[Any]
    ) -> dict[str, Any] | list[Any]:
        def _c():
            ex = self._get_exchange()
            # list of oids or dict with orderId/symbol
            if isinstance(body, list):
                results = []
                for item in body:
                    if isinstance(item, dict):
                        oid = int(item.get("orderId") or item.get("oid"))
                        coin = to_hl_coin(str(item.get("symbol") or ""))
                    else:
                        # bare oid — need coin from open orders (caller should pass dict)
                        raise HyperliquidError(
                            "HL cancel needs {orderId, symbol} per order"
                        )
                    results.append(ex.cancel(coin, oid))
                return results
            oid = int(body.get("orderId") or body.get("oid"))
            coin = to_hl_coin(str(body.get("symbol") or ""))
            return ex.cancel(coin, oid)

        try:
            return await self._to_thread(_c)
        except Exception as e:
            raise HyperliquidError(f"cancel failed: {e}") from e

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        def _o():
            info = self._get_info()
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
            if not addr:
                return []
            orders = info.open_orders(addr)
            coin_f = to_hl_coin(symbol) if symbol else None
            out = []
            for o in orders or []:
                coin = str(o.get("coin") or "").upper()
                if coin_f and coin != coin_f:
                    continue
                out.append(
                    {
                        "orderId": o.get("oid"),
                        "symbol": coin,
                        # HL side is A=ask/sell, B=bid/buy
                        "side": o.get("side"),
                        "reduceOnly": bool(o.get("reduceOnly")),
                        "vol": o.get("sz"),
                        "price": o.get("limitPx"),
                        "raw": o,
                    }
                )
            return out

        try:
            return await self._to_thread(_o)
        except Exception as e:
            raise HyperliquidError(f"open_orders failed: {e}") from e

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open trigger (SL/TP) orders via frontend_open_orders (includes triggers)."""

        def _s():
            info = self._get_info()
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
            if not addr:
                return []
            try:
                orders = info.frontend_open_orders(addr)
            except Exception:
                orders = info.open_orders(addr)
            coin_f = to_hl_coin(symbol) if symbol else None
            out = []
            for o in orders or []:
                coin = str(o.get("coin") or "").upper()
                if coin_f and coin != coin_f:
                    continue
                otype = str(o.get("orderType") or "")
                is_trigger = bool(o.get("isTrigger")) or "stop" in otype.lower() or "take" in otype.lower()
                if not is_trigger:
                    continue
                out.append(
                    {
                        "orderId": o.get("oid"),
                        "symbol": coin,
                        "triggerPrice": o.get("triggerPx"),
                        "orderType": otype,
                        "reduceOnly": o.get("reduceOnly"),
                        "raw": o,
                    }
                )
            return out

        try:
            return await self._to_thread(_s)
        except Exception as e:
            raise HyperliquidError(f"open_stop_orders failed: {e}") from e

    async def close_position_market(
        self,
        symbol: str,
        *,
        side: str,
        vol: float,
        open_type: int = 1,
    ) -> dict[str, Any]:
        def _cl():
            ex = self._get_exchange()
            coin = to_hl_coin(symbol)
            want = (side or "").lower()
            if want not in ("long", "short"):
                raise HyperliquidError(
                    f"close side must be long/short, got {side!r}"
                )
            # ── F-08 TOCTOU guard: the service checked the side earlier, but the
            # SDK's market_close ignores `side` and closes WHATEVER position is
            # live. Re-read the live side/size immediately before closing; if the
            # position flipped externally between check and execution, refuse
            # instead of market-closing the new opposite side.
            info = self._get_info()
            addr = self._resolve_address()
            state = info.user_state(addr) if addr else {}
            live_szi = 0.0
            for ap in (state or {}).get("assetPositions") or []:
                pos = ap.get("position") or {}
                if str(pos.get("coin") or "").upper() == coin:
                    live_szi = float(pos.get("szi") or 0)
                    break
            live_side = (
                "long" if live_szi > 0 else ("short" if live_szi < 0 else None)
            )
            if live_side is None:
                raise HyperliquidError(
                    f"no open {want} position on {coin} to close"
                )
            if live_side != want:
                raise HyperliquidError(
                    f"refusing close: requested {want} but live position is "
                    f"{live_side} (side flipped externally) — not closing the "
                    "wrong side"
                )
            live_sz = abs(live_szi)
            close_sz = (
                min(float(vol), live_sz) if vol and float(vol) > 0 else live_sz
            )
            if close_sz <= 0:
                raise HyperliquidError("close size resolved to 0")
            # Emergency close: allow wider slippage so the flatten actually fills.
            slippage = max(0.01, self.market_slippage)
            # market_close is a reduce-only IOC close of the (now-verified) side.
            result = ex.market_close(coin, sz=close_sz, slippage=slippage)
            # ── F-03: HL returns order rejections INSIDE an outwardly-ok
            # response. Treat an inner error as a FAILED close so the service
            # never logs `closed` / answers ok while the position is still open.
            err = _status_error(result)
            if err:
                raise HyperliquidError(f"close rejected: {err}", raw=result)
            return result

        try:
            return await self._to_thread(_cl)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"close failed: {e}") from e

    async def order_by_external_oid(self, symbol: str, external_oid: str) -> Any:
        """Find OUR order (open or filled) by externalOid, via its Cloid.

        Primary: orderStatus-by-cloid returns resting AND filled orders, so a
        market order that already filled during a transport timeout is found —
        this is what prevents a re-confirm double position. Fallback: scan open
        + trigger orders for the cloid. Returns {} when nothing matches
        (fail-closed: recovery must not false-positive).
        """
        oid = (external_oid or "").strip()
        if not oid:
            return {}
        cloid = external_oid_to_cloid(oid)
        cloid_raw = cloid.to_raw() if cloid is not None else None

        if cloid is not None:
            def _q():
                addr = self._resolve_address()
                if not addr:
                    return None
                return self._get_info().query_order_by_cloid(addr, cloid)

            try:
                status = await self._to_thread(_q)
            except HyperliquidError:
                status = None
            if isinstance(status, dict) and str(status.get("status")).lower() == "order":
                # Echo externalOid so service.py's substring recovery guard passes.
                return {
                    "externalOid": oid,
                    "cloid": cloid_raw,
                    "match": "cloid",
                    "order": status.get("order"),
                    "raw": status,
                }

        orders = await self.open_orders(symbol)
        try:
            stops = await self.open_stop_orders(symbol)
        except Exception:
            stops = []
        wanted = {oid}
        if cloid_raw:
            wanted.add(cloid_raw)
        hits: list[dict[str, Any]] = []
        for o in list(orders or []) + list(stops or []):
            if not isinstance(o, dict):
                continue
            raw = o.get("raw") if isinstance(o.get("raw"), dict) else {}
            matched = False
            for src in (o, raw):
                for key in (
                    "cloid",
                    "clientOrderId",
                    "client_order_id",
                    "externalOid",
                    "external_oid",
                ):
                    val = src.get(key)
                    if val is not None and str(val) in wanted:
                        hits.append({**o, "externalOid": oid})
                        matched = True
                        break
                if matched:
                    break
        return hits if hits else {}


def _status_error(result: Any) -> str | None:
    """Return error text from an HL order response, or None if accepted."""
    if not isinstance(result, dict):
        return None
    if result.get("status") not in (None, "ok"):
        return str(result.get("status"))
    try:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        for st in statuses:
            if isinstance(st, dict) and "error" in st:
                return str(st["error"])
    except Exception:
        return None
    return None


def _extract_oid(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    # common SDK shape: {"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":...}}]}}}
    try:
        statuses = (
            result.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        if statuses:
            st0 = statuses[0]
            if "resting" in st0:
                return st0["resting"].get("oid")
            if "filled" in st0:
                return st0["filled"].get("oid")
            if "error" in st0:
                return None
    except Exception:
        pass
    return result.get("orderId") or result.get("oid")
