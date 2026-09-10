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
from decimal import Decimal, InvalidOperation, ROUND_DOWN
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

# Allow ordinary host/exchange clock skew without exposing far-future market rows.
_MIN_MARKET_TIMESTAMP_S = 1_000_000_000
_MAX_MARKET_FUTURE_SKEW_MS = 5 * 60 * 1000

# Account cards need contract sizes for correct position display, but these
# public contract definitions do not need to be downloaded on every 30-second
# account poll. Money-path contract checks intentionally do not use this cache.
_ACCOUNT_CONTRACT_SIZE_TTL_S = 600.0


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
    if isinstance(v, bool):
        raise MexcError(f"Invalid price value: {v!r}")
    try:
        parsed = float(v)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MexcError(f"Invalid price value: {v!r}") from exc
    if not math.isfinite(parsed):
        raise MexcError(f"Non-finite price value: {v!r}")
    try:
        d = Decimal(str(v))
    except InvalidOperation as exc:
        raise MexcError(f"Invalid price value: {v!r}") from exc
    was_positive = d > 0
    decimals = scale if scale is not None else _DEFAULT_PRICE_DECIMALS
    if decimals < 0:
        decimals = 0
    quant = Decimal(1).scaleb(-decimals)
    try:
        d = d.quantize(quant, rounding=ROUND_DOWN)
    except InvalidOperation as exc:
        raise MexcError(f"Price value cannot be quantized: {v!r}") from exc
    # F-3 safety leine: a positive price/SL smaller than the quantization step
    # ROUND_DOWNs to 0 here. Shipping "0" for a stopLossPrice while the service
    # still believes body_had_sl=True is a SILENT stop-loss loss ("protected"
    # report, no real stop). Reject hard instead — independent of whether a
    # contract priceScale was threaded in (a legitimate 0, e.g. a market-order
    # price field, is never "positive" so it passes untouched).
    if was_positive and d <= 0:
        raise MexcError(
            f"Price {v!r} collapses to 0 at quantization scale {decimals}; "
            "rejecting instead of silently losing the SL/price on the wire"
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
        if key not in out:
            continue
        if out[key] is None:
            if key != "price":
                raise MexcError(f"{key} must be > 0")
            continue
        formatted = _fmt_price(out[key])
        price = Decimal(formatted)
        if key == "price":
            if price < 0:
                raise MexcError(f"{key} must be >= 0")
        elif price <= 0:
            raise MexcError(f"{key} must be > 0")
        out[key] = formatted
    return out


def _to_ms(ts: int | float) -> int:
    """MEXC kline time is seconds; chart libs expect ms."""
    t = int(ts)
    return t if t >= 1_000_000_000_000 else t * 1000


def normalize_klines(data: dict[str, Any]) -> list[Candle]:
    """Convert MEXC parallel-array kline payload to list[Candle]."""
    names = ("time", "open", "high", "low", "close", "vol", "amount")
    arrays = {name: data.get(name) for name in names}
    if not all(isinstance(values, list) for values in arrays.values()):
        raise MexcError("kline payload requires list arrays for all fields", raw=data)
    times = arrays["time"]
    opens = arrays["open"]
    highs = arrays["high"]
    lows = arrays["low"]
    closes = arrays["close"]
    vols = arrays["vol"]
    amounts = arrays["amount"]
    n = len(times)
    if any(len(arrays[name]) != n for name in names[1:]):
        raise MexcError("kline payload arrays have different lengths", raw=data)
    candles: list[Candle] = []
    latest_candle_time = int(time.time() * 1000) + _MAX_MARKET_FUTURE_SKEW_MS
    for i in range(n):
        timestamp = _required_finite_float(times[i], "kline time")
        open_px = _required_finite_float(opens[i], "kline open")
        high_px = _required_finite_float(highs[i], "kline high")
        low_px = _required_finite_float(lows[i], "kline low")
        close_px = _required_finite_float(closes[i], "kline close")
        volume = _required_finite_float(vols[i], "kline vol")
        amount = _required_finite_float(amounts[i], "kline amount")
        if timestamp < _MIN_MARKET_TIMESTAMP_S or not timestamp.is_integer():
            raise MexcError("kline time must be a plausible positive integer")
        timestamp_ms = _to_ms(timestamp)
        if timestamp_ms > latest_candle_time:
            raise MexcError("kline time is implausibly far in the future")
        if min(open_px, high_px, low_px, close_px) <= 0:
            raise MexcError("kline prices must be > 0")
        if high_px < max(open_px, close_px) or low_px > min(open_px, close_px):
            raise MexcError("kline OHLC geometry is invalid")
        if volume < 0 or amount < 0:
            raise MexcError("kline volume and amount must be >= 0")
        candles.append(
            Candle(
                time=timestamp_ms,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                vol=volume,
                amount=amount,
            )
        )
    candles.sort(key=lambda candle: candle.time)
    if any(a.time == b.time for a, b in zip(candles, candles[1:])):
        raise MexcError("duplicate kline timestamp")
    return candles


def parse_contract_meta(row: dict[str, Any]) -> ContractMeta:
    max_leverage = _required_int(row.get("maxLeverage"), "maxLeverage")
    if "countryConfigContractMaxLeverage" in row:
        country_max = _required_int(
            row.get("countryConfigContractMaxLeverage"),
            "countryConfigContractMaxLeverage",
        )
        if country_max < 0:
            raise MexcError("countryConfigContractMaxLeverage must be >= 0")
        if country_max > 0:
            max_leverage = min(max_leverage, country_max)
    meta = ContractMeta(
        symbol=str(row.get("symbol", "")),
        contract_size=_required_finite_float(
            row.get("contractSize") or 0, "contractSize"
        ),
        price_unit=_required_finite_float(row.get("priceUnit") or 0, "priceUnit"),
        vol_unit=_required_finite_float(row.get("volUnit") or 0, "volUnit"),
        min_vol=_required_finite_float(row.get("minVol") or 0, "minVol"),
        max_vol=_required_finite_float(row.get("maxVol") or 0, "maxVol"),
        max_leverage=max_leverage,
        min_leverage=_required_int(row.get("minLeverage"), "minLeverage"),
        api_allowed=row.get("apiAllowed") is True,
        price_scale=row.get("priceScale"),
        vol_scale=row.get("volScale"),
        base_coin=row.get("baseCoin"),
        quote_coin=row.get("quoteCoin"),
        state=_required_int(row.get("state"), "state"),
    )
    positive = ("contract_size", "price_unit", "vol_unit", "min_vol", "max_vol")
    for field_name in positive:
        if getattr(meta, field_name) <= 0:
            raise MexcError(f"{field_name} must be > 0")
    if meta.min_vol > meta.max_vol:
        raise MexcError("minVol exceeds maxVol")
    if (
        meta.min_leverage < 1
        or meta.max_leverage < 1
        or meta.min_leverage > meta.max_leverage
    ):
        raise MexcError("invalid leverage bounds")
    return meta


class MexcClient:
    # Exchange identity — used by the order service to gate MEXC-specific SL
    # verification (O-01/O-02: position-bound SL, empty plan list ≠ missing SL).
    exchange_id = "mexc"

    def __init__(self, base_url: str, api_key: str = "", api_secret: str = ""):
        self.base_url = base_url.rstrip("/")
        # Match setup and readiness semantics: surrounding whitespace is never
        # part of a MEXC credential and must not make an empty key look usable.
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self._account_contract_sizes_cache: tuple[float, dict[str, float]] | None = None
        self._account_contract_sizes_lock = asyncio.Lock()

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
        request_path = path
        content: str | None = None
        param_string = ""

        method_u = method.upper()
        if method_u in ("GET", "DELETE"):
            param_string = sorted_query(params)
        elif json_body is not None:
            body_to_send: Any = json_body
            if isinstance(json_body, dict):
                body_to_send = _format_price_fields(json_body)
            try:
                content = json.dumps(
                    body_to_send, separators=(",", ":"), allow_nan=False
                )
            except (TypeError, ValueError) as exc:
                raise MexcError("request body is not valid finite JSON") from exc
            param_string = content

        if private:
            if not self.api_key or not self.api_secret:
                raise MexcError("MEXC API keys not configured")
            if method_u in ("GET", "DELETE"):
                # Send the exact canonical bytes that are signed. Letting httpx
                # rebuild a dict would restore insertion order, include None as
                # an empty value and encode spaces as '+', invalidating the HMAC.
                req_params = None
                if param_string:
                    request_path = f"{path}?{param_string}"
            req_time = str(int(time.time() * 1000))
            headers.update(
                {
                    "ApiKey": self.api_key,
                    "Request-Time": req_time,
                    "Signature": sign_payload(
                        self.api_key, self.api_secret, req_time, param_string
                    ),
                }
            )

        try:
            r = await self._client.request(
                method_u,
                request_path,
                params=req_params,
                content=content,
                headers=headers,
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
        if isinstance(data, dict) and "success" in data:
            success = data.get("success")
            if success is False:
                raise MexcError(
                    str(data.get("message") or data.get("code") or "MEXC error"),
                    raw=data,
                )
            if success is not True:
                raise MexcError("invalid MEXC success marker", raw=data)
        # The official common response uses code=0 for success; some endpoints
        # use an errorCode variant. Validate every present marker independently
        # of success so a contradictory envelope never authorizes a mutation.
        # Absence stays compatible; a present value must be integer/string zero.
        # A JSON float such as 0.0 is not the documented status-code type and
        # must not compare equal to integer zero by Python coercion.
        if isinstance(data, dict):
            for marker_name in ("code", "errorCode", "error_code"):
                if marker_name not in data:
                    continue
                code = data.get(marker_name)
                if _mexc_zero_code(code):
                    continue
                raise MexcError(
                    str(
                        data.get("message")
                        or data.get("msg")
                        or f"MEXC error {marker_name}={code}"
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
        server_time = _required_int(data, "server time")
        if server_time <= 0:
            raise MexcError("server time must be > 0")
        return server_time

    async def contract_detail(
        self, symbol: str | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        data = await self._request("GET", "/api/v1/contract/detail", params=params)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and all(isinstance(row, dict) for row in data):
            return data
        raise MexcError("unrecognized contract-detail response shape", raw=data)

    async def _account_contract_sizes(
        self, required: set[str], *, fresh: bool = False
    ) -> dict[str, float]:
        """Return display-only contract sizes with TTL and cold-miss singleflight."""

        def cached() -> dict[str, float] | None:
            hit = self._account_contract_sizes_cache
            if hit is None:
                return None
            created_at, sizes = hit
            if time.monotonic() - created_at >= _ACCOUNT_CONTRACT_SIZE_TTL_S:
                return None
            if not required.issubset(sizes):
                return None
            return dict(sizes)

        if not fresh and (hit := cached()) is not None:
            return hit

        async with self._account_contract_sizes_lock:
            if not fresh and (hit := cached()) is not None:
                return hit

            detail = await self.contract_detail()
            rows = detail if isinstance(detail, list) else [detail]
            sizes: dict[str, float] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "")
                size = _opt_float(row.get("contractSize"))
                if symbol and size is not None and size > 0:
                    sizes[symbol] = size

            missing = required - set(sizes)
            if missing:
                raise MexcError(
                    "contract size unavailable for open position(s): "
                    + ", ".join(sorted(missing))
                )
            self._account_contract_sizes_cache = (time.monotonic(), sizes)
            return dict(sizes)

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
        if isinstance(data, dict):
            source_rows = [data]
        elif isinstance(data, list) and all(isinstance(row, dict) for row in data):
            source_rows = data
        else:
            raise MexcError("unrecognized market-overview response shape", raw=data)
        rows: list[dict[str, Any]] = []
        for r in source_rows:
            sym = str(r.get("symbol") or "")
            if not sym.endswith("_USDT"):
                continue
            # Task 24 universe fields. riseFallRate is MEXC's 24h price-change
            # FRACTION (e.g. 0.05 = +5%) -> ×100 for a percent; holdVol is the
            # OI LEVEL in contracts (MEXC exposes no OI-Δ, so this alone never
            # triggers the scanner oi_read and classic stays byte-for-byte).
            rfr = _opt_float(r.get("riseFallRate"))
            volume24 = _opt_float(r.get("amount24"))
            last = _opt_float(r.get("lastPrice"))
            open_interest = _opt_float(r.get("holdVol"))
            price_change_pct = rfr * 100.0 if rfr is not None else None
            if price_change_pct is not None and not math.isfinite(price_change_pct):
                price_change_pct = None
            rows.append(
                {
                    "symbol": sym,
                    "volume24": volume24 if volume24 is not None and volume24 >= 0 else 0.0,
                    "funding": _opt_float(r.get("fundingRate")),
                    "last": last if last is not None and last > 0 else None,
                    "price_change_pct": price_change_pct,
                    "open_interest": (
                        open_interest
                        if open_interest is not None and open_interest >= 0
                        else None
                    ),
                }
            )
        rows.sort(key=lambda x: x["volume24"], reverse=True)
        return rows[: max(1, int(limit))]

    async def contract_meta(self, symbol: str) -> ContractMeta:
        data = await self.contract_detail(symbol)
        if isinstance(data, list):
            row = next(
                (item for item in data if str(item.get("symbol") or "") == symbol),
                None,
            )
            if row is None:
                raise MexcError(f"No contract detail for {symbol}")
        else:
            row = data
        if str(row.get("symbol") or "") != symbol:
            raise MexcError(f"Contract symbol mismatch: wanted {symbol}")
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
        if sec and any(
            first.time // (sec * 1000) == second.time // (sec * 1000)
            for first, second in zip(candles, candles[1:])
        ):
            raise MexcError("duplicate kline interval")
        if limit_hint > 0 and len(candles) > limit_hint:
            candles = candles[-limit_hint:]
        return candles

    async def ticker(self, symbol: str) -> Ticker:
        data = await self._request(
            "GET", "/api/v1/contract/ticker", params={"symbol": symbol}
        )
        if isinstance(data, list):
            if not all(isinstance(item, dict) for item in data):
                raise MexcError("ticker response contains a non-object row", raw=data)
            row = next((x for x in data if x.get("symbol") == symbol), None)
            if row is None:
                raise MexcError(
                    f"Ticker for {symbol} not found (refusing other-symbol fallback)"
                )
        elif isinstance(data, dict):
            row = data
            row_symbol = row.get("symbol")
            if row_symbol is not None and not isinstance(row_symbol, str):
                raise MexcError(
                    f"Ticker symbol is invalid: wanted {symbol}, got {row_symbol}"
                )
            if row_symbol and row_symbol != symbol:
                raise MexcError(
                    f"Ticker symbol mismatch: wanted {symbol}, got {row_symbol}"
                )
        else:
            raise MexcError("unrecognized ticker response shape", raw=data)
        last_price = _required_finite_float(
            row.get("lastPrice") or 0, "ticker lastPrice"
        )
        if last_price <= 0:
            raise MexcError("ticker lastPrice must be > 0")
        return Ticker(
            symbol=str(row.get("symbol") or symbol),
            last_price=last_price,
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
        if not isinstance(data, dict):
            raise MexcError("unrecognized funding-rate response shape", raw=data)
        row = data
        row_symbol = row.get("symbol")
        if row_symbol is not None and not isinstance(row_symbol, str):
            raise MexcError(
                f"Funding symbol is invalid: wanted {symbol}, got {row_symbol}"
            )
        if row_symbol and row_symbol != symbol:
            raise MexcError(
                f"Funding symbol mismatch: wanted {symbol}, got {row_symbol}"
            )
        funding_rate = _required_finite_float(row.get("fundingRate"), "fundingRate")
        return FundingRate(
            symbol=str(row.get("symbol") or symbol),
            funding_rate=funding_rate,
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
        if not isinstance(data, list):
            raise MexcError("unrecognized account-assets response shape", raw=data)
        if not all(isinstance(row, dict) for row in data):
            raise MexcError("account-assets response contains a non-object row")
        return data

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
        if not isinstance(data, list):
            raise MexcError("unrecognized open-positions response shape", raw=data)
        rows = data
        if symbol:
            wanted_symbol = str(symbol).strip().upper()
            scoped_rows = []
            for row in rows:
                if not isinstance(row, dict):
                    scoped_rows.append(row)
                    continue
                symbol_raw = row.get("symbol")
                row_symbol = (
                    symbol_raw.strip().upper()
                    if isinstance(symbol_raw, str)
                    else ""
                )
                if not row_symbol or row_symbol == wanted_symbol:
                    scoped_rows.append(row)
            rows = scoped_rows
        for row in rows:
            if not isinstance(row, dict):
                raise MexcError("open positions contains a non-object row")
            mapped = map_position(row)
            hold = mapped.get("hold_vol")
            entry = mapped.get("entry_price")
            position_symbol = mapped.get("symbol")
            if (
                not isinstance(position_symbol, str)
                or not position_symbol.strip()
                or mapped.get("side") not in ("long", "short")
            ):
                raise MexcError("open position identity is invalid")
            if mapped.get("open_type") not in ("isolated", "cross"):
                raise MexcError("open position openType is invalid")
            if hold is None or hold <= 0:
                raise MexcError("open position holdVol is invalid")
            if entry is None or entry <= 0:
                raise MexcError("open position entry price is invalid")
        return rows

    async def account_state(
        self, symbol: str | None = None, *, fresh: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Combined (assets, positions) read for the money path (Finding 2).

        MEXC exposes assets and positions as SEPARATE endpoints (no shared
        snapshot like HL), so this fires the two live reads CONCURRENTLY rather
        than sequentially and returns both. `symbol` scopes the positions read
        exactly like positions(symbol). Either error fails closed immediately:
        the still-running sibling GET is cancelled and settled. If both already
        failed, the assets error keeps deterministic priority.
        """
        assets_task = asyncio.create_task(self.assets(fresh=fresh))
        positions_task = asyncio.create_task(self.positions(symbol, fresh=fresh))
        tasks = (assets_task, positions_task)
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            if any(task.cancelled() or task.exception() is not None for task in done):
                for task in pending:
                    task.cancel()
            assets_raw, positions_raw = await asyncio.gather(
                *tasks, return_exceptions=True
            )
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        # Peer cancellation is not the root cause. Prefer actual Exceptions in
        # argument order, then propagate a standalone cancellation/BaseException.
        if isinstance(assets_raw, Exception):
            raise assets_raw
        if isinstance(positions_raw, Exception):
            raise positions_raw
        if isinstance(assets_raw, BaseException):
            raise assets_raw
        if isinstance(positions_raw, BaseException):
            raise positions_raw
        assets_list = list(assets_raw)
        equity, available = usdt_balances(assets_list)
        if not math.isfinite(equity) or not math.isfinite(available):
            raise MexcError(
                "account state contains non-finite equity/available balance"
            )
        return assets_list, list(positions_raw)

    async def account_snapshot(self, *, fresh: bool = False) -> dict[str, Any]:
        """Fetch assets + open positions and map to API account shape.

        Each position gets its OWN contract_size (F-10) — resolved from
        MEXC's contract metadata (one /contract/detail call covering all
        symbols) rather than the active chart symbol's. Missing metadata is
        unknown, not 1.0: fail so callers can retain the last good snapshot.
        """
        assets_raw, positions_raw = await self.account_state(fresh=fresh)
        contract_sizes: dict[str, float] = {}
        if positions_raw:
            required = {str(position.get("symbol") or "") for position in positions_raw}
            contract_sizes = await self._account_contract_sizes(required, fresh=fresh)
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
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage <= 0:
            raise MexcError("leverage must be a positive integer")
        if (
            isinstance(open_type, bool)
            or not isinstance(open_type, int)
            or open_type not in (1, 2)
        ):
            raise MexcError("openType must be 1 (isolated) or 2 (cross)")
        if position_id is not None:
            if not _mexc_positive_order_id(position_id):
                raise MexcError("positionId must be a positive numeric ID")
        elif (
            isinstance(position_type, bool)
            or not isinstance(position_type, int)
            or position_type not in (1, 2)
        ):
            raise MexcError("positionType must be 1 (long) or 2 (short)")
        if position_id is None and (
            not isinstance(symbol, str) or not symbol.strip()
        ):
            raise MexcError("symbol is required without positionId")

        body: dict[str, Any] = {"leverage": leverage}
        if position_id is not None:
            body["positionId"] = int(position_id)
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
        # The official endpoint documents only the public response parameters
        # (``success: true`` on success). A 2xx body whose data was null, a
        # scalar, a list, or an arbitrary object is therefore not evidence that
        # leverage actually changed. Confirm must fail closed here, before the
        # entry order is sent.
        if not isinstance(data, dict) or data.get("success") is not True:
            raise MexcError("uncertain set-leverage response shape", raw=data)
        for key in ("code", "errorCode", "error_code"):
            if key not in data:
                continue
            marker = data.get(key)
            if not _mexc_zero_code(marker):
                raise MexcError(
                    f"uncertain set-leverage response: {key}={marker!r}",
                    raw=data,
                )
        return data

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/private/order/create (current official path).

        Typical fields: symbol, price, vol, side, type, openType, leverage,
        optional externalOid, stopLossPrice, takeProfitPrice.
        side: 1 open long, 2 close short, 3 open short, 4 close long
        type: 1 limit, 5 market (see MEXC docs)
        openType: 1 isolated, 2 cross
        """
        symbol = body.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise MexcError("order symbol must be a non-empty string")
        volume_raw = body.get("vol")
        if isinstance(volume_raw, bool) or not isinstance(volume_raw, (int, float)):
            raise MexcError("order volume must be a positive finite number")
        try:
            volume = float(volume_raw)
        except OverflowError as exc:
            raise MexcError("order volume must be a positive finite number") from exc
        if not math.isfinite(volume) or volume <= 0:
            raise MexcError("order volume must be a positive finite number")

        side = body.get("side")
        if isinstance(side, bool) or not isinstance(side, int) or side not in (1, 2, 3, 4):
            raise MexcError("side must be an integer from 1 to 4")
        order_type = body.get("type")
        if (
            isinstance(order_type, bool)
            or not isinstance(order_type, int)
            or order_type not in (1, 5)
        ):
            raise MexcError("type must be 1 (limit) or 5 (market)")
        if order_type == 1:
            price_raw = body.get("price")
            if isinstance(price_raw, bool) or not isinstance(price_raw, (int, float)):
                raise MexcError("limit order price must be a positive finite number")
            try:
                price = float(price_raw)
            except OverflowError as exc:
                raise MexcError(
                    "limit order price must be a positive finite number"
                ) from exc
            if not math.isfinite(price) or price <= 0:
                raise MexcError("limit order price must be a positive finite number")
        open_type = body.get("openType")
        if (
            isinstance(open_type, bool)
            or not isinstance(open_type, int)
            or open_type not in (1, 2)
        ):
            raise MexcError("openType must be 1 (isolated) or 2 (cross)")
        leverage = body.get("leverage")
        if side in (1, 3) or leverage is not None:
            if (
                isinstance(leverage, bool)
                or not isinstance(leverage, int)
                or leverage <= 0
            ):
                raise MexcError("leverage must be a positive integer for open orders")
        data = await self._request(
            "POST",
            "/api/v1/private/order/create",
            json_body=body,
            private=True,
        )

        if isinstance(data, dict):
            if "success" in data and data.get("success") is not True:
                raise MexcError(
                    "uncertain order-create response: nested success is not true",
                    raw=data,
                )
            for key in ("code", "errorCode", "error_code"):
                if key not in data:
                    continue
                marker = data.get(key)
                if not _mexc_zero_code(marker):
                    raise MexcError(
                        f"uncertain order-create response: nested {key}={marker!r}",
                        raw=data,
                    )
            if any(key in data for key in ("orderId", "order_id", "oid")):
                if _mexc_consistent_order_id(data) is None:
                    raise MexcError(
                        "uncertain order-create response: invalid or conflicting "
                        "order IDs",
                        raw=data,
                    )
                return data
        elif _mexc_positive_order_id(data):
            # Normalize MEXC's documented scalar create response to the same
            # shape consumed by cancellation and recovery code.
            return {"orderId": data}
        raise MexcError("uncertain order-create response shape", raw=data)

    async def cancel_order(
        self, body: dict[str, Any] | list[Any]
    ) -> dict[str, Any] | list[Any]:
        """POST /api/v1/private/order/cancel

        Official body is often a list of order ids (max 50).
        Also accepts a dict wrapper if callers prefer that shape.
        """
        items = body if isinstance(body, list) else [body]
        if not items:
            raise MexcError("cancel request must not be empty")
        requested: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                id_keys = [key for key in ("orderId", "oid") if key in item]
                if len(id_keys) != 1:
                    raise MexcError(
                        "cancel requires exactly one orderId or oid per order"
                    )
                value = item.get(id_keys[0])
            else:
                value = item
            if not _mexc_positive_order_id(value):
                raise MexcError("cancel requires positive numeric order IDs")
            order_id = str(int(value))
            if order_id in requested:
                raise MexcError("cancel request contains duplicate order IDs")
            requested.add(order_id)

        data = await self._request(
            "POST",
            "/api/v1/private/order/cancel",
            json_body=body,
            private=True,
        )
        if (
            not isinstance(data, list)
            or not data
            or not all(isinstance(row, dict) for row in data)
        ):
            raise MexcError("uncertain cancel response shape", raw=data)
        if any("orderId" not in row or "errorCode" not in row for row in data):
            raise MexcError("cancel response row lacks orderId/errorCode", raw=data)
        for row in data:
            if "success" in row and row.get("success") is not True:
                raise MexcError(
                    "cancel rejected: nested success is not true",
                    raw=data,
                )
            for marker_name in ("code", "errorCode", "error_code"):
                if marker_name not in row:
                    continue
                marker = row.get(marker_name)
                if _mexc_zero_code(marker):
                    continue
                detail = row.get("errorMsg") or row.get("message") or "unknown"
                raise MexcError(
                    f"cancel rejected: {marker_name}={marker} {detail}",
                    raw=data,
                )

        returned: set[str] = set()
        for row in data:
            returned_id = _mexc_consistent_order_id(row)
            if returned_id is None:
                raise MexcError(
                    "cancel response does not exactly identify requested order(s)",
                    raw=data,
                )
            returned.add(returned_id)
        if returned != requested or len(data) != len(requested):
            raise MexcError(
                "cancel response does not exactly identify requested order(s)", raw=data
            )
        return data

    _OPEN_ORDERS_PAGE_SIZE = 100
    _OPEN_ORDERS_MAX_PAGES = 5

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET all open orders within the bounded MEXC paging window."""
        rows: list[dict[str, Any]] = []
        for page_num in range(1, self._OPEN_ORDERS_MAX_PAGES + 1):
            params: dict[str, Any] = {
                "page_num": page_num,
                "page_size": self._OPEN_ORDERS_PAGE_SIZE,
            }
            if symbol:
                params["symbol"] = symbol
            data = await self._request(
                "GET",
                "/api/v1/private/order/list/open_orders",
                params=params,
                private=True,
            )
            if isinstance(data, dict) and isinstance(data.get("resultList"), list):
                page_rows = data["resultList"]
            elif isinstance(data, list):
                page_rows = data
            else:
                raise MexcError("unrecognized open-orders response shape", raw=data)
            if not all(isinstance(row, dict) for row in page_rows):
                raise MexcError(
                    "open-orders response contains a non-object row", raw=data
                )
            rows.extend(page_rows)
            if len(page_rows) < self._OPEN_ORDERS_PAGE_SIZE:
                if not symbol:
                    return rows
                wanted_symbol = str(symbol).strip().upper()
                scoped_rows = []
                for row in rows:
                    symbol_raw = row.get("symbol")
                    if symbol_raw is not None and not isinstance(symbol_raw, str):
                        raise MexcError(
                            "open-orders response contains an invalid symbol identity",
                            raw=row,
                        )
                    row_symbol = (symbol_raw or "").strip().upper()
                    if not row_symbol or row_symbol == wanted_symbol:
                        scoped_rows.append(row)
                return scoped_rows
        raise MexcError("open-orders pagination exceeded safe page cap", raw=rows)

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
        requested_limit = max(int(limit), 1)
        page_size = min(requested_limit, self._FILLS_PAGE_SIZE)
        raw_rows: list[Any] = []
        out: list[dict[str, Any]] = []
        wanted_symbol = str(symbol or "").strip().upper()
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
            if isinstance(data, dict) and isinstance(data.get("resultList"), list):
                page_rows = data["resultList"]
            elif isinstance(data, list):
                page_rows = data
            else:
                raise MexcError("unrecognized user-fills response shape", raw=data)
            raw_rows.extend(page_rows)
            for row in page_rows:
                if not isinstance(row, dict):
                    continue
                try:
                    normalized = normalize_mexc_fill(row)
                except (TypeError, ValueError):
                    continue  # graceful degrade: skip, never fabricate a fill
                if wanted_symbol and normalized["symbol"] != wanted_symbol:
                    continue
                out.append(normalized)
            if len(out) >= requested_limit or len(page_rows) < page_size:
                break
        else:
            raise MexcError(
                "user-fills pagination exceeded safe page cap", raw=raw_rows
            )
        out.sort(key=lambda r: r["time"], reverse=True)
        return out[:requested_limit]

    # Current official open TP/SL endpoint first. The only fallback is the
    # official history endpoint constrained to unfinished rows; unfiltered
    # history must never masquerade as live protection.
    _STOP_ORDER_PATHS = (
        "/api/v1/private/stoporder/open_orders",
        "/api/v1/private/stoporder/list/orders",
    )
    _STOP_ORDERS_PAGE_SIZE = 100
    _STOP_ORDERS_MAX_PAGES = 5

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET open stop / plan orders.

        Use the current official unpaged endpoint. If it is unavailable, the
        official history endpoint is queried with ``is_finished=0`` and bounded
        pagination. Return only a RECOGNIZED order-list schema (a bare list, or
        a dict containing "resultList" — an empty list/resultList is still a
        genuine "no stop orders" answer).

        A stale/deprecated endpoint that responds 2xx with an unrecognized
        shape (e.g. `{}` or `null` instead of erroring) is NOT proof of "no
        stop orders" — it is treated the same as a failing candidate, so if
        every candidate is either an error or unrecognized, the last error is
        propagated (never a silent []) and the SL-verify path treats the check
        as UNKNOWN, never MISSING.
        """
        last_err: MexcError | None = None
        wanted_symbol = str(symbol or "").strip().upper()

        def _active(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            active = []
            for row in rows:
                state = _required_int(row.get("state"), "stop-order state")
                if state not in (1, 2, 3, 4, 5):
                    raise MexcError("stop-order state is outside the documented range")
                if state == 1:
                    active.append(row)
            return active

        def _scoped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not wanted_symbol:
                return rows
            scoped = []
            for row in rows:
                symbol_raw = row.get("symbol")
                if symbol_raw is not None and not isinstance(symbol_raw, str):
                    raise MexcError(
                        "stop-order response contains an invalid symbol identity",
                        raw=row,
                    )
                row_symbol = (symbol_raw or "").strip().upper()
                if not row_symbol or row_symbol == wanted_symbol:
                    scoped.append(row)
            return scoped

        ordered_paths = list(self._STOP_ORDER_PATHS)

        for path in ordered_paths:
            is_current_open_endpoint = path == self._STOP_ORDER_PATHS[0]
            params: dict[str, Any] = (
                {}
                if is_current_open_endpoint
                else {
                    "is_finished": 0,
                    "page_num": 1,
                    "page_size": self._STOP_ORDERS_PAGE_SIZE,
                }
            )
            if symbol:
                params["symbol"] = symbol
            try:
                data = await self._request("GET", path, params=params, private=True)
            except MexcError as e:
                last_err = e
                continue
            if isinstance(data, dict) and isinstance(data.get("resultList"), list):
                first_page = data["resultList"]
            elif isinstance(data, list):
                first_page = data
            else:
                last_err = MexcError(
                    f"unrecognized stop-order response shape from {path}", raw=data
                )
                continue
            if not all(isinstance(row, dict) for row in first_page):
                last_err = MexcError(
                    f"unrecognized stop-order response shape from {path}", raw=data
                )
                continue

            # A recognized first page selects the authoritative candidate for
            # this read. Any later failure must propagate: falling back to an
            # empty response from a different endpoint would turn UNKNOWN into
            # a false "no stop orders" result.
            rows = list(first_page)
            if is_current_open_endpoint or len(first_page) < self._STOP_ORDERS_PAGE_SIZE:
                return _scoped(_active(rows))
            for page_num in range(2, self._STOP_ORDERS_MAX_PAGES + 1):
                params["page_num"] = page_num
                data = await self._request("GET", path, params=params, private=True)
                if isinstance(data, dict) and isinstance(
                    data.get("resultList"), list
                ):
                    page_rows = data["resultList"]
                elif isinstance(data, list):
                    page_rows = data
                else:
                    raise MexcError(
                        f"unrecognized stop-order response shape from {path}", raw=data
                    )
                if not all(isinstance(row, dict) for row in page_rows):
                    raise MexcError(
                        f"unrecognized stop-order response shape from {path}", raw=data
                    )
                rows.extend(page_rows)
                if len(page_rows) < self._STOP_ORDERS_PAGE_SIZE:
                    return _scoped(_active(rows))
            raise MexcError(
                f"stop-order pagination exceeded safe page cap for {path}", raw=rows
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
        if not isinstance(symbol, str) or not symbol.strip():
            raise MexcError("close symbol is required")
        if (
            isinstance(open_type, bool)
            or not isinstance(open_type, int)
            or open_type not in (1, 2)
        ):
            raise MexcError("openType must be 1 (isolated) or 2 (cross)")
        close_side = str(side or "").strip().lower()
        if close_side not in ("long", "short"):
            raise MexcError(f"close side must be long/short, got {side!r}")
        close_vol = _required_finite_float(vol, "close volume")
        if close_vol <= 0:
            raise MexcError("close volume must be > 0")
        mexc_side = 4 if close_side == "long" else 2
        body = {
            "symbol": symbol,
            "price": 0,
            "vol": close_vol,
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
            if isinstance(data, dict) and isinstance(data.get("resultList"), list):
                rows = data["resultList"]
            elif isinstance(data, list):
                rows = data
            else:
                break
            for r in rows:
                if _mexc_recovery_identity_matches(r, symbol, external_oid):
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

        X2-04 STATE-FILTER: cancelled(4)/invalid(5) is terminal-dead only when
        `dealVol` explicitly proves zero fill. A positive, missing or malformed
        fill cannot be discarded because a partial position may already exist.

        Every live match is tagged with a marker so a later reconciliation step
        can tell WHERE the order was found:
            {"match": "direct"|"history"|"open", "externalOid": external_oid, "order": <raw>}
        ("direct" = guessed direct-lookup endpoint with an exact oid-field
        match; history/open use the same exact field comparison.)
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
        # Trust only an exact oid field, never a substring in unrelated
        # diagnostic text. The wrapper below injects the oid, so a false match
        # here would otherwise look like a recovered live order to the caller.
        if _mexc_recovery_identity_matches(direct, symbol, external_oid):
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
                if _mexc_recovery_identity_matches(r, symbol, external_oid):
                    if _mexc_state_is_dead(r):
                        return {}  # X2-04
                    return {"match": "open", "externalOid": external_oid, "order": r}

            # Not found in either list yet — wait for the index to catch up,
            # unless this was the last attempt.
            if attempt < attempts - 1 and delay_s > 0:
                await asyncio.sleep(delay_s)

        return {}


# MEXC futures order-state codes: 1=uninformed, 2=uncompleted, 3=completed,
# 4=cancelled, 5=invalid. A terminal marker alone cannot prove zero fill:
# `dealVol` must also be present, finite and zero. Otherwise the order may have
# partially filled before cancellation and must continue through protection.
_MEXC_DEAD_STATES = frozenset({"4", "5", "cancelled", "canceled", "invalid"})


def _mexc_state_is_dead(row: Any) -> bool:
    """True only for an explicitly terminal MEXC order with a proven zero fill."""
    if not isinstance(row, dict):
        return False
    states: list[str] = []
    for key in ("state", "orderState", "order_state"):
        if key not in row:
            continue
        state = str(row.get(key)).strip().lower()
        if state not in _MEXC_DEAD_STATES:
            return False
        states.append(state)
    if not states:
        return False

    fills: list[float] = []
    for key in ("dealVol", "deal_vol"):
        if key not in row:
            continue
        fill = _opt_float(row.get(key))
        if fill is None or fill != 0.0:
            return False
        fills.append(fill)
    return bool(fills)


def _mexc_positive_order_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        if not value.isdigit():
            return False
        try:
            return int(value) > 0
        except ValueError:
            return False
    return False


def _mexc_zero_code(value: Any) -> bool:
    """True only for MEXC's documented integer/string zero status code."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and value == 0
    ) or (isinstance(value, str) and value == "0")


def _mexc_consistent_order_id(row: Any) -> str | None:
    """Canonical positive ID only when every present alias agrees."""
    if not isinstance(row, dict):
        return None
    values: list[str] = []
    for key in ("orderId", "order_id", "oid"):
        if key not in row:
            continue
        value = row.get(key)
        if not _mexc_positive_order_id(value):
            return None
        values.append(str(int(value)))
    if not values or len(set(values)) != 1:
        return None
    return values[0]


def _mexc_external_oid_matches(row: Any, external_oid: str) -> bool:
    """Require every present client-ID alias to match the requested string."""
    if not isinstance(row, dict) or not isinstance(external_oid, str) or not external_oid:
        return False
    values: list[str] = []
    for key in ("externalOid", "external_oid"):
        if key not in row:
            continue
        value = row.get(key)
        if not isinstance(value, str) or not value:
            return False
        values.append(value)
    return bool(values) and all(value == external_oid for value in values)


def _mexc_recovery_identity_matches(
    row: Any, symbol: str, external_oid: str
) -> bool:
    """Require a concrete order ID, exact externalOid and no symbol conflict."""
    if not isinstance(row, dict):
        return False
    if not _mexc_external_oid_matches(row, external_oid):
        return False
    if _mexc_consistent_order_id(row) is None:
        return False
    row_symbol = row.get("symbol")
    return row_symbol is None or str(row_symbol).upper() == str(symbol).upper()


def _opt_float(v: Any) -> float | None:
    """float(v) or None — mirrors Hyperliquid's `_opt_f`: MEXC blanks optional
    numeric fields as "" (not just null/omitted), e.g. fairPrice/liquidatePrice/
    im/marginRatio/funding fields. A bare `float(v)` raises ValueError on ""
    (or on garbage), which is not a MexcError and escapes the ExchangeError
    handlers upstream — degrade to None instead of crashing the poll cycle."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        value = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _required_finite_float(v: Any, field: str) -> float:
    """Parse a required exchange number and keep failures in adapter semantics."""
    if isinstance(v, bool):
        raise MexcError(f"{field} is not numeric")
    try:
        value = float(v)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MexcError(f"{field} is not numeric") from exc
    if not math.isfinite(value):
        raise MexcError(f"{field} is non-finite")
    return value


def _required_int(v: Any, field: str) -> int:
    if v is None or v == "":
        raise MexcError(f"{field} is not an integer")
    raw = v
    if isinstance(raw, bool):
        raise MexcError(f"{field} is not an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise MexcError(f"{field} is not an integer") from exc
    raise MexcError(f"{field} is not an integer")


def _opt_int(v: Any) -> int | None:
    """int(v) or None — same "" / garbage guard as `_opt_float`, for the raw
    int(...) timestamp/cycle fields (timestamp, collectCycle, nextSettleTime)
    that previously only checked `is not None`."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
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
    """First present finite numeric value across candidate field names."""
    for k in keys:
        if k in row and row[k] is not None:
            if isinstance(row[k], bool):
                continue
            try:
                value = float(row[k])
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                return value
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
    symbol_raw = row.get("symbol")
    symbol = symbol_raw.strip() if isinstance(symbol_raw, str) else ""
    if (
        not symbol
        or px is None
        or px <= 0
        or sz is None
        or sz <= 0
        or t is None
        # MEXC may return epoch seconds or milliseconds; smaller values cannot
        # represent a real fill from this exchange and would fabricate 1970 age.
        or t < _MIN_MARKET_TIMESTAMP_S
        or not t.is_integer()
    ):
        raise ValueError("MEXC deal row has invalid symbol/px/sz/time")
    fill_time = _to_ms(t)
    if fill_time > int(time.time() * 1000) + _MAX_MARKET_FUTURE_SKEW_MS:
        raise ValueError("MEXC deal row has invalid symbol/px/sz/time")

    side_i: int | None
    if isinstance(row.get("side"), bool):
        side_i = None
    elif isinstance(row.get("side"), int):
        side_i = row["side"]
    elif isinstance(row.get("side"), str) and row["side"].isdigit():
        side_i = int(row["side"])
    else:
        side_i = None
    # MEXC side 1 (open long) / 2 (close short) both execute as a buy;
    # 3 (open short) / 4 (close long) both execute as a sell — mirrors
    # Hyperliquid's literal B/S trade-side semantics, not open/close intent.
    if side_i not in _MEXC_FILL_DIR:
        raise ValueError("MEXC deal row has invalid side")
    side = "buy" if side_i in (1, 2) else "sell"

    closed_pnl = _first_float(row, ("profit", "closedPnl", "realizedPnl"))
    oid_raw = row.get("orderId")

    return {
        "symbol": symbol,
        "px": px,
        "sz": sz,
        "side": side,
        # _to_ms ist idempotent fuer ms-Werte, konvertiert Sekunden->ms. MEXCs
        # Deal-Zeitformat ist nicht live-verifiziert; der Marker-Layer erwartet
        # ms -> defensiv durch _to_ms, damit ein Sekunden-Payload die Marker
        # nicht still auf 1970 setzt.
        "time": fill_time,
        "dir": _MEXC_FILL_DIR[side_i],
        "closed_pnl": closed_pnl,
        "oid": oid_raw if _mexc_positive_order_id(oid_raw) else None,
        "fee": (
            0.0
            if row.get("fee") in (None, "")
            else _first_float(row, ("fee",))
        ),
    }


def usdt_balances(assets: list[dict[str, Any]]) -> tuple[float, float]:
    """Return (equity_usdt, available_usdt) from MEXC assets list."""
    usdt: dict[str, Any] | None = None
    for row in assets:
        if str(row.get("currency") or "").upper() == "USDT":
            if usdt is not None:
                raise MexcError("multiple USDT account rows are ambiguous")
            usdt = row
    if usdt is None:
        return 0.0, 0.0
    equity_raw = usdt.get("equity")
    available_raw = usdt.get("availableBalance")
    if isinstance(equity_raw, bool) or isinstance(available_raw, bool):
        raise MexcError("account balance contains a boolean numeric value")
    return (
        _required_finite_float(equity_raw, "account equity"),
        _required_finite_float(available_raw, "account available balance"),
    )


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
    if not isinstance(pt, bool) and pt == 1:
        side = "long"
    elif not isinstance(pt, bool) and pt == 2:
        side = "short"
    else:
        side = str(pt) if pt is not None else None

    ot = row.get("openType")
    if not isinstance(ot, bool) and ot == 1:
        open_type = "isolated"
    elif not isinstance(ot, bool) and ot == 2:
        open_type = "cross"
    else:
        open_type = ot

    entry = row.get("holdAvgPrice")
    if entry is None:
        entry = row.get("openAvgPrice")
    leverage = _opt_float(row.get("leverage"))
    if leverage is not None and leverage <= 0:
        leverage = None
    liquidation_price = _opt_float(row.get("liquidatePrice"))
    if liquidation_price is not None and liquidation_price <= 0:
        liquidation_price = None
    initial_margin = _opt_float(row.get("im"))
    if initial_margin is not None and initial_margin <= 0:
        initial_margin = None

    return {
        "position_id": row.get("positionId"),
        "symbol": row.get("symbol"),
        "side": side,
        "position_type": pt,
        "hold_vol": _opt_float(row.get("holdVol")),
        "entry_price": _opt_float(entry),
        "leverage": leverage,
        "open_type": open_type,
        "unrealized_pnl": _opt_float(row.get("unRealizedPnl")),
        "realised": _opt_float(row.get("realised")),
        "liquidate_price": liquidation_price,
        "im": initial_margin,
        "margin_ratio": _opt_float(row.get("marginRatio")),
        "state": row.get("state"),
        "contract_size": (
            value
            if (value := _opt_float(contract_size)) is not None and value > 0
            else None
        ),
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
