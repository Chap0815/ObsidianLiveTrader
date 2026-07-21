"""MEXC USDT-M futures REST client (public + signed private).

Auth: Signature = hex HMAC-SHA256(accessKey + requestTime + paramString)
  - GET/DELETE: paramString = sorted k=v&... (skip nulls)
  - POST: paramString = raw JSON body (separators comma/colon, no spaces)

Order place path (current official docs): POST /api/v1/private/order/create
Older docs still list /submit as under maintenance — code uses /create.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from app.mexc.errors import MexcError
from app.models import Candle, ContractMeta, FundingRate, Ticker

INTERVAL_MAP: dict[str, str] = {
    "5m": "Min5",
    "15m": "Min15",
    "1H": "Min60",
    "4H": "Hour4",
    "1D": "Day1",
}

# Seconds per UI interval (for kline start window from limit_hint)
_INTERVAL_SECONDS: dict[str, int] = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1H": 60 * 60,
    "4H": 4 * 60 * 60,
    "1D": 24 * 60 * 60,
    "Min5": 5 * 60,
    "Min15": 15 * 60,
    "Min60": 60 * 60,
    "Hour4": 4 * 60 * 60,
    "Day1": 24 * 60 * 60,
}


def sign_payload(
    access_key: str, secret_key: str, req_time: str, param_string: str
) -> str:
    target = f"{access_key}{req_time}{param_string}"
    return hmac.new(
        secret_key.encode("utf-8"),
        target.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sorted_query(params: dict[str, Any] | None) -> str:
    """MEXC GET signature string: keys sorted, values URL-encoded (space as %20)."""
    from urllib.parse import quote

    if not params:
        return ""
    items: list[str] = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        items.append(f"{k}={quote(str(v), safe='')}")
    return "&".join(items)


# Body keys that carry an absolute price and go straight into the signed
# POST body — every one of them must be formatted decimal (never `e`
# notation) before json.dumps, or a low-price coin (SHIB/PEPE-style
# sub-cent price) can be rejected/misinterpreted by the exchange parser.
_PRICE_FIELDS = (
    "price",
    "stopLossPrice",
    "takeProfitPrice",
    "takeProfitPrice2",
    "triggerPrice",
)

# Fallback decimal places when no exchange priceScale/priceUnit is known for
# the symbol. Generous enough to preserve any realistic contract price while
# still guaranteeing a plain fixed-point string (trailing zeros are trimmed).
_DEFAULT_PRICE_DECIMALS = 8


def _fmt_price(v: Any, scale: int | None = None) -> str:
    """Format a price as a fixed-point decimal string, never scientific notation.

    The MEXC POST signature is computed over the raw JSON text of the body
    (see module docstring), and Python's default float formatting switches
    to scientific notation for small magnitudes (``json.dumps(0.00002)`` ->
    ``"2e-05"``). That is still a *consistent* string for signing purposes,
    but MEXC's exchange-side JSON parser can reject or misread it for
    low-price coins. Converting the value to a quoted decimal string before
    ``json.dumps`` keeps the exact same sign-then-send flow (the string
    itself is what gets signed) while guaranteeing the wire format MEXC
    expects.

    ``scale`` is the number of decimal places to keep, normally the
    contract's ``priceScale`` (or one derived from ``priceUnit``); when
    unknown, a conservative fixed fallback is used instead.
    """
    if not math.isfinite(float(v)):
        raise MexcError(f"Nicht-endlicher Preiswert: {v!r}")
    d = Decimal(str(v))
    was_positive = d > 0
    decimals = scale if scale is not None else _DEFAULT_PRICE_DECIMALS
    if decimals < 0:
        decimals = 0
    quant = Decimal(1).scaleb(-decimals)
    d = d.quantize(quant, rounding=ROUND_DOWN)
    # F-3 safety leine: a positive price/SL smaller than the quantization step
    # ROUND_DOWNs to 0 here. Shipping "0" for a stopLossPrice while the service
    # still believes body_had_sl=True is a SILENT stop-loss loss ("protected"
    # report, no real stop). Reject hard instead — independent of whether a
    # contract priceScale was threaded in (a legitimate 0, e.g. a market-order
    # price field, is never "positive" so it passes untouched).
    if was_positive and d <= 0:
        raise MexcError(
            f"Preis {v!r} kollabiert bei Quantisierung (scale={decimals}) auf 0 "
            "— harter Reject statt stillem SL-/Preis-Verlust auf der Wire"
        )
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _format_price_fields(body: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``body`` with every known price key formatted via
    :func:`_fmt_price`. Non-price keys (vol, leverage, side, ...) are left
    untouched so they stay bare JSON numbers."""
    out = dict(body)
    for key in _PRICE_FIELDS:
        if key in out and out[key] is not None:
            out[key] = _fmt_price(out[key])
    return out


