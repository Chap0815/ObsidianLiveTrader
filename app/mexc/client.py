"""MEXC USDT-M futures REST client (public + signed private).

Auth: Signature = hex HMAC-SHA256(accessKey + requestTime + paramString)
  - GET/DELETE: paramString = sorted k=v&... (skip nulls)
  - POST: paramString = raw JSON body (separators comma/colon, no spaces)

Order place path (current official docs): POST /api/v1/private/order/create
Older docs still list /submit as under maintenance — code uses /create.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
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
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

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
            content = json.dumps(json_body, separators=(",", ":"))
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
            rows.append(
                {
                    "symbol": sym,
                    "volume24": _f(r.get("amount24")),
                    "funding": _f(r.get("fundingRate")),
                    "last": _f(r.get("lastPrice")),
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
        self, symbol: str, interval: str, limit_hint: int = 200
    ) -> list[Candle]:
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
            timestamp=int(row["timestamp"]) if row.get("timestamp") is not None else None,
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
            collect_cycle=int(row["collectCycle"])
            if row.get("collectCycle") is not None
            else None,
            next_settle_time=int(row["nextSettleTime"])
            if row.get("nextSettleTime") is not None
            else None,
            timestamp=int(row["timestamp"]) if row.get("timestamp") is not None else None,
        )

    # ── private ─────────────────────────────────────────────────────────

    async def assets(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/api/v1/private/account/assets", private=True
        )
        return list(data or [])

    async def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET /api/v1/private/position/open_positions"""
        params = {"symbol": symbol} if symbol else None
        data = await self._request(
            "GET",
            "/api/v1/private/position/open_positions",
            params=params,
            private=True,
        )
        return list(data or [])

    async def account_snapshot(self) -> dict[str, Any]:
        """Fetch assets + open positions and map to API account shape.

        Each position gets its OWN contract_size (F-10) — resolved from
        MEXC's contract metadata (one /contract/detail call covering all
        symbols) rather than the active chart symbol's. If that lookup
        fails or a symbol is missing from it, map_position safely defaults
        that position's contract_size to 1.0.
        """
        assets_raw = await self.assets()
        positions_raw = await self.positions()
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
        for path in self._STOP_ORDER_PATHS:
            try:
                data = await self._request("GET", path, params=params, private=True)
            except MexcError as e:
                last_err = e
                continue
            if isinstance(data, dict) and "resultList" in data:
                return list(data.get("resultList") or [])
            if isinstance(data, list):
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
    ) -> dict[str, Any]:
        """Emergency flatten: close long=side 4, close short=side 2, type market."""
        mexc_side = 4 if side == "long" else 2
        body = {
            "symbol": symbol,
            "price": 0,
            "vol": vol,
            "side": mexc_side,
            "type": 5,
            "openType": open_type,
        }
        return await self.place_order(body)

    async def order_by_external_oid(self, symbol: str, external_oid: str) -> Any:
        """Lookup order by client externalOid (idempotency / timeout recovery).

        The dedicated external-oid endpoint is preferred. The history fallback
        returns a full list, so it MUST be filtered by externalOid before it is
        returned — otherwise timeout recovery could match an unrelated order and
        wrongly treat a failed place as live.
        """
        # Path name varies slightly across doc revisions; try common form
        try:
            return await self._request(
                "GET",
                "/api/v1/private/order/external/" + external_oid,
                params={"symbol": symbol},
                private=True,
            )
        except MexcError:
            data = await self._request(
                "GET",
                "/api/v1/private/order/list/history_orders",
                params={
                    "symbol": symbol,
                    "page_num": 1,
                    "page_size": 20,
                },
                private=True,
            )
            if isinstance(data, dict) and "resultList" in data:
                rows = data.get("resultList") or []
            elif isinstance(data, list):
                rows = data
            else:
                rows = [data] if data else []
            matched = [
                r
                for r in rows
                if isinstance(r, dict)
                and str(r.get("externalOid") or r.get("external_oid") or "")
                == str(external_oid)
            ]
            return matched


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


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