def _to_ms(ts: int | float) -> int:
    """MEXC kline time is seconds; chart libs expect ms."""
    t = int(ts)
    return t if t >= 1_000_000_000_000 else t * 1000


def normalize_klines(data: dict[str, Any]) -> list[Candle]:
    """Convert MEXC parallel-array kline payload to list[Candle]."""
    times = data.get("time") or []
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    vols = data.get("vol") or []
    amounts = data.get("amount") or []
    n = len(times)
    candles: list[Candle] = []
    for i in range(n):
        candles.append(
            Candle(
                time=_to_ms(times[i]),
                open=float(opens[i]) if i < len(opens) else 0.0,
                high=float(highs[i]) if i < len(highs) else 0.0,
                low=float(lows[i]) if i < len(lows) else 0.0,
                close=float(closes[i]) if i < len(closes) else 0.0,
                vol=float(vols[i]) if i < len(vols) else 0.0,
                amount=float(amounts[i]) if i < len(amounts) else 0.0,
            )
        )
    return candles


def parse_contract_meta(row: dict[str, Any]) -> ContractMeta:
    return ContractMeta(
        symbol=str(row.get("symbol", "")),
        contract_size=float(row.get("contractSize") or 0),
        price_unit=float(row.get("priceUnit") or 0),
        vol_unit=float(row.get("volUnit") or 0),
        min_vol=float(row.get("minVol") or 0),
        max_vol=float(row.get("maxVol") or 0),
        max_leverage=int(row.get("maxLeverage") or 1),
        min_leverage=int(row.get("minLeverage") or 1),
        api_allowed=bool(row.get("apiAllowed", False)),
        price_scale=row.get("priceScale"),
        vol_scale=row.get("volScale"),
        base_coin=row.get("baseCoin"),
        quote_coin=row.get("quoteCoin"),
        state=row.get("state"),
    )


class MexcClient:
    # Exchange identity — used by the order service to gate MEXC-specific SL
    # verification (O-01/O-02: position-bound SL, empty plan list ≠ missing SL).
    exchange_id = "mexc"

    def __init__(self, base_url: str, api_key: str = "", api_secret: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        # O-04: remembers which open_stop_orders candidate path last answered
        # with a recognized schema, so the next call tries it first (fallback
        # order is unchanged — this is purely an ordering hint).
        self._stop_path_cache: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        private: bool = False,
    ) -> Any:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        req_params = params
        content: str | None = None
        param_string = ""

        method_u = method.upper()
        if method_u in ("GET", "DELETE"):
            param_string = sorted_query(params)
        elif json_body is not None:
            body_to_send: Any = json_body
            if isinstance(json_body, dict):
                body_to_send = _format_price_fields(json_body)
            content = json.dumps(body_to_send, separators=(",", ":"))
            param_string = content

        if private:
            if not self.api_key or not self.api_secret:
                raise MexcError("MEXC API keys not configured")
            req_time = str(int(time.time() * 1000))
            headers.update(
                {
                    "ApiKey": self.api_key,
                    "Request-Time": req_time,
                    "Signature": sign_payload(
                        self.api_key, self.api_secret, req_time, param_string
                    ),
                    "Recv-Window": "10000",
                }
            )

        try:
            r = await self._client.request(
                method_u, path, params=req_params, content=content, headers=headers
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise MexcError(
                f"HTTP {e.response.status_code}: {e.response.text[:300]}",
                raw={"status": e.response.status_code, "body": e.response.text},
            ) from e
        except httpx.HTTPError as e:
            raise MexcError(f"HTTP error: {e}") from e

        try:
            data = r.json()
        except ValueError as e:
            # 2xx with an empty/truncated/non-JSON body. The order may still
            # be LIVE on the exchange (e.g. place-order accepted but response
            # body dropped) — this must surface as a recoverable MexcError,
            # never an unhandled crash that hides a possibly-live order.
            raise MexcError(
                f"invalid JSON in 2xx response body: {e}",
                raw={"status": r.status_code, "body": r.text[:300]},
            ) from e
        if isinstance(data, dict) and data.get("success") is False:
            raise MexcError(
                str(data.get("message") or data.get("code") or "MEXC error"),
                raw=data,
            )
        # F-2: MEXC can answer HTTP 200 with {"code": <nonzero>, "message": ...}
        # and NO "success" field (gateway/maintenance/rate-limit variants). The
        # success-only check above would pass such an error through as a result
        # (fatal on place_order). Mirror _close_response_error's code guard, but
        # ONLY as a FALLBACK when success is absent: an explicit success:true is
        # authoritative (a legitimate answer that also carries a non-zero code
        # must NOT be blocked in the money path), and success:false is already
        # caught above. So gate on `success is None`. A missing code or the
        # success codes (0/200) are legitimate answers and must not be rejected.
        if isinstance(data, dict) and data.get("success") is None:
            code = data.get("code")
            if code not in (None, 0, "0", 200, "200"):
                raise MexcError(
                    str(
                        data.get("message")
                        or data.get("msg")
                        or f"MEXC error code={code}"
                    ),
                    raw=data,
                )
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    # ── public ──────────────────────────────────────────────────────────

    async def ping(self) -> int:
        """Server time in ms."""
        data = await self._request("GET", "/api/v1/contract/ping")
        return int(data)

    async def contract_detail(
        self, symbol: str | None = None
    ) -> dict[str, Any] | list[Any]:
        params = {"symbol": symbol} if symbol else None
        return await self._request("GET", "/api/v1/contract/detail", params=params)

    async def list_symbols(self) -> list[str]:
        """All USDT-M perpetual symbols (for the UI symbol dropdown)."""
        data = await self.contract_detail()
        rows = data if isinstance(data, list) else [data]
        out = []
        for r in rows:
            sym = str(r.get("symbol") or "")
            if sym.endswith("_USDT"):
                out.append(sym)
        return sorted(out)

    async def market_overview(self, limit: int = 12) -> list[dict[str, Any]]:
        """Top USDT-perps by 24h turnover with funding/last (for the scanner)."""
        data = await self._request("GET", "/api/v1/contract/ticker")
        rows: list[dict[str, Any]] = []
        for r in data if isinstance(data, list) else [data]:
            sym = str(r.get("symbol") or "")
            if not sym.endswith("_USDT"):
                continue
            # Task 24 universe fields. riseFallRate is MEXC's 24h price-change
            # FRACTION (e.g. 0.05 = +5%) -> ×100 for a percent; holdVol is the
            # OI LEVEL in contracts (MEXC exposes no OI-Δ, so this alone never
            # triggers the scanner oi_read and classic stays byte-for-byte).
            rfr = _opt_float(r.get("riseFallRate"))
            rows.append(
                {
                    "symbol": sym,
                    "volume24": _f(r.get("amount24")),
                    "funding": _f(r.get("fundingRate")),
                    "last": _f(r.get("lastPrice")),
                    "price_change_pct": (rfr * 100.0) if rfr is not None else None,
                    "open_interest": _opt_float(r.get("holdVol")),
                }
            )
        rows.sort(key=lambda x: x["volume24"], reverse=True)
        return rows[: max(1, int(limit))]

    async def contract_meta(self, symbol: str) -> ContractMeta:
        data = await self.contract_detail(symbol)
        if isinstance(data, list):
            if not data:
                raise MexcError(f"No contract detail for {symbol}")
            row = data[0]
        else:
            row = data
        return parse_contract_meta(row)

    async def klines(
        self, symbol: str, interval: str, limit_hint: int = 200, *, paced: bool = False
    ) -> list[Candle]:
        # `paced` is accepted for interface parity with HyperliquidClient (the
        # scanner passes it uniformly); MEXC has no HL-style read-rate budget, so
        # it's a no-op here (its fan-out is bounded by the scanner's semaphore).
        _ = paced
        mexc_interval = INTERVAL_MAP.get(interval, interval)
        params: dict[str, Any] = {"interval": mexc_interval}
        sec = _INTERVAL_SECONDS.get(interval) or _INTERVAL_SECONDS.get(mexc_interval)
        if sec and limit_hint > 0:
            # start is seconds; cap window so we roughly get limit_hint bars
            end = int(time.time())
            start = end - sec * int(limit_hint)
            params["start"] = start
            params["end"] = end
        data = await self._request(
            "GET", f"/api/v1/contract/kline/{symbol}", params=params
        )
        if not isinstance(data, dict):
            raise MexcError("Unexpected kline payload", raw=data)
        candles = normalize_klines(data)
        if limit_hint > 0 and len(candles) > limit_hint:
            candles = candles[-limit_hint:]
        return candles

    async def ticker(self, symbol: str) -> Ticker:
        data = await self._request(
            "GET", "/api/v1/contract/ticker", params={"symbol": symbol}
        )
        if isinstance(data, list):
            row = next((x for x in data if x.get("symbol") == symbol), None)
            if row is None:
                raise MexcError(
                    f"Ticker for {symbol} not found (refusing other-symbol fallback)"
                )
        else:
            row = data or {}
            if row.get("symbol") and str(row.get("symbol")) != symbol:
                raise MexcError(
                    f"Ticker symbol mismatch: wanted {symbol}, got {row.get('symbol')}"
                )
        return Ticker(
            symbol=str(row.get("symbol") or symbol),
            last_price=float(row.get("lastPrice") or 0),
            bid1=_opt_float(row.get("bid1")),
            ask1=_opt_float(row.get("ask1")),
            fair_price=_opt_float(row.get("fairPrice")),
            index_price=_opt_float(row.get("indexPrice")),
            volume24=_opt_float(row.get("volume24")),
            amount24=_opt_float(row.get("amount24")),
            funding_rate=_opt_float(row.get("fundingRate")),
            timestamp=_opt_int(row.get("timestamp")),
        )

    async def funding_rate(self, symbol: str) -> FundingRate:
        data = await self._request(
            "GET", f"/api/v1/contract/funding_rate/{symbol}"
        )
        row = data or {}
        return FundingRate(
            symbol=str(row.get("symbol") or symbol),
            funding_rate=float(row.get("fundingRate") or 0),
            max_funding_rate=_opt_float(row.get("maxFundingRate")),
            min_funding_rate=_opt_float(row.get("minFundingRate")),
            collect_cycle=_opt_int(row.get("collectCycle")),
            next_settle_time=_opt_int(row.get("nextSettleTime")),
            timestamp=_opt_int(row.get("timestamp")),
        )

    # ── private ─────────────────────────────────────────────────────────

    async def assets(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        # `fresh` is accepted for interface parity with HyperliquidClient (money
        # reads request it); MEXC issues a live request every call and has no
        # stale-serve cache, so it's already effectively fresh — no-op.
        _ = fresh
        data = await self._request(
            "GET", "/api/v1/private/account/assets", private=True
        )
        return list(data or [])

    async def positions(
        self, symbol: str | None = None, *, fresh: bool = False
    ) -> list[dict[str, Any]]:
        """GET /api/v1/private/position/open_positions"""
        _ = fresh  # interface parity — MEXC has no stale-serve cache (see assets)
        params = {"symbol": symbol} if symbol else None
        data = await self._request(
            "GET",
            "/api/v1/private/position/open_positions",
            params=params,
            private=True,
        )
        return list(data or [])

    async def account_snapshot(self, *, fresh: bool = False) -> dict[str, Any]:
        """Fetch assets + open positions and map to API account shape.

        Each position gets its OWN contract_size (F-10) — resolved from
        MEXC's contract metadata (one /contract/detail call covering all
        symbols) rather than the active chart symbol's. If that lookup
        fails or a symbol is missing from it, map_position safely defaults
        that position's contract_size to 1.0.
        """
        assets_raw = await self.assets(fresh=fresh)
        positions_raw = await self.positions(fresh=fresh)
        contract_sizes: dict[str, float] = {}
        if positions_raw:
            try:
                detail = await self.contract_detail()
                rows = detail if isinstance(detail, list) else [detail]
                for r in rows:
                    sym = str(r.get("symbol") or "")
                    if not sym:
                        continue
                    contract_sizes[sym] = float(r.get("contractSize") or 0) or 1.0
            except MexcError:
                pass  # per-position default (1.0) still applies below
        return map_account_snapshot(
            assets_raw, positions_raw, contract_sizes=contract_sizes
        )

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        open_type: int,
        position_type: int | None = None,
        position_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/private/position/change_leverage

        With open position: pass position_id (+ leverage).
        Without: symbol + openType + positionType + leverage.
        """
        body: dict[str, Any] = {"leverage": leverage}
        if position_id is not None:
            body["positionId"] = position_id
        else:
            body["symbol"] = symbol
            body["openType"] = open_type
            if position_type is not None:
                body["positionType"] = position_type
        data = await self._request(
            "POST",
            "/api/v1/private/position/change_leverage",
            json_body=body,
            private=True,
        )
        return data if isinstance(data, dict) else {"data": data}

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/private/order/create (current official path).

        Typical fields: symbol, price, vol, side, type, openType, leverage,
        optional externalOid, stopLossPrice, takeProfitPrice.
        side: 1 open long, 2 close short, 3 open short, 4 close long
        type: 1 limit, 5 market (see MEXC docs)
        openType: 1 isolated, 2 cross
        """
        data = await self._request(
            "POST",
            "/api/v1/private/order/create",
            json_body=body,
            private=True,
        )
        return data if isinstance(data, dict) else {"data": data}

    async def cancel_order(
        self, body: dict[str, Any] | list[Any]
    ) -> dict[str, Any] | list[Any]:
        """POST /api/v1/private/order/cancel

        Official body is often a list of order ids (max 50).
        Also accepts a dict wrapper if callers prefer that shape.
        """
        return await self._request(
            "POST",
            "/api/v1/private/order/cancel",
            json_body=body,
            private=True,
        )

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET open orders — path per current MEXC futures docs."""
        params: dict[str, Any] = {"page_num": 1, "page_size": 100}
        if symbol:
            params["symbol"] = symbol
        data = await self._request(
            "GET",
            "/api/v1/private/order/list/open_orders",
            params=params,
            private=True,
        )
        if isinstance(data, dict) and "resultList" in data:
            return list(data.get("resultList") or [])
        if isinstance(data, list):
            return data
        return list(data or []) if data else []

    # C3-01: page size / page cap for user_fills paging (mirrors the
    # history_orders pattern above — widen past a single page only as far as
    # `limit` actually needs).
    _FILLS_PAGE_SIZE = 100
    _FILLS_MAX_PAGES = 5

    async def user_fills(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent executions (deals) for the account, newest first.

        Read-only GET .../order/list/order_deals — MEXC's fill-history
        equivalent of Hyperliquid's userFills. Normalized to the SAME shape
        the UI/marker layer consumes (see normalize_mexc_fill /
        hyperliquid.client.user_fills): symbol, px, sz, side, time(ms), dir,
        closed_pnl, oid, fee. Paged like history_orders (widen only as far
        as `limit` needs, capped at _FILLS_MAX_PAGES pages).
        """
        page_size = min(max(int(limit), 1), self._FILLS_PAGE_SIZE)
        raw_rows: list[Any] = []
        for page_num in range(1, self._FILLS_MAX_PAGES + 1):
            params: dict[str, Any] = {"page_num": page_num, "page_size": page_size}
            if symbol:
                params["symbol"] = symbol
            data = await self._request(
                "GET",
                "/api/v1/private/order/list/order_deals",
                params=params,
                private=True,
            )
            if isinstance(data, dict) and "resultList" in data:
                page_rows = list(data.get("resultList") or [])
            elif isinstance(data, list):
                page_rows = data
            else:
                page_rows = [data] if data else []
            raw_rows.extend(page_rows)
            if len(page_rows) < page_size or len(raw_rows) >= limit:
                break

        out: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            try:
                out.append(normalize_mexc_fill(row))
            except (TypeError, ValueError):
                continue  # graceful degrade: skip, never fabricate a fill
        out.sort(key=lambda r: r["time"], reverse=True)
        return out[: max(1, int(limit))]

    # Known MEXC futures stop/plan-order list paths across doc revisions.
    # NEEDS LIVE VERIFICATION: tried in order, first that responds wins.
    _STOP_ORDER_PATHS = (
        "/api/v1/private/stoporder/list/orders",
        "/api/v1/private/planorder/list/orders",
        "/api/v1/private/stoporder/list/open_orders",
        "/api/v1/private/stoporder/orders",
        "/api/v1/private/planorder/orders",
    )

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET open stop / plan orders.

        The exact path varies by MEXC revision and is not guaranteed by a live
        test here — try several known candidates and return the first that
        answers with a RECOGNIZED order-list schema (a bare list, or a dict
        containing "resultList" — an empty list/resultList is still trusted,
        that's a genuine "no stop orders" answer).

        A stale/deprecated endpoint that responds 2xx with an unrecognized
        shape (e.g. `{}` or `null` instead of erroring) is NOT proof of "no
        stop orders" — it is treated the same as a failing candidate, so if
        every candidate is either an error or unrecognized, the last error is
        propagated (never a silent []) and the SL-verify path treats the check
        as UNKNOWN, never MISSING.
        """
        params: dict[str, Any] = {"page_num": 1, "page_size": 100}
        if symbol:
            params["symbol"] = symbol
        last_err: MexcError | None = None

        # O-04: try the last-known-working path first (pure ordering hint —
        # the fallback order over the remaining candidates is unchanged).
        ordered_paths = list(self._STOP_ORDER_PATHS)
        cached = self._stop_path_cache
        if cached is not None and cached in ordered_paths:
            ordered_paths.remove(cached)
            ordered_paths.insert(0, cached)

        for path in ordered_paths:
            try:
                data = await self._request("GET", path, params=params, private=True)
            except MexcError as e:
                last_err = e
                continue
            if isinstance(data, dict) and "resultList" in data:
                self._stop_path_cache = path
                return list(data.get("resultList") or [])
            if isinstance(data, list):
                self._stop_path_cache = path
                return data
            # Unrecognized schema — do not trust as "no stop orders"; the
            # endpoint may be stale/deprecated. Keep trying other candidates.
            last_err = MexcError(
                f"unrecognized stop-order response shape from {path}", raw=data
            )
        if last_err is not None:
            raise last_err
        return []

    async def close_position_market(
        self,
        symbol: str,
        *,
        side: str,
        vol: float,
        open_type: int = 1,
        external_oid: str | None = None,
    ) -> dict[str, Any]:
        """Emergency flatten: close long=side 4, close short=side 2, type market.

        X2-06: an optional deterministic ``external_oid`` is stamped onto the
        order (namespaced ``close:`` so it never collides with the entry oid) so
        a transport timeout during a close can be recovered/looked-up
        unambiguously via ``order_by_external_oid`` instead of guessing.
        """
        mexc_side = 4 if side == "long" else 2
        body = {
            "symbol": symbol,
            "price": 0,
            "vol": vol,
            "side": mexc_side,
            "type": 5,
            "openType": open_type,
        }
        if external_oid:
            body["externalOid"] = f"close:{external_oid}"
        return await self.place_order(body)

    # O-09: history fallback window — widened from the old page_size=20 and
    # paged up to a cap, so a fill that lands past the first 20 rows (e.g.
    # after other activity on the account) is still found.
    _HISTORY_PAGE_SIZE = 100
    _HISTORY_MAX_PAGES = 3

    # X2-03 settle-retry: a just-filled order can be absent from BOTH history and
    # open during MEXC index-lag. Re-poll history+open a few times (short delay)
    # before giving up ({} → caller fail-closes to a hard error). Instance-level
    # so tests can zero the delay.
    _RECOVERY_ATTEMPTS = 3
    _RECOVERY_RETRY_DELAY_S = 1.0

    async def _history_row_by_external_oid(
        self, symbol: str, external_oid: str
    ) -> dict[str, Any] | None:
        """Page through history_orders looking for external_oid. Fail-closed:
        any request error just stops the scan (never raises) — the caller
        still has the open_orders fallback."""
        for page_num in range(1, self._HISTORY_MAX_PAGES + 1):
            try:
                data = await self._request(
                    "GET",
                    "/api/v1/private/order/list/history_orders",
                    params={
                        "symbol": symbol,
                        "page_num": page_num,
                        "page_size": self._HISTORY_PAGE_SIZE,
                    },
                    private=True,
                )
            except MexcError:
                break
            if isinstance(data, dict) and "resultList" in data:
                rows = data.get("resultList") or []
            elif isinstance(data, list):
                rows = data
            else:
                rows = [data] if data else []
            for r in rows:
                if isinstance(r, dict) and str(
                    r.get("externalOid") or r.get("external_oid") or ""
                ) == str(external_oid):
                    return r
            if len(rows) < self._HISTORY_PAGE_SIZE:
                break  # short page — no more data to page through
        return None

    async def order_by_external_oid(self, symbol: str, external_oid: str) -> Any:
        """Lookup order by client externalOid (idempotency / timeout recovery).

        Tries, in order:
        1. A guessed direct external-oid endpoint (path varies across MEXC
           doc revisions — a 404 here is routine/expected and must NOT
           propagate as an error; fall through to the fallbacks instead).
        2. The history-orders list, widened + paged (see
           `_history_row_by_external_oid`) — catches fills that a single
           page_size=20 call could miss.
        3. The open-orders list — catches an order that is live but not (yet)
           in history.

        X2-03 SETTLE-RETRY: steps 2+3 are re-polled `_RECOVERY_ATTEMPTS` times
        (~`_RECOVERY_RETRY_DELAY_S` apart) because a just-filled order can be
        absent from BOTH lists during MEXC's index-lag window; a premature `{}`
        would send the caller to a hard error → re-preview → double position.

        X2-04 STATE-FILTER: a match is only reported LIVE when its MEXC order
        `state` is not terminal-dead — cancelled(4)/invalid(5) → `{}`, so a
        cancelled order is never reported "recovered". Unknown/missing state is
        NOT treated as dead (over-rejecting a genuine fill would reopen X2-03).

        Every live match is tagged with a marker so a later reconciliation step
        can tell WHERE the order was found:
            {"match": "direct"|"history"|"open", "externalOid": external_oid, "order": <raw>}
        ("direct" = guessed direct-lookup endpoint — weaker, substring-based
        evidence than the paged/field-filtered history list.)
        No live match anywhere -> `{}` (fail-closed: never a fabricated match).
        """
        try:
            direct = await self._request(
                "GET",
                "/api/v1/private/order/external/" + external_oid,
                params={"symbol": symbol},
                private=True,
            )
        except MexcError:
            direct = None
        # Fail-closed: only trust the direct answer if OUR oid actually appears
        # in the raw payload — the wrapper below injects externalOid itself, so
        # without this check a garbage/unrelated 2xx response would fabricate a
        # "match" that the caller can no longer detect as bogus.
        if direct and str(external_oid) in str(direct):
            if _mexc_state_is_dead(direct):
                return {}  # X2-04: cancelled/invalid — not live
            return {"match": "direct", "externalOid": external_oid, "order": direct}

        attempts = max(1, int(getattr(self, "_RECOVERY_ATTEMPTS", 3)))
        delay_s = max(0.0, float(getattr(self, "_RECOVERY_RETRY_DELAY_S", 1.0)))
        for attempt in range(attempts):
            hist_row = await self._history_row_by_external_oid(symbol, external_oid)
            if hist_row is not None:
                if _mexc_state_is_dead(hist_row):
                    return {}  # X2-04: terminal-dead state is definitive → no retry
                return {
                    "match": "history",
                    "externalOid": external_oid,
                    "order": hist_row,
                }

            try:
                open_rows = await self.open_orders(symbol)
            except MexcError:
                open_rows = []
            for r in open_rows:
                if isinstance(r, dict) and str(
                    r.get("externalOid") or r.get("external_oid") or ""
                ) == str(external_oid):
                    if _mexc_state_is_dead(r):
                        return {}  # X2-04
                    return {"match": "open", "externalOid": external_oid, "order": r}

            # Not found in either list yet — wait for the index to catch up,
            # unless this was the last attempt.
            if attempt < attempts - 1 and delay_s > 0:
                await asyncio.sleep(delay_s)

        return {}


# MEXC futures order-state codes: 1=uninformed, 2=uncompleted, 3=completed,
# 4=cancelled, 5=invalid. X2-04: a recovery match may only be reported LIVE for
# the non-terminal-dead states. Only an EXPLICIT cancelled/invalid marker kills a
# match — unknown/missing state is NOT dead (over-rejecting a genuine index-lag
# fill would reopen X2-03: hard error → re-preview → double position).
_MEXC_DEAD_STATES = frozenset({"4", "5", "cancelled", "canceled", "invalid"})


def _mexc_state_is_dead(row: Any) -> bool:
    """True only if `row` carries an explicit MEXC cancelled(4)/invalid(5) state."""
    if not isinstance(row, dict):
        return False
    raw = row.get("state")
    if raw is None:
        raw = row.get("orderState")
    if raw is None:
        raw = row.get("order_state")
    if raw is None:
        return False
    return str(raw).strip().lower() in _MEXC_DEAD_STATES


def _opt_float(v: Any) -> float | None:
    """float(v) or None — mirrors Hyperliquid's `_opt_f`: MEXC blanks optional
    numeric fields as "" (not just null/omitted), e.g. fairPrice/liquidatePrice/
    im/marginRatio/funding fields. A bare `float(v)` raises ValueError on ""
    (or on garbage), which is not a MexcError and escapes the ExchangeError
    handlers upstream — degrade to None instead of crashing the poll cycle."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any) -> int | None:
    """int(v) or None — same "" / garbage guard as `_opt_float`, for the raw
    int(...) timestamp/cycle fields (timestamp, collectCycle, nextSettleTime)
    that previously only checked `is not None`."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# C3-01: MEXC deal `side` codes (same codes place_order takes) -> the same
# free-text "Open Long"/"Close Short"-style dir Hyperliquid's user_fills
# emits, so classifyFillDir() in the frontend needs no MEXC special-case.
# NOTE: MEXC deals carry no liquidation flag — a forced-liquidation close
# still normalizes to a plain "Close ..." dir here (never fabricated as
# "Liquidated ..."), so classifyFillDir will bucket it as "close", not "liq".
_MEXC_FILL_DIR = {
    1: "Open Long",
    2: "Close Short",
    3: "Open Short",
    4: "Close Long",
}


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """First present, non-null, numeric-parseable value across candidate
    field names (MEXC doc revisions vary the exact key)."""
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None


def normalize_mexc_fill(row: dict[str, Any]) -> dict[str, Any]:
    """Map one MEXC order_deals row to the SAME normalized fill shape
    Hyperliquid's user_fills produces (see hyperliquid/client.py): symbol,
    px, sz, side, time(ms), dir, closed_pnl, oid, fee. Raises
    TypeError/ValueError if px/sz/time can't be resolved — callers must skip
    that row rather than fabricate a fill.
    """
    px = _first_float(row, ("price", "dealPrice", "avgPrice"))
    sz = _first_float(row, ("vol", "dealVol", "dealVolume"))
    t = _first_float(row, ("timestamp", "dealTime", "createTime", "time"))
    if px is None or sz is None or t is None:
        raise ValueError("MEXC deal row missing px/sz/time")

    side_i: int | None
    try:
        side_i = int(row.get("side"))
    except (TypeError, ValueError):
        side_i = None
    # MEXC side 1 (open long) / 2 (close short) both execute as a buy;
    # 3 (open short) / 4 (close long) both execute as a sell — mirrors
    # Hyperliquid's literal B/S trade-side semantics, not open/close intent.
    side = "sell" if side_i in (3, 4) else "buy"

    closed_pnl = _first_float(row, ("profit", "closedPnl", "realizedPnl"))

    return {
        "symbol": str(row.get("symbol") or ""),
        "px": px,
        "sz": sz,
        "side": side,
        # _to_ms ist idempotent fuer ms-Werte, konvertiert Sekunden->ms. MEXCs
        # Deal-Zeitformat ist nicht live-verifiziert; der Marker-Layer erwartet
        # ms -> defensiv durch _to_ms, damit ein Sekunden-Payload die Marker
        # nicht still auf 1970 setzt.
        "time": _to_ms(t),
        "dir": _MEXC_FILL_DIR.get(side_i or -1, ""),
        "closed_pnl": closed_pnl,
        "oid": row.get("orderId"),
        "fee": _first_float(row, ("fee",)) or 0.0,
    }


def _f(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def usdt_balances(assets: list[dict[str, Any]]) -> tuple[float, float]:
    """Return (equity_usdt, available_usdt) from MEXC assets list."""
    usdt: dict[str, Any] | None = None
    for row in assets:
        if str(row.get("currency") or "").upper() == "USDT":
            usdt = row
            break
    if usdt is None:
        return 0.0, 0.0
    return _f(usdt.get("equity")), _f(usdt.get("availableBalance"))


def map_position(row: dict[str, Any], contract_size: float = 1.0) -> dict[str, Any]:
    """Normalize one MEXC open-position row for the account API.

    `contract_size` is this position's OWN per-symbol contract size (from
    MEXC contract metadata), NOT the active chart symbol's — the frontend
    used to apply the active symbol's contractSize to every open position,
    which is wrong whenever positions span symbols with different contract
    sizes (F-10). Callers that don't have per-symbol metadata handy may omit
    it; it then safely defaults to 1.0 (correct for e.g. Hyperliquid, which
    is coin-denominated).
    """
    pt = row.get("positionType")
    if pt == 1:
        side = "long"
    elif pt == 2:
        side = "short"
    else:
        side = str(pt) if pt is not None else None

    ot = row.get("openType")
    if ot == 1:
        open_type = "isolated"
    elif ot == 2:
        open_type = "cross"
    else:
        open_type = ot

    entry = row.get("holdAvgPrice")
    if entry is None:
        entry = row.get("openAvgPrice")

    return {
        "position_id": row.get("positionId"),
        "symbol": row.get("symbol"),
        "side": side,
        "position_type": pt,
        "hold_vol": _f(row.get("holdVol")),
        "entry_price": _f(entry),
        "leverage": row.get("leverage"),
        "open_type": open_type,
        "unrealized_pnl": _f(row.get("unRealizedPnl")),
        "realised": _f(row.get("realised")),
        "liquidate_price": _opt_float(row.get("liquidatePrice")),
        "im": _opt_float(row.get("im")),
        "margin_ratio": _opt_float(row.get("marginRatio")),
        "state": row.get("state"),
        "contract_size": float(contract_size) if contract_size else 1.0,
    }


def map_account_snapshot(
    assets: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    *,
    contract_sizes: dict[str, float] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Map raw assets/positions to the /api/account shape.

    `contract_sizes` is an optional {symbol: contractSize} lookup so each
    mapped position gets its OWN contract size rather than one size applied
    to every position (F-10). Omitted -> every position defaults to 1.0
    (correct for Hyperliquid; MEXC callers should pass real per-symbol
    sizes from contract metadata).
    """
    equity, available = usdt_balances(assets)
    sizes = contract_sizes or {}
    return {
        "equity_usdt": equity,
        "available_usdt": available,
        "positions": [
            map_position(p, sizes.get(str(p.get("symbol") or ""), 1.0))
            for p in positions
        ],
        "error": error,
    }


def empty_account(*, error: str | None = None) -> dict[str, Any]:
    return {
        "equity_usdt": 0.0,
        "available_usdt": 0.0,
        "positions": [],
        "error": error,
    }
