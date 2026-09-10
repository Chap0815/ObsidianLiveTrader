"""Order preview → confirm → cancel orchestration.

No place without unused, unexpired preview token AND TRADING_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.hyperliquid.errors import HyperliquidError
from app.mexc.client import MexcClient, map_position, usdt_balances
from app.mexc.errors import MexcError
from app.models import ContractMeta, OrderTicket
from app.orders.protection import classify_order_label, classify_protection
from app.orders.tokens import PreviewStore, TokenError
from app.risk.gates import GateResult, validate_order
from app.risk.sizing import round_down_to_unit, round_trigger_to_unit

ExchangeError = (MexcError, HyperliquidError)

# MEXC futures order enums (official create docs / common practice):
# side: 1 open long, 2 close short, 3 open short, 4 close long
# type: 1 limit (price+vol), 2 post-only, 3 IOC, 4 FOK, 5 market
# openType: 1 isolated, 2 cross
MEXC_SIDE_OPEN_LONG = 1
MEXC_SIDE_OPEN_SHORT = 3
MEXC_TYPE_LIMIT = 1
MEXC_TYPE_MARKET = 5
_PREVIEW_EXTERNAL_OID_RE = re.compile(r"^mlt-[0-9a-f]{20}$")


class OrderError(Exception):
    """Business-level order flow error (gates, arming, token, policy)."""

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]


def ticket_to_mexc_body(
    ticket: OrderTicket,
    *,
    rounded_vol: float,
    rounded_price: float | None,
    external_oid: str,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    attach_triggers: bool = True,
) -> dict[str, Any]:
    """Map internal ticket → MEXC POST /api/v1/private/order/create body.

    attach_triggers=False (manual mode): send NO stopLossPrice/takeProfitPrice —
    the trader manages the exit; the SL value is still used for risk gates only.
    """
    side = (ticket.side or "").lower()
    mexc_side = MEXC_SIDE_OPEN_LONG if side == "long" else MEXC_SIDE_OPEN_SHORT
    order_type = (ticket.order_type or "").lower()
    mexc_type = MEXC_TYPE_MARKET if order_type == "market" else MEXC_TYPE_LIMIT

    body: dict[str, Any] = {
        "symbol": ticket.symbol.upper().strip(),
        "vol": rounded_vol,
        "side": mexc_side,
        "type": mexc_type,
        "openType": int(ticket.open_type or 1),
        "leverage": int(ticket.leverage),
        "externalOid": external_oid,
    }

    if mexc_type == MEXC_TYPE_LIMIT:
        if rounded_price is None or rounded_price <= 0:
            raise OrderError("limit order missing rounded price")
        body["price"] = rounded_price
    else:
        body["price"] = 0

    if attach_triggers:
        sl = stop_loss if stop_loss is not None else ticket.stop_loss
        tp = take_profit if take_profit is not None else ticket.take_profit
        if sl is not None and float(sl) > 0:
            body["stopLossPrice"] = float(sl)
        if tp is not None and float(tp) > 0:
            body["takeProfitPrice"] = float(tp)
        if getattr(ticket, "scale_out", False) and ticket.tp2 and float(ticket.tp2) > 0:
            body["takeProfitPrice2"] = float(ticket.tp2)
            body["tp1Share"] = float(getattr(ticket, "tp1_share", 0.5) or 0.5)

    return body


def _audit_order_request(body: dict[str, Any], ticket: OrderTicket) -> dict[str, Any]:
    """Add local provenance metadata without sending it to the exchange."""
    audit = dict(body)
    proposal_id = getattr(ticket, "proposal_id", None)
    if proposal_id is not None:
        audit["_proposal_id"] = int(proposal_id)
    return audit


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _utc_now_iso_from_ts(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _symbols_match(
    reported: Any, wanted: str, *, allow_bare_base_alias: bool = False
) -> bool:
    """Match an exact symbol or, when allowed, only a truly bare base coin."""
    if not isinstance(reported, str) or not isinstance(wanted, str):
        return False
    candidate = reported.strip().upper()
    target = wanted.strip().upper()
    if not candidate or not target:
        return False
    return candidate == target or (
        allow_bare_base_alias
        and "_" not in candidate
        and candidate == target.split("_", 1)[0]
    )


def _position_sl_price(p: dict[str, Any]) -> float | None:
    """Own stop-loss of an open position, if it carries one (R-01).

    Position rows from different adapters/lookups may expose the SL under
    different keys; take the first finite, positive value.
    """
    for key in ("stop_loss", "sl_price", "sl", "stopLossPrice", "stop_price"):
        v = _coerce_float(p.get(key))
        if v is not None and math.isfinite(v) and v > 0:
            return v
    return None


def estimate_same_side_risk_usdt(
    positions: list[dict[str, Any]],
    *,
    symbol: str,
    side: str,
    contract_size: float,
    allow_base_symbol_alias: bool = False,
) -> tuple[float, list[str]]:
    """Open same-side risk (USDT) from a known stop or liquidation price.

    Per position, prefer the loss to its OWN stop-loss when present
    (``abs(entry - sl) * contract_size * vol``). Without an SL, use the full
    known liquidation distance. Missing liquidation data blocks: a percentage
    of entry notional is not a conservative upper bound for an unprotected
    position and could understate aggregate MAX_RISK_PCT.

    Returns ``(total_risk_usdt, warnings)``; the stable tuple shape is retained
    for callers, although the fail-closed calculation currently emits no
    fallback warnings.
    """
    if not isinstance(positions, list):
        raise ValueError("open position data is unavailable or invalid")
    total = 0.0
    warnings: list[str] = []
    for raw in positions:
        if not isinstance(raw, dict):
            raise ValueError("open position row is invalid")
        p = raw if "hold_vol" in raw else map_position(raw)
        symbol_raw = p.get("symbol")
        if not isinstance(symbol_raw, str) or not symbol_raw.strip():
            raise ValueError("open position has invalid symbol/side identity")
        symbol_matches = _symbols_match(
            symbol_raw,
            symbol,
            allow_bare_base_alias=allow_base_symbol_alias,
        )
        if not symbol_matches:
            continue
        side_raw = p.get("side")
        position_side = (
            side_raw.strip().lower() if isinstance(side_raw, str) else ""
        )
        if position_side not in ("long", "short"):
            raise ValueError("open position has invalid symbol/side identity")
        if position_side != side.lower():
            continue
        vol = _coerce_float(p.get("hold_vol"))
        if vol is None or vol < 0:
            raise ValueError(f"open {side} position on {symbol} has invalid hold_vol")
        if vol == 0:
            continue
        entry = _coerce_float(p.get("entry_price"))
        if entry is None or entry <= 0:
            raise ValueError(f"open {side} position on {symbol} has invalid entry_price")
        if contract_size <= 0:
            continue
        sl = _position_sl_price(p)
        if sl is not None:
            # Loss to this position's own stop — the realistic exposure.
            total += abs(entry - sl) * contract_size * vol
            continue
        liq = _coerce_float(p.get("liquidate_price"))
        if liq is None or liq <= 0:
            raise ValueError(
                f"open {side} position on {symbol} has invalid or missing "
                "liquidate_price — "
                "cannot enforce aggregate MAX_RISK_PCT (close or wait for liq data)"
            )
        dist = abs(entry - liq)
        if dist == 0:
            raise ValueError(
                f"open {side} position on {symbol} has invalid liquidate_price — "
                "liquidation distance is zero"
            )
        total += dist * contract_size * vol
    return total, warnings


def _coerce_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            value = float(v)
        except OverflowError:
            return None
        return value if math.isfinite(value) else None
    if isinstance(v, str):
        try:
            value = float(v.strip())
            return value if math.isfinite(value) else None
        except (ValueError, OverflowError):
            return None
    return None


def _validated_close_amount(
    value: Any, *, field: str, maximum: float | None = None
) -> float | None:
    """Normalize one explicit close amount without falling back to full close."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise OrderError(f"{field} must be numeric, not boolean")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OrderError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise OrderError(f"{field} must be finite")
    if parsed <= 0:
        raise OrderError(f"{field} must be > 0")
    if maximum is not None and parsed > maximum:
        raise OrderError(f"{field} must be <= {maximum:g}")
    return parsed


def _extract_filled_vol(resp: Any) -> float | None:
    """Filled quantity of THIS order from the exchange place response.

    Returns the exchange-reported fill (contracts/coins) for our order, or None
    when the response does not carry it. Used to bound auto-flatten to our OWN
    fill so a concurrent external bot that adds same-side volume in the same
    window is never partially closed by us.

    A key that is present but 0 is trusted as "reported unfilled" (returns 0.0)
    — that is the fail-closed choice: we would rather cancel a resting entry
    than market-close volume that may not be ours. A negative report is invalid
    and returns None; it must never be normalized into zero-fill evidence.
    """
    if not isinstance(resp, dict):
        return None
    # Flat quantity keys seen across MEXC revisions / internal adapters.
    for key in (
        "dealVol",
        "deal_vol",
        "dealVolume",
        "filledVol",
        "filled_vol",
        "filledQty",
        "filled_qty",
        "filled",
        "cumQty",
        "filledSz",
    ):
        if key in resp:
            f = _coerce_float(resp.get(key))
            if f is not None:
                return f if f >= 0 else None
    recovered_order = resp.get("order")
    if isinstance(recovered_order, dict):
        recovered_fill = _extract_filled_vol(recovered_order)
        if recovered_fill is not None:
            return recovered_fill
    # Hyperliquid nested SDK shape: response.data.statuses[].filled.totalSz
    for container in (resp.get("response"), resp):
        if not isinstance(container, dict):
            continue
        try:
            statuses = (
                container.get("response", {}).get("data", {}).get("statuses", [])
            )
        except AttributeError:
            statuses = []
        total = 0.0
        found = False
        for st in statuses or []:
            if isinstance(st, dict) and isinstance(st.get("filled"), dict):
                fv = _coerce_float(st["filled"].get("totalSz"))
                if fv is not None:
                    if fv < 0:
                        return None
                    total += fv
                    if not math.isfinite(total):
                        return None
                    found = True
        if found:
            return total
    return None


def _close_response_error(resp: Any) -> str | None:
    """Detect an inner rejection in a market-close response.

    Mirrors the Hyperliquid ``_status_error`` pattern but is exchange-agnostic:
    Hyperliquid returns order errors INSIDE an outwardly-200 response
    (``status != ok`` or a nested ``statuses[].error``), and MEXC surfaces
    ``success: false`` / a non-zero ``code``. Returning the error text here lets
    the close path report a FAILED close instead of a false ``closed``/``ok``.
    """
    if isinstance(resp, list):
        if not resp:
            return "unrecognized exchange response"
        for item in resp:
            error = _close_response_error(item)
            if error:
                return error
        return None
    if not isinstance(resp, dict):
        return "unrecognized exchange response"
    # MEXC-shaped markers. Absence is compatible, but every marker that is
    # present must have the adapter's exact success type/value. In particular,
    # Python booleans and floats must not pass as integer zero (False == 0 and
    # 0.0 == 0).
    for marker_name in ("code", "errorCode", "error_code"):
        if marker_name not in resp:
            continue
        marker = resp.get(marker_name)
        if (
            isinstance(marker, int)
            and not isinstance(marker, bool)
            and marker == 0
        ) or (isinstance(marker, str) and marker == "0"):
            continue
        return f"{marker_name}={marker} {resp.get('message') or ''}".strip()
    if "success" in resp and resp.get("success") is not True:
        return str(resp.get("message") or "invalid success marker")
    # Hyperliquid-shaped rejections (same shape as client._status_error).
    status = resp.get("status")
    if status is not None and status != "ok":
        return str(status)
    try:
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
    except Exception:  # noqa: BLE001
        return "invalid exchange statuses"
    if not isinstance(statuses, list):
        return "invalid exchange statuses"
    if status == "ok" and not statuses:
        return "invalid exchange statuses"
    for st in statuses:
        if st == "success":
            continue
        if not isinstance(st, dict):
            return "invalid exchange statuses"
        if "error" in st:
            return str(st["error"]).strip() or "unknown exchange error"
        if not any(isinstance(st.get(key), dict) for key in ("filled", "resting")):
            return "invalid exchange statuses"
    return None


def _recovery_is_match(recovered: Any, external_oid: str) -> bool:
    """True if `recovered` is trustworthy evidence our order is already live.

    O-05: accepts either an exchange-client MATCH MARKER — a dict carrying a
    truthy ``match`` field, which the client sets only after matching OUR
    oid/cloid (MEXC ``history``/``open`` and HL ``cloid`` are field-filtered) —
    or, lacking a marker, an exact external-oid field on a dict/list row (the HL
    list-of-hits fallback). Free-text substring matches are not evidence: a
    diagnostic like ``<oid> not found`` must remain a failed recovery.
    """
    if not recovered:
        return False
    if isinstance(recovered, dict) and recovered.get("match"):
        marker = str(recovered.get("match") or "").lower()
        if marker not in {"direct", "history", "open", "cloid"}:
            return False
        if not _has_exact_external_oid(recovered, external_oid):
            return False
        order_wrapper = recovered.get("order")
        if not isinstance(order_wrapper, dict):
            return False
        if marker == "cloid":
            return _has_consistent_positive_order_id(order_wrapper.get("order"))
        return (
            _has_exact_external_oid(order_wrapper, external_oid)
            and _has_consistent_positive_order_id(order_wrapper)
        )
    rows = recovered if isinstance(recovered, list) else [recovered]
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (
            _has_exact_external_oid(row, external_oid)
            and _has_consistent_positive_order_id(row)
        ):
            return True
    return False


def _has_exact_external_oid(row: Any, external_oid: str) -> bool:
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


def _has_consistent_positive_order_id(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    values: list[str] = []
    for key in ("orderId", "order_id", "oid"):
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool):
            return False
        text = str(value)
        if not text.isdigit():
            return False
        try:
            parsed = int(text)
        except ValueError:
            return False
        if parsed <= 0:
            return False
        values.append(str(parsed))
    return bool(values) and len(set(values)) == 1


def _consistent_stop_order_id(row: Any) -> int | None:
    """Return one unambiguous positive ID from a normalized stop-order row."""
    if not isinstance(row, dict):
        return None
    raw = row.get("raw")
    sources = (row, raw) if isinstance(raw, dict) else (row,)
    values: list[int] = []
    for source in sources:
        for key in ("orderId", "oid"):
            if key not in source:
                continue
            value = source.get(key)
            if isinstance(value, bool):
                return None
            text = str(value)
            if not text.isdigit():
                return None
            try:
                parsed = int(text)
            except (ValueError, OverflowError):
                return None
            if parsed <= 0:
                return None
            values.append(parsed)
    if not values or len(set(values)) != 1:
        return None
    return values[0]


def _is_uncertain_order_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "timeout",
            "timed out",
            "connect",
            "network",
            "invalid json",
            "uncertain order-create response",
            "uncertain close response",
        )
    )


def _sl_matches(expected: float, candidate: float | None, tol_pct: float = 0.15) -> bool:
    if isinstance(expected, bool) or candidate is None or isinstance(candidate, bool):
        return False
    try:
        expected_f = float(expected)
        candidate_f = float(candidate)
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        not math.isfinite(expected_f)
        or expected_f <= 0
        or not math.isfinite(candidate_f)
        or candidate_f <= 0
    ):
        return False
    return abs(candidate_f - expected_f) / expected_f * 100.0 <= tol_pct


def _consistent_positive_price_aliases(
    row: Any, keys: tuple[str, ...]
) -> tuple[float | None, tuple[str, ...]]:
    """Return one price only when every present alias is valid and agrees."""
    if not isinstance(row, dict):
        return None, ()
    present = tuple(key for key in keys if key in row)
    if not present:
        return None, ()
    values: list[float] = []
    for key in present:
        parsed = _coerce_float(row.get(key))
        if parsed is None or parsed <= 0:
            return None, present
        values.append(parsed)
    if len(set(values)) != 1:
        return None, present
    return values[0], present


def scale_out_errors(
    ticket: OrderTicket, entry: float | None, client: Any = None
) -> list[str]:
    """Geometry + exchange-support checks for the optional two-rung TP ladder.

    Empty list = ok. Scale-out is HYPERLIQUID-ONLY: the ladder split (two
    reduce-only TP triggers) is implemented only in HyperliquidClient.place_order.
    MEXC's create-order API would silently ignore takeProfitPrice2/tp1Share and
    place only a single TP — a stale half-execution the trader never asked for.
    """
    if not getattr(ticket, "scale_out", False):
        return []
    if getattr(client, "exchange_id", "") != "hyperliquid":
        return ["Scale-out is only available on Hyperliquid"]
    errs: list[str] = []
    tp1 = ticket.take_profit
    tp2 = ticket.tp2
    if tp1 is None or float(tp1) <= 0:
        errs.append("scale-out requires TP1 (take_profit)")
    tp2_f: float | None = None
    if tp2 is not None:
        try:
            if isinstance(tp2, bool):
                raise ValueError
            tp2_f = float(tp2)
            if not math.isfinite(tp2_f) or tp2_f <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            tp2_f = None
    if tp2_f is None:
        errs.append("scale-out requires a positive finite TP2 (tp2)")
    try:
        raw_share = getattr(ticket, "tp1_share", 0.5)
        if isinstance(raw_share, bool):
            raise ValueError
        share = float(raw_share)
    except (TypeError, ValueError, OverflowError):
        share = float("nan")
    if not math.isfinite(share) or not (0.0 < share < 1.0):
        errs.append("tp1_share must be between 0 and 1")
    if errs:
        return errs
    side = (ticket.side or "").lower()
    e = float(entry) if entry else None
    if side == "long":
        if not (tp2_f > float(tp1)):
            errs.append("long scale-out: TP2 must be above TP1")
        if e is not None and not (float(tp1) > e):
            errs.append("long scale-out: TP1 must be above entry")
    elif side == "short":
        if not (tp2_f < float(tp1)):
            errs.append("short scale-out: TP2 must be below TP1")
        if e is not None and not (float(tp1) < e):
            errs.append("short scale-out: TP1 must be below entry")
    return errs


class OrderService:
    def __init__(
        self,
        client: MexcClient,
        settings: Settings,
        store: PreviewStore,
        db: Any | None = None,
        trade_lock: "asyncio.Lock | None" = None,
    ):
        self.client = client
        self.settings = settings
        self.store = store
        self.db = db
        # Serialize confirm/close so parallel places cannot both see risk=0.
        # MUST be shared across requests: a new OrderService is built per
        # request, so a per-instance lock would serialize nothing. The caller
        # passes an app-global lock; the fallback only helps single-instance
        # unit tests.
        self._trade_lock = trade_lock if trade_lock is not None else asyncio.Lock()

    async def _audit_order_best_effort(self, **fields: Any) -> str | None:
        """Persist an order audit without obscuring the exchange outcome."""
        if self.db is None:
            return None
        try:
            await self.db.insert_order(**fields)
        except Exception as exc:  # noqa: BLE001 — exchange result stays authoritative
            return f"audit log failed: {exc}"
        return None

    async def _read_account_state(
        self, symbol: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fresh (assets, positions) for the money path in ONE combined read.

        Finding 2: the real HL/MEXC clients expose ``account_state`` which fetches
        both from a SINGLE snapshot (HL: one clearinghouseState; MEXC: two
        endpoints fired concurrently), replacing the old separate
        ``assets(fresh=True)`` + ``positions(fresh=True)`` reads. ``fresh=True`` is
        preserved end-to-end, so the 429 fail-closed / no-stale-serve guarantee is
        identical.

        A client that does not implement the combined method (e.g. a bare test
        double) transparently falls back to the two sequential reads — detected on
        the CLASS so an auto-attributing mock does not accidentally match.
        """
        combined = getattr(type(self.client), "account_state", None)
        if inspect.iscoroutinefunction(combined):
            result = await self.client.account_state(symbol, fresh=True)
        else:
            result = (
                await self.client.assets(fresh=True),
                await self.client.positions(symbol, fresh=True),
            )
        if not isinstance(result, tuple) or len(result) != 2:
            raise OrderError("combined account state envelope is invalid")
        assets, positions = result
        if (
            not isinstance(assets, list)
            or not isinstance(positions, list)
            or any(not isinstance(row, dict) for row in assets)
            or any(not isinstance(row, dict) for row in positions)
        ):
            raise OrderError("combined account state collections are invalid")
        return assets, positions

    def _map_balances(self, assets: list[dict[str, Any]]) -> tuple[float, float]:
        """(equity, available) from an already-fetched assets blob. Fail-closed on
        a zero/unknown equity. Fetch errors are handled by the caller; adapter
        mapping errors are translated here for the shared preview/confirm path."""
        try:
            equity, available = usdt_balances(assets)
        except MexcError as exc:
            raise OrderError(
                "equity unknown or available margin invalid — fail-closed"
            ) from exc
        if equity is None or not math.isfinite(float(equity)) or float(equity) <= 0:
            raise OrderError(
                "equity unknown/zero — fail-closed (cannot enforce MAX_RISK_PCT)"
            )
        if available is None or not math.isfinite(float(available)):
            raise OrderError(
                "available margin unknown/non-finite — fail-closed"
            )
        return float(equity), float(available)

    def _map_existing_risk(
        self,
        positions: list[dict[str, Any]],
        symbol: str,
        side: str,
        contract_size: float,
    ) -> tuple[float, list[str]]:
        """Same-side open risk + warnings from already-fetched positions.

        Preview, Confirm and sizing use the same fail-closed aggregate semantics.
        """
        try:
            return estimate_same_side_risk_usdt(
                positions,
                symbol=symbol,
                side=side,
                contract_size=contract_size,
                allow_base_symbol_alias=(
                    getattr(self.client, "exchange_id", "") == "hyperliquid"
                ),
            )
        except ValueError as e:
            raise OrderError(str(e)) from e

    async def _balances(self) -> tuple[float, float]:
        """Return (equity, available). Fail-closed on API/mapping errors.

        fresh=True: this equity feeds order SIZING/gating, so it must never come
        from the 429 stale-serve cache (which could hand back optimistic-high
        equity during a volatile rate-limit burst and let an oversized order pass
        MAX_RISK_PCT). On a 429 here we fail closed instead."""
        try:
            assets = await self.client.assets(fresh=True)
        except ExchangeError as e:
            raise OrderError(f"equity unavailable: {e}") from e
        return self._map_balances(assets)

    async def _existing_risk(
        self, symbol: str, side: str, contract_size: float
    ) -> tuple[float, list[str]]:
        """Same-side open risk + warnings. Positions API failure is fail-closed."""
        try:
            # fresh=True: aggregate-exposure input to the risk gate — never the
            # 429 stale-serve cache (stale positions could understate open risk).
            positions = await self.client.positions(symbol, fresh=True)
        except ExchangeError as e:
            # Fail-closed: treating unknown exposure as 0 would understate MAX_RISK_PCT
            raise OrderError(
                f"positions unavailable — cannot enforce aggregate same-side risk: {e}"
            ) from e
        return self._map_existing_risk(positions, symbol, side, contract_size)

    async def _ensure_no_pending_same_side_entry(
        self, symbol: str, side: str
    ) -> None:
        """Block MEXC exposure stacking that the positions snapshot cannot see.

        A resting opening order can fill after Confirm returns. Until pending
        entry risk is modelled explicitly, accepting another same-side entry
        would let both orders consume the same aggregate risk budget.
        """
        if getattr(self.client, "exchange_id", "") != "mexc":
            return
        try:
            rows = await self.client.open_orders(symbol)
        except ExchangeError as e:
            raise OrderError(
                "open orders unavailable — cannot verify pending entry exposure"
            ) from e
        if not isinstance(rows, list):
            raise OrderError(
                "open orders returned an invalid shape — pending exposure unknown"
            )

        wanted_side = MEXC_SIDE_OPEN_LONG if side == "long" else MEXC_SIDE_OPEN_SHORT
        wanted_symbol = symbol.upper()
        for row in rows:
            if not isinstance(row, dict):
                raise OrderError(
                    "open orders contain an invalid row — pending exposure unknown"
                )
            row_symbol = str(row.get("symbol") or "").upper().strip()
            if not row_symbol:
                raise OrderError(
                    "open order has no symbol — pending exposure unknown"
                )
            if row_symbol != wanted_symbol:
                continue
            side_value = _coerce_float(row.get("side"))
            if side_value is None or not side_value.is_integer():
                raise OrderError(
                    f"open order on {symbol} has invalid side — pending exposure unknown"
                )
            side_code = int(side_value)
            if side_code not in (1, 2, 3, 4):
                raise OrderError(
                    f"open order on {symbol} has unknown side — pending exposure unknown"
                )
            if side_code == wanted_side:
                raise OrderError(
                    f"pending {side} entry already exists on {symbol} — "
                    "cancel or resolve it before placing another"
                )

    async def preview(self, ticket: OrderTicket) -> dict[str, Any]:
        """Run gates, optionally issue one-time token + persist preview hash."""
        symbol = ticket.symbol.upper().strip()
        ticket = ticket.model_copy(update={"symbol": symbol})

        # A proposal link is optional, but if supplied it must identify a real
        # stored proposal for this symbol and direction. This makes the later
        # reevaluation anchor provenance, not caller-controlled decoration.
        if ticket.proposal_id is not None:
            if self.db is None:
                raise OrderError("proposal provenance unavailable — preview blocked")
            try:
                proposal_row = await self.db.proposal_by_id(ticket.proposal_id)
            except Exception as exc:  # database uncertainty must fail closed
                raise OrderError(
                    "proposal provenance lookup failed — preview blocked"
                ) from exc
            proposal = (proposal_row or {}).get("proposal")
            action = str((proposal or {}).get("action") or "").upper()
            action_side = (
                "long"
                if action in {"BUY", "STRONG_BUY"}
                else "short"
                if action in {"SELL", "STRONG_SHORT"}
                else None
            )
            if (
                not proposal_row
                or proposal_row.get("symbol") != symbol
                or action_side != ticket.side
            ):
                raise OrderError(
                    "proposal_id does not match this ticket's symbol and side"
                )

        # Contract meta, ticker, the combined account read and MEXC's pending-entry
        # guard are independent, so fetch them concurrently. Results keep the
        # established error priority (contract → ticker → account → pending).
        contract_r, ticker_r, account_r, pending_r = await asyncio.gather(
            self.client.contract_meta(symbol),
            self.client.ticker(symbol),
            self._read_account_state(symbol),
            self._ensure_no_pending_same_side_entry(symbol, ticket.side),
            return_exceptions=True,
        )

        if isinstance(contract_r, BaseException):
            if isinstance(contract_r, ExchangeError):
                raise OrderError(f"contract meta failed: {contract_r}") from contract_r
            raise contract_r
        contract = contract_r

        last_price: float | None = None
        if isinstance(ticker_r, BaseException):
            if isinstance(ticker_r, ExchangeError):
                raise OrderError(f"ticker failed: {ticker_r}") from ticker_r
            raise ticker_r
        last_price = _coerce_float(ticker_r.last_price)
        if last_price is None or last_price <= 0:
            raise OrderError("ticker price unavailable or invalid")

        # Account read + its mappings share the fail-closed dict the old separate
        # _balances / _existing_risk failures returned (Preview shows gate errors,
        # never a 500). A fresh-read fetch failure maps to the same "equity
        # unavailable" OrderError class the balances path raised.
        try:
            if isinstance(account_r, BaseException):
                if isinstance(account_r, ExchangeError):
                    raise OrderError(f"equity unavailable: {account_r}") from account_r
                raise account_r
            assets, positions = account_r
            equity, available = self._map_balances(assets)
            existing, existing_warnings = self._map_existing_risk(
                positions, symbol, ticket.side, contract.contract_size
            )
            if isinstance(pending_r, BaseException):
                raise pending_r
        except OrderError as e:
            # Preview: still return gate errors instead of 500
            return {
                "ok": False,
                "token": None,
                "errors": list(e.errors),
                "warnings": [],
                "gate": {"ok": False, "errors": list(e.errors)},
                "summary": {},
            }
        gate = validate_order(
            ticket,
            contract,
            equity,
            self.settings,
            last_price=last_price,
            for_confirm=False,
            existing_same_side_risk_usdt=existing,
            existing_same_side_warnings=existing_warnings,
            available_usdt=available,
        )

        summary = _confirm_summary(ticket, gate, contract, equity, last_price)
        summary["available_usdt"] = available
        summary["existing_same_side_risk_usdt"] = existing

        if not gate.ok:
            return {
                "ok": False,
                "token": None,
                "errors": gate.errors,
                "warnings": gate.warnings,
                "gate": gate.to_dict(),
                "summary": summary,
            }

        so_errs = scale_out_errors(ticket, gate.entry_for_risk, self.client)
        if so_errs:
            return {
                "ok": False,
                "token": None,
                "errors": so_errs,
                "warnings": gate.warnings,
                "gate": gate.to_dict(),
                "summary": summary,
            }

        # Fix externalOid at preview for idempotent confirm retries
        external_oid = f"mlt-{uuid.uuid4().hex[:20]}"
        payload = {
            "ticket": ticket.model_dump(),
            "gate": gate.to_dict(),
            "contract": {
                "symbol": contract.symbol,
                "contract_size": contract.contract_size,
                "price_unit": contract.price_unit,
                "vol_unit": contract.vol_unit,
                "min_vol": contract.min_vol,
                "max_vol": contract.max_vol,
                "max_leverage": contract.max_leverage,
                "api_allowed": contract.api_allowed,
            },
            "equity_usdt": equity,
            "available_usdt": available,
            "last_price": last_price,
            "external_oid": external_oid,
            "created_at": _utc_now_iso(),
        }
        ttl = self.settings.preview_token_ttl_seconds
        token = self.store.create(payload, ttl)
        token_hash = PreviewStore.hash_token(token)

        if self.db is not None:
            # Mirror PreviewStore.create()'s clamp (app/orders/tokens.py) so the
            # DB-preview row's expiry never diverges from the in-memory token's
            # for ttl<=0 (both must agree on when a preview is actually gone).
            expires = datetime.now(timezone.utc).timestamp() + max(1, int(ttl))
            try:
                await self.db.insert_preview(
                    token_hash=token_hash,
                    payload_json=payload,
                    expires_at=_utc_now_iso_from_ts(expires),
                )
            except BaseException:
                # The response never disclosed this token. Do not leave hidden
                # confirm authority behind, but preserve any newer single-slot
                # token that a concurrent preview may already have created.
                self.store.discard(token)
                raise

        return {
            "ok": True,
            "token": token,
            "errors": [],
            "warnings": gate.warnings,
            "gate": gate.to_dict(),
            "summary": summary,
            "expires_in_seconds": ttl,
            "external_oid": external_oid,
        }

    async def _verify_sl_attached(
        self,
        *,
        symbol: str,
        expected_sl: float,
        side: str,
        pre_existing_same_side: bool = False,
    ) -> tuple[bool, str, bool]:
        """Best-effort check that exchange has SL protection near expected_sl.

        Returns (verified, detail, checked).

        `checked` is True ONLY if the reliable SL source — the stop/plan-order
        lookup — succeeded at least once. A position row that lacks an SL field
        is NOT proof the SL is missing (MEXC usually does not surface a planned
        SL on the position), so it never sets `checked`. This prevents a broken
        stop-order endpoint from producing a false "MISSING" and auto-flattening
        a genuinely protected trade.

        Retries a few times because exchanges reflect a freshly placed trigger
        with a short delay — an immediate single lookup can miss a real SL.

        F-C1 hardening: this fallback only runs when no concrete new-trigger oid
        is available (see caller — the oid short-circuit at confirm time already
        covers the safe case). Without an oid, a price-only match here cannot
        tell OUR new stop apart from an OLD same-side stop that happens to rest
        near the same price (e.g. adding to a position, or re-entering at a
        similar level) — that old stop is sized for the OLD volume only and
        does not protect the freshly added size. `pre_existing_same_side=True`
        signals that ambiguity was possible; in that case a price-only match is
        recorded as unresolved evidence and the result is UNKNOWN (checked=
        False), never a false "verified". UNKNOWN never auto-flattens (see
        confirm's flatten gate, which requires sl_checked=True) — it only
        surfaces a loud manual-check warning, so the existing fail-safe holds.
        """
        sl_keys = (
            "stopLossPrice",
            "stop_loss_price",
            "stopLoss",
            "stop_loss",
            "slPrice",
            "sl_price",
        )
        stops_ever_ok = False
        # O-01: on MEXC the SL is position-bound in the create body and usually
        # does NOT surface as a separate plan order, so an empty stop list is the
        # NORMAL answer and is NOT proof the SL is missing. `saw_sl_field` records
        # whether any stop/position object actually CARRIED an SL-ish field — only
        # then is a non-match trustworthy enough to count as `checked` on MEXC.
        exchange_id = getattr(self.client, "exchange_id", "")
        is_mexc = exchange_id == "mexc"
        is_hl = exchange_id == "hyperliquid"
        saw_sl_field = False
        invalid_stop_evidence = False
        invalid_position_evidence = False
        last_detail = ""
        attempts = max(1, int(getattr(self.settings, "sl_verify_attempts", 3)))
        delay_s = max(0.0, float(getattr(self.settings, "sl_verify_delay_s", 0.7)))
        for attempt in range(attempts):
            ambiguous_price_match = False

            # 1) Stop / plan orders — the authoritative SL source.
            try:
                stops = await self.client.open_stop_orders(symbol)
                if not isinstance(stops, list) or not all(
                    isinstance(stop, dict) for stop in stops
                ):
                    last_detail = (
                        "open stop orders returned an invalid response shape "
                        f"({type(stops).__name__})"
                    )
                    stops = []
                else:
                    stops_ever_ok = True
                for s in stops:
                    if not _symbols_match(
                        s.get("symbol"), symbol, allow_bare_base_alias=is_hl
                    ):
                        continue
                    kind = str(
                        s.get("orderType") or s.get("tpsl") or s.get("type") or ""
                    ).lower()
                    # Shared backend classifier (Q-05): the SAME SL/TP label rule
                    # the reevaluate extractor uses, so the two money-path sites
                    # cannot drift. `classify_order_label` returns 'tp' only on an
                    # unambiguous take-profit marker (never bare "tpsl"), keeping
                    # this verifier bit-identical to its prior inline rule.
                    label = classify_order_label(kind)
                    explicit_price, explicit_fields = (
                        _consistent_positive_price_aliases(
                            s, ("stopLossPrice", "stop_loss_price")
                        )
                    )
                    if explicit_fields:
                        if explicit_price is None:
                            invalid_stop_evidence = True
                            last_detail = (
                                "stop order explicit SL aliases were invalid or "
                                "conflicting"
                            )
                            continue
                        saw_sl_field = True
                        if _sl_matches(expected_sl, explicit_price):
                            if pre_existing_same_side:
                                ambiguous_price_match = True
                                last_detail = (
                                    "price-only match on explicit stop-loss field "
                                    "— cannot confirm it protects the newly added "
                                    "size (a same-side position pre-existed); not "
                                    "counted as verified"
                                )
                                continue
                            return True, "explicit stop-loss field matched", True
                        continue
                    if label == "tp":
                        continue
                    _trigger_price, trigger_fields = (
                        _consistent_positive_price_aliases(
                            s,
                            (
                                "stopPrice",
                                "stop_price",
                                "triggerPrice",
                                "trigger_price",
                            ),
                        )
                    )
                    if trigger_fields and _trigger_price is None:
                        invalid_stop_evidence = True
                        last_detail = (
                            "stop order trigger aliases were invalid or conflicting"
                        )
                        continue
                    for key in (
                        "stopPrice",
                        "stop_price",
                        "triggerPrice",
                        "trigger_price",
                    ):
                        if key not in s:
                            continue
                        # A bare triggerPrice can be a TP; only accept it as SL
                        # proof when the order kind is SL-ish (or unmarked/plan).
                        if key.startswith("trigger") and not (
                            label == "sl" or kind in ("", "plan")
                        ):
                            continue
                        # A stop object that actually carries an SL-ish field is
                        # real evidence the endpoint reports SLs — a non-match here
                        # is then trustworthy (even on MEXC).
                        candidate = s.get(key)
                        if candidate is None:
                            continue
                        candidate_price = _coerce_float(candidate)
                        if candidate_price is None or candidate_price <= 0:
                            invalid_stop_evidence = True
                            last_detail = (
                                f"stop order field {key} contained an invalid price"
                            )
                            continue
                        saw_sl_field = True
                        if _sl_matches(expected_sl, candidate_price):
                            if pre_existing_same_side:
                                ambiguous_price_match = True
                                last_detail = (
                                    f"price-only match on stop order field {key} "
                                    "— cannot confirm it protects the newly added "
                                    "size (a same-side position pre-existed); not "
                                    "counted as verified"
                                )
                                continue
                            return True, f"stop order field {key} matched", True
            except ExchangeError as e:
                last_detail = str(e)

            # 2) Position row fields — ADDITIONAL positive evidence only. Their
            # absence never proves the SL is missing, so it must not set checked.
            try:
                positions = await self.client.positions(symbol)
                if not isinstance(positions, list) or not all(
                    isinstance(position, dict) for position in positions
                ):
                    last_detail = last_detail or (
                        "positions returned an invalid response shape "
                        f"({type(positions).__name__})"
                    )
                    positions = []
                for raw in positions:
                    p = map_position(raw) if "hold_vol" not in raw else raw
                    if str(p.get("symbol") or "").upper() != symbol.upper():
                        continue
                    if str(p.get("side") or "").lower() != side.lower():
                        continue
                    sl_evidence: dict[str, Any] = {}
                    for key in sl_keys:
                        if key in raw:
                            sl_evidence[key] = raw.get(key)
                        elif key in p:
                            sl_evidence[key] = p.get(key)
                    candidate_price, position_fields = (
                        _consistent_positive_price_aliases(sl_evidence, sl_keys)
                    )
                    if not position_fields:
                        continue
                    if candidate_price is None:
                        invalid_position_evidence = True
                        last_detail = "position SL aliases were invalid or conflicting"
                        continue
                    saw_sl_field = True
                    if _sl_matches(expected_sl, candidate_price):
                        if pre_existing_same_side:
                            ambiguous_price_match = True
                            last_detail = (
                                "price-only match on position SL field — cannot "
                                "confirm it protects the newly added size (a "
                                "same-side position pre-existed); not counted as "
                                "verified"
                            )
                            continue
                        return True, "position SL field matched", True
            except ExchangeError as e:
                last_detail = last_detail or str(e)

            if ambiguous_price_match:
                # Do not resolve to MISSING either (that could wrongly trip
                # auto-flatten against a still-protected old position) — return
                # UNKNOWN immediately; retrying will not resolve the ambiguity.
                return False, last_detail, False

            # Not found yet — the trigger may simply not be reflected. Wait & retry.
            if attempt < attempts - 1 and delay_s > 0:
                await asyncio.sleep(delay_s)

        # O-01: on MEXC an empty/absent SL field is NOT proof of a missing SL
        # (the SL is position-bound in the create body). Only treat the check as
        # conclusive (`checked=True`) when a stop/position object actually carried
        # an SL field; otherwise return UNKNOWN so the confirm flow can resolve it
        # via fill evidence (positive verify) instead of a false MISSING → flatten.
        checked = (
            saw_sl_field
            and not invalid_stop_evidence
            and not invalid_position_evidence
            if is_mexc
            else stops_ever_ok and not invalid_stop_evidence
        )
        return (
            False,
            last_detail or "no matching stop found on exchange",
            checked,
        )

    async def _mexc_market_fill_delta(
        self, symbol: str, side: str, pre_hold: float, fill_eps: float
    ) -> tuple[float | None, bool]:
        """Settle-loop same-side hold read for a MEXC MARKET fill (X2-02).

        A fast market fill can beat a slow positions endpoint: a single-shot read
        then measures delta≈0 and misreports the filled entry as ``unfilled_resting``
        ("LIMIT RUHT — kein SL"), which is false info. Retry with the SAME cadence
        as ``_verify_sl_attached`` (``sl_verify_attempts``/``sl_verify_delay_s``)
        until a non-trivial fill delta appears. Returns ``(delta, known)`` — ``known``
        is True once ANY read succeeded, so an all-zero result is still trusted as
        genuinely resting (never invents a fill). LIMIT orders do NOT use this: a
        resting limit is the normal state and its hold-delta must never verify (X2-01).
        """
        attempts = max(1, int(getattr(self.settings, "sl_verify_attempts", 3)))
        delay_s = max(0.0, float(getattr(self.settings, "sl_verify_delay_s", 0.7)))
        delta: float | None = None
        known = False
        for attempt in range(attempts):
            hold_now, _mot, hold_ok = await self._same_side_hold_vol_ok(symbol, side)
            if hold_ok:
                delta = max(0.0, hold_now - pre_hold)
                known = True
                if delta > fill_eps:
                    break
            if attempt < attempts - 1 and delay_s > 0:
                await asyncio.sleep(delay_s)
        return delta, known

    async def _mexc_order_fill_confirmed(
        self, symbol: str, external_oid: str, rounded_vol: float, vol_eps: float
    ) -> bool:
        """Order-own fill evidence for a MEXC LIMIT without a reported fill (X2-01).

        A hold-delta can be an EXTERNAL same-side bump on a still-resting limit, so
        it must NEVER verify a limit (that is exactly the class of unprotected order
        the positive verify was built to guard). Instead ask the exchange about OUR
        order by ``externalOid``: only a ``dealVol`` at/above the ordered size — or a
        smaller dealVol paired with an explicit fully-filled state — counts as OUR
        fill. Any lookup failure / ambiguity returns False → the caller keeps UNKNOWN
        (loud), never a silent verify and never a new flatten path.
        """
        try:
            found = await self.client.order_by_external_oid(symbol, external_oid)
        except Exception:  # noqa: BLE001 — any lookup failure → UNKNOWN, fail-closed
            return False
        if not isinstance(found, dict) or not found:
            return False
        order = found.get("order")
        if not isinstance(order, dict):
            return False
        deal = _coerce_float(
            order.get("dealVol")
            if order.get("dealVol") is not None
            else order.get("deal_vol")
            if order.get("deal_vol") is not None
            else order.get("dealVolume")
        )
        if deal is None:
            return False
        # Primary signal: our order filled at/above the ordered size.
        if deal >= rounded_vol - vol_eps:
            return True
        # Corroborated signal: a partial dealVol PLUS an explicit fully-filled
        # state (MEXC futures state 3 = completed/filled).
        state = str(order.get("state") or order.get("orderState") or "").lower()
        if deal > vol_eps and state in ("3", "filled", "completed", "done"):
            return True
        return False

    async def _same_side_hold_vol_ok(
        self, symbol: str, side: str, *, fresh: bool = False
    ) -> tuple[float, int, bool]:
        """Return (hold_vol, open_type 0|1|2, checked) for same-side position.

        ``open_type=0`` means a matching position did not provide a recognized
        margin mode; money paths that need it must fail closed.

        ``checked`` is True only if the positions query SUCCEEDED. On lookup
        failure it is False so callers can fail-closed instead of trusting the
        silent 0.0 — a fake "no pre-existing size" would let auto-flatten treat
        an OLD position as a fresh fill and market-close it.

        More than one matching (symbol, side) row is ambiguous and therefore
        also returns ``checked=False``; money paths must never guess which size
        or margin mode is authoritative.

        ``fresh`` forces a live positions read (bypassing the client's ~2s cache).
        The SL-modify path needs it: a reduce-only stop sized to a <=2s-stale hold
        under-covers a position that grew externally, and the modify then cancels
        the old (larger) stop → net under-protection.
        """
        try:
            positions = await self.client.positions(symbol, fresh=fresh)
        except ExchangeError:
            return 0.0, 1, False
        if not isinstance(positions, list):
            return 0.0, 1, False
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        matched: tuple[float, int] | None = None
        for raw in positions:
            if not isinstance(raw, dict):
                return 0.0, 1, False
            p = map_position(raw) if "hold_vol" not in raw else raw
            symbol_raw = p.get("symbol")
            if not isinstance(symbol_raw, str) or not symbol_raw.strip():
                return 0.0, 1, False
            if not _symbols_match(
                symbol_raw, symbol, allow_bare_base_alias=is_hl
            ):
                continue
            side_raw = p.get("side")
            position_side = (
                side_raw.strip().lower() if isinstance(side_raw, str) else ""
            )
            if position_side not in ("long", "short"):
                return 0.0, 1, False
            if position_side != side.lower():
                continue
            hv = _coerce_float(p.get("hold_vol"))
            ot_raw = p.get("open_type")
            if isinstance(ot_raw, bool):
                ot = 0
            elif ot_raw in (2, "2", "cross"):
                ot = 2
            elif ot_raw in (1, "1", "isolated"):
                ot = 1
            else:
                ot = 0
            if hv is None or hv < 0:
                return 0.0, ot, False
            if matched is not None:
                return 0.0, 0, False
            matched = (hv, ot)
        if matched is not None:
            return matched[0], matched[1], True
        return 0.0, 1, True

    async def _mexc_pre_hold_and_position_id(
        self,
        symbol: str,
        side: str,
        *,
        positions: list[dict[str, Any]] | None = None,
    ) -> tuple[tuple[float, int, bool], int | None]:
        """MEXC-only: one positions read → (pre_hold triple, same-side positionId).

        ``positions`` lets the caller inject an ALREADY-FETCHED positions snapshot
        (the confirm gate's combined account read) so no second positions request
        is issued (Finding 2). A supplied list still has to contain at most one
        valid same-side row; ambiguity or an invalid hold returns
        ``checked=False``. When omitted the original self-read behaviour is kept
        for any other caller.

        F-1: MEXC change_leverage rejects a leverage set while a position is open
        unless positionId is supplied (see MexcClient.set_leverage docstring), so
        an add-on onto an open position needs the existing position's id. Rather
        than adding a NEW positions call, this reuses the read that already backs
        ``pre_hold`` (taken here, just moved ahead of set_leverage on MEXC), so
        the fix costs no extra API request. The returned triple matches
        ``_same_side_hold_vol_ok`` exactly ((hold_vol, open_type 1|2, checked)),
        and the caller uses it verbatim as ``pre_hold``.

        Fail-closed: a positions lookup failure returns ((0.0, 1, False), None) —
        checked=False so the caller fails closed on hold just like the original
        pre_hold path. A positive matching hold with a missing/garbage id also
        returns checked=False, blocking before set_leverage; only a verified flat
        snapshot may use the no-position form.
        """
        if positions is None:
            try:
                positions = await self.client.positions(symbol)
            except ExchangeError:
                return (0.0, 1, False), None
        matched: tuple[tuple[float, int, bool], int | None] | None = None
        for raw in positions or []:
            p = map_position(raw) if "hold_vol" not in raw else raw
            if str(p.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(p.get("side") or "").lower() != side.lower():
                continue
            hv_raw = _coerce_float(p.get("hold_vol"))
            ot_raw = p.get("open_type")
            if isinstance(ot_raw, bool):
                ot = 0
            elif ot_raw in (2, "2", "cross"):
                ot = 2
            elif ot_raw in (1, "1", "isolated"):
                ot = 1
            else:
                ot = 0
            if hv_raw is None or hv_raw < 0:
                return (0.0, ot, False), None
            raw_pid = p.get("position_id")
            if isinstance(raw_pid, bool):
                pid: int | None = None
            elif isinstance(raw_pid, int) and raw_pid > 0:
                pid = raw_pid
            elif isinstance(raw_pid, str) and raw_pid.isdigit():
                try:
                    parsed_pid = int(raw_pid)
                except ValueError:
                    pid = None
                else:
                    pid = parsed_pid if parsed_pid > 0 else None
            else:
                pid = None
            if hv_raw > 0 and pid is None:
                return (hv_raw, ot, False), None
            if matched is not None:
                return (0.0, 0, False), None
            matched = ((hv_raw, ot, True), pid)
        if matched is not None:
            return matched
        return (0.0, 1, True), None

    async def confirm(self, token: str) -> dict[str, Any]:
        """Consume token, re-check arming + gates, set leverage, place order."""
        async with self._trade_lock:
            return await self._confirm_locked(token)

    async def _confirm_locked(self, token: str) -> dict[str, Any]:
        if not self.settings.trading_enabled:
            # Consume token so it cannot be reused after arming without re-preview
            try:
                self.store.consume(token)
            except TokenError:
                pass
            raise OrderError(
                "DISARMED: TRADING_ENABLED=false — confirm blocked. "
                "Set TRADING_ENABLED=true in .env to arm live trading."
            )

        try:
            payload = self.store.consume(token)
        except TokenError as e:
            raise OrderError(str(e)) from e

        token_hash = PreviewStore.hash_token(token)
        if self.db is not None:
            await self.db.mark_preview_used(token_hash)

        try:
            ticket = OrderTicket.model_validate(payload["ticket"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderError("preview payload has invalid ticket") from exc
        symbol = ticket.symbol.upper().strip()
        external_oid = payload.get("external_oid")
        if not isinstance(external_oid, str) or not _PREVIEW_EXTERNAL_OID_RE.fullmatch(
            external_oid
        ):
            raise OrderError("preview payload has no valid bound external_oid")
        preview_last = payload.get("last_price")
        if preview_last is not None:
            try:
                preview_last = float(preview_last)
            except (TypeError, ValueError, OverflowError) as exc:
                raise OrderError("preview payload has invalid last_price") from exc
            if not math.isfinite(preview_last) or preview_last <= 0:
                raise OrderError("preview payload has invalid last_price")

        # Contract meta, ticker, combined account state and the MEXC pending-entry
        # guard run concurrently. The account positions are reused for pre_hold /
        # positionId, while the pending read closes the exposure gap left by a
        # resting entry that is not yet represented in positions.
        contract_r, ticker_r, account_r, pending_r = await asyncio.gather(
            self.client.contract_meta(symbol),
            self.client.ticker(symbol),
            self._read_account_state(symbol),
            self._ensure_no_pending_same_side_entry(symbol, ticket.side),
            return_exceptions=True,
        )

        if isinstance(contract_r, BaseException):
            if isinstance(contract_r, ExchangeError):
                raise OrderError(f"contract meta failed: {contract_r}") from contract_r
            raise contract_r
        contract = contract_r

        last_price: float | None = None
        if isinstance(ticker_r, BaseException):
            if isinstance(ticker_r, ExchangeError):
                raise OrderError(f"ticker failed on confirm: {ticker_r}") from ticker_r
            raise ticker_r
        last_price = _coerce_float(ticker_r.last_price)
        if last_price is None or last_price <= 0:
            raise OrderError("ticker price unavailable or invalid on confirm")

        if isinstance(account_r, BaseException):
            if isinstance(account_r, ExchangeError):
                raise OrderError(f"equity unavailable: {account_r}") from account_r
            raise account_r
        assets, account_positions = account_r
        equity, available = self._map_balances(assets)
        existing, existing_warnings = self._map_existing_risk(
            account_positions, symbol, ticket.side, contract.contract_size
        )
        if isinstance(pending_r, BaseException):
            raise pending_r

        gate = validate_order(
            ticket,
            contract,
            equity,
            self.settings,
            last_price=last_price,
            for_confirm=True,
            existing_same_side_risk_usdt=existing,
            existing_same_side_warnings=existing_warnings,
            available_usdt=available,
            preview_last_price=preview_last,
        )
        if not gate.ok:
            raise OrderError(
                "order failed risk gates on confirm",
                errors=gate.errors,
            )

        so_errs = scale_out_errors(ticket, gate.entry_for_risk, self.client)
        if so_errs:
            raise OrderError("scale-out geometry invalid", errors=so_errs)

        # Manual mode: the trader manages the exit; NO exchange SL/TP trigger is
        # attached. The SL value is still required (risk gates), but it is not
        # sent to the exchange, and the SL-verify / auto-flatten paths below are
        # deliberately skipped for this order only.
        # The gate above accepts only known literals after case normalization;
        # mirror that exact decision without inventing a default.
        manual_sltp = ticket.trigger_mode.lower() == "manual"

        # Fail-closed option: manual mode places NO exchange stop, so it bypasses
        # the exchange-side protection even when ALLOW_UNPROTECTED_ENTRY=false.
        # When ALLOW_MANUAL_TRIGGER=false, refuse manual orders entirely.
        # (R-04: the gate above already blocks this in preview + here on
        # confirm; this is defense-in-depth, kept in sync — same fail-closed
        # getattr default.)
        if manual_sltp and not getattr(self.settings, "allow_manual_trigger", False):
            raise OrderError(
                "manual trigger_mode blocked (ALLOW_MANUAL_TRIGGER=false) — "
                "manual mode places no exchange SL/TP; use auto mode or enable "
                "ALLOW_MANUAL_TRIGGER to permit it."
            )

        sl = gate.rounded_stop if gate.rounded_stop is not None else ticket.stop_loss
        tp = gate.rounded_tp if gate.rounded_tp is not None else ticket.take_profit
        # For a market order the gate validates geometry, RRR and risk against
        # the WORST fill admitted by MARKET_ENTRY_SLIPPAGE_PCT. Thus a tight-stop
        # order cannot pass merely because the pre-submit ticker was safer than
        # the exchange-side market cap.
        # SL value is mandatory for the risk gate in BOTH modes (it sizes risk),
        # unless unprotected entries are explicitly allowed.
        if (sl is None or float(sl) <= 0) and not self.settings.allow_unprotected_entry:
            raise OrderError(
                "stop_loss required (ALLOW_UNPROTECTED_ENTRY=false); "
                "cannot size risk without a stop"
            )

        try:
            body = ticket_to_mexc_body(
                ticket,
                rounded_vol=gate.rounded_vol,
                rounded_price=gate.rounded_price,
                external_oid=external_oid,
                stop_loss=sl,
                take_profit=tp,
                attach_triggers=not manual_sltp,
            )
        except OrderError:
            raise

        # Auto mode still requires the SL to be attachable on the body.
        if (
            not manual_sltp
            and not self.settings.allow_unprotected_entry
            and "stopLossPrice" not in body
        ):
            raise OrderError(
                "SL not attachable on create body — blocked "
                "(ALLOW_UNPROTECTED_ENTRY=false)"
            )

        # Set leverage — HARD fail (do not place with unknown leverage)
        open_type = int(ticket.open_type or 1)
        position_type = 1 if ticket.side == "long" else 2
        # F-1: MEXC change_leverage REQUIRES positionId when a same-side position
        # is already open — without it every add-on onto an open MEXC position is
        # hard-blocked here ("set_leverage failed"). Resolve the existing
        # positionId and forward it; with no open position we keep the prior
        # symbol/openType/positionType form.
        #
        # Chosen over the alternative ("skip set_leverage on MEXC entirely,
        # leverage already rides in the create body"): the create body's leverage
        # DOES apply for both isolated/cross (openType is in the body too), so
        # skipping would be safe for the FRESH-position case — but keeping the
        # explicit set preserves the existing "never place with unverified
        # leverage" hard-fail guarantee and, for an add-on, still lets MEXC
        # confirm/adjust the position's leverage. It is also the smaller change.
        # Trade-off: if the add-on requests a DIFFERENT leverage than the open
        # position, MEXC may reject the change_leverage(positionId) call — but
        # that is a genuine, surfaced leverage conflict, not the old blanket
        # "positionId required" block on every add-on.
        #
        # The positionId AND pre_hold come from the SAME positions snapshot the
        # gate already read at the top of confirm (Finding 2, MEXC): it is passed
        # straight into the helper, so NO second positions fetch runs here. This
        # also makes pre_hold and the aggregate-risk gate share one consistent
        # snapshot; the trade-off is that pre_hold is now read at gate time rather
        # than immediately before set_leverage, but a failed-place is reconciled
        # against a FRESH live hold below, so under-/over-flatten is still caught.
        # HL keeps its later pre-send positions read. It must complete before
        # set_leverage/place_order: without a reliable pre-hold, a failed SL
        # cannot be safely attributed or differentially flattened.
        position_id: int | None = None
        pre_hold_precomputed: tuple[float, int, bool] | None = None
        if getattr(self.client, "exchange_id", "") == "mexc":
            pre_hold_precomputed, position_id = (
                await self._mexc_pre_hold_and_position_id(
                    symbol, ticket.side, positions=account_positions
                )
            )
            pre_hold_value, pre_open_type, pre_hold_checked = pre_hold_precomputed
            if not pre_hold_checked or (
                pre_hold_value > 0 and pre_open_type not in (1, 2)
            ):
                raise OrderError(
                    "MEXC position response is ambiguous or invalid — order blocked"
                )
        else:
            pre_hold_precomputed = await self._same_side_hold_vol_ok(
                symbol, ticket.side, fresh=True
            )

        pre_hold, _, pre_hold_ok = pre_hold_precomputed
        if not pre_hold_ok:
            raise OrderError(
                "pre-trade position snapshot is unavailable or ambiguous — "
                "order blocked"
            )
        try:
            await self.client.set_leverage(
                symbol,
                int(ticket.leverage),
                open_type,
                position_type=position_type,
                position_id=position_id,
            )
        except ExchangeError as e:
            raise OrderError(f"set_leverage failed — order blocked: {e}") from e

        # Hold before place — used so auto-flatten never closes pre-existing size.
        # pre_hold_ok records whether the query was RELIABLE; a failed lookup
        # must fail-closed (no differential-flatten) rather than assume 0.
        # On MEXC this reuses the read taken above (no duplicate positions call).

        recovered_from_timeout = False
        transport_err: str | None = None
        try:
            resp = await self.client.place_order(body)
        except ExchangeError as e:
            # Timeout / network / uncertain-response: try recover by externalOid
            # before declaring failure. An unparseable 2xx body (F-04) is just as
            # uncertain as a timeout — the order may already be live — so it must
            # go through the same reconciliation path, not a hard failure.
            recovered = None
            if _is_uncertain_order_error(e):
                try:
                    recovered = await self.client.order_by_external_oid(
                        symbol, external_oid
                    )
                except ExchangeError:
                    recovered = None
                # O-05: trust the exchange client's MATCH MARKER as the recovery
                # signal (MEXC "direct"/"history"/"open"; HL "cloid") — the client
                # sets it only after matching OUR oid/cloid and echoes that ID in
                # the normalized wrapper. Absent a marker, fall back to an exact
                # ID plus concrete order-ID check;
                # a falsy result ({} / None) stays fail-closed (hard error below).
                if not _recovery_is_match(recovered, external_oid):
                    recovered = None
            if not recovered:
                audit_error = await self._audit_order_best_effort(
                    symbol=symbol,
                    side=ticket.side,
                    request_json=_audit_order_request(body, ticket),
                    response_json={
                        "error": getattr(e, "raw", None),
                        "recovery": None,
                    },
                    status="error",
                    error=str(e),
                )
                audit_suffix = f" ({audit_error})" if audit_error else ""
                raise OrderError(
                    f"place_order failed: {e}. "
                    f"If timeout, check the exchange for externalOid={external_oid} "
                    f"before retry (do not blind re-preview).{audit_suffix}"
                ) from e
            # Order is (likely) live — fall through to SL verify/flatten (not early-return)
            recovered_from_timeout = True
            transport_err = str(e)
            resp = (
                recovered
                if isinstance(recovered, dict)
                else {"data": recovered, "recovery": recovered}
            )

        # ── Post-placement: the order IS live from here on. ────────────
        # Nothing below may raise out of confirm() — a crash here would
        # report an error for an order that already exists on the exchange
        # (and could bait the user into placing it again).
        warnings: list[str] = list(gate.warnings)
        if recovered_from_timeout:
            warnings.append(
                "place_order transport error but order recovered "
                f"(order found by externalOid={external_oid}). DO NOT re-preview — "
                f"verify on the exchange. Detail: {transport_err}"
            )
        sl_verified: bool = True
        sl_checked: bool = True
        # M1: distinct from sl_verified. sl_verified means "the SL trigger we
        # placed was accepted"; sl_fully_verified means "that SL covers the WHOLE
        # intended position size". They diverge for a partially-filled resting
        # GTC limit: the HL adapter sizes the reduce-only SL to the ACTUAL fill
        # (F-02), so a resting remainder that fills later is unprotected even
        # though the placed SL is real.
        sl_fully_verified: bool = True
        sl_detail = "no SL required"
        flatten_result: dict[str, Any] | None = None
        post_errors: list[str] = []

        if manual_sltp:
            # Manual mode: no exchange trigger by design → do not verify or
            # auto-flatten. The trader was warned and confirmed they manage it.
            sl_detail = "manual mode — no exchange SL/TP (trader manages exit)"
            warnings.append(
                "MANUAL: No exchange-side SL/TP was placed — you must close the "
                "position yourself. It is unprotected when the browser is closed."
            )
            # F-F1: scale_out places NO exchange triggers in manual mode either
            # (attach_triggers=False covers TP1/TP2 too) — surface that the
            # configured TP ladder was silently skipped, not just the SL.
            if getattr(ticket, "scale_out", False):
                warnings.append(
                    "SCALE-OUT IGNORED: trigger_mode=manual places no exchange "
                    "triggers, so the TP ladder (TP1/TP2) was NOT placed. "
                    "You must manage partial exits yourself."
                )

        # A non-marketable LIMIT entry that Hyperliquid RESTS carries no filled
        # position to protect: the HL adapter places NO SL by design (F-02) and
        # returns unfilled=True / entryFilledSz=0. That is NOT an SL failure — we
        # must not "verify" a stop that cannot exist yet, and must not
        # auto-flatten/cancel a perfectly valid resting order. (Regression fix:
        # before F-02 the SL trigger rested unconditionally with an oid, so this
        # path never mislabelled a resting limit as "SL nicht verifiziert".)
        unfilled_resting = isinstance(resp, dict) and resp.get("unfilled") is True

        # ── MEXC fill evidence (O-01 positive verify + O-02 resting) ──────────
        # MEXC never returns an slTriggerOid and the SL is position-bound in the
        # create body, so `open_stop_orders` is [] on every normal trade (→
        # _verify_sl_attached now yields UNKNOWN there, not MISSING). The ONLY
        # reliable positive signal is whether the entry actually FILLED: MEXC
        # accepts the create body with stopLossPrice atomically (fill ⟹ SL
        # accepted). We derive the fill from the response, else from the hold
        # delta against the reliable pre-trade quantity.
        is_mexc = getattr(self.client, "exchange_id", "") == "mexc"
        entry_order_type = (getattr(ticket, "order_type", "") or "").lower()
        mexc_new_fill: float | None = None
        mexc_fill_known = False
        # Provenance of the fill evidence. `reported` comes from the order
        # response and is bot-safe (counts ONLY this order's volume). The hold
        # delta is NOT: a concurrent same-side position increase by another bot
        # can inflate it, so hold-delta evidence needs the attribution guard in
        # the positive-verify block below before it may fully verify.
        mexc_fill_from_report = False
        if is_mexc and not manual_sltp and not unfilled_resting:
            reported = _extract_filled_vol(resp)
            fill_eps = max(float(gate.rounded_vol) * 1e-4, 1e-9)
            if reported is not None:
                mexc_new_fill = reported
                mexc_fill_known = True
                mexc_fill_from_report = True
            elif entry_order_type == "market" and pre_hold_ok:
                # X2-02: a fast MARKET fill can beat a slow positions endpoint;
                # a single-shot read then misreports it as resting. Retry with
                # the SL-verify settle cadence before trusting delta≈0.
                mexc_new_fill, mexc_fill_known = await self._mexc_market_fill_delta(
                    symbol, ticket.side, pre_hold, fill_eps
                )
            elif pre_hold_ok:
                # LIMIT without reported fill: a single hold read still CLASSIFIES
                # a resting entry (delta≈0), but this hold-delta must NEVER become
                # positive fill evidence for a limit (X2-01) — an external same-side
                # bump could otherwise verify a genuinely unprotected resting order.
                # Positive verify for a limit uses order-own evidence
                # (order_by_external_oid) in the verify block below.
                hold_now, _mot, hold_ok = await self._same_side_hold_vol_ok(
                    symbol, ticket.side
                )
                if hold_ok:
                    mexc_new_fill = max(0.0, hold_now - pre_hold)
                    mexc_fill_known = True
            if (
                mexc_fill_known
                and mexc_new_fill is not None
                and mexc_new_fill <= fill_eps
            ):
                # O-02: the entry rests unfilled — no position to protect yet.
                # Exempt from flatten/cancel exactly like the HL resting branch.
                unfilled_resting = True

        if unfilled_resting and not manual_sltp:
            sl_detail = "resting limit entry not filled — no SL until it fills"
            warnings.append(
                "RESTING LIMIT: Entry has not filled — there is NO exchange-side "
                "SL until the order fills (no fill watcher). Add protection after "
                "the fill or cancel the resting order."
            )

        try:
            if (
                not manual_sltp
                and not unfilled_resting
                and sl is not None
                and float(sl) > 0
                and not self.settings.allow_unprotected_entry
            ):
                # Trust only explicit exchange evidence: a real trigger-order id
                # from the adapter (HL) or a stop order found on the exchange.
                # Request echoes are never proof.
                sl_trigger_oid = (
                    resp.get("slTriggerOid") if isinstance(resp, dict) else None
                )
                trigger_errors = (
                    resp.get("triggerErrors") if isinstance(resp, dict) else None
                ) or []
                if sl_trigger_oid is not None:
                    sl_verified = True
                    sl_detail = f"exchange accepted SL trigger (oid={sl_trigger_oid})"
                else:
                    sl_verified, sl_detail, sl_checked = await self._verify_sl_attached(
                        symbol=symbol,
                        expected_sl=float(sl),
                        side=ticket.side,
                        pre_existing_same_side=bool(pre_hold_ok and pre_hold > 0),
                    )
                    if trigger_errors:
                        sl_detail += (
                            f"; trigger errors: {'; '.join(map(str, trigger_errors))}"
                        )

                # AUFLAGE — positive MEXC verify. The stop-order lookup is UNKNOWN
                # on a normal MEXC trade (position-bound SL, empty plan list). Do
                # NOT warn on every trade: a FILLED entry whose create body carried
                # stopLossPrice>0 is treated as protected because MEXC accepts the
                # body's stopLossPrice ATOMICALLY and geometry-validated (fill ⟹
                # SL accepted). A divergent explicit stop object seen elsewhere is
                # therefore treated as phantom, not authoritative, here. Requires
                # fill evidence — never mark a genuinely unconfirmable case as
                # verified. NOTE: unlike Hyperliquid (which auto-flattens a filled
                # entry whose reduce-only SL trigger is genuinely missing), MEXC by
                # design has no genuine-missing-SL detection for a filled entry
                # without body SL — it resolves to UNKNOWN (loud), never a blind
                # flatten. Intentional divergence.
                if is_mexc and not sl_verified:
                    body_had_sl = float(
                        (body.get("stopLossPrice") if isinstance(body, dict) else 0)
                        or 0
                    ) > 0
                    rounded_vol = float(gate.rounded_vol)
                    vol_eps = max(rounded_vol * 1e-4, 1e-9)
                    # ATTRIBUTION GUARD (money-critical). Evidence source decides:
                    #  • reported fill (order response): bot-safe (counts ONLY this
                    #    order) → needs only to be non-trivial.
                    #  • MARKET-IOC hold-delta: no resting remainder, so the delta
                    #    plausibly is OUR OWN fill when it reaches the ordered volume
                    #    (path UNCHANGED — the X2-02 retry only feeds it a settled read).
                    #  • LIMIT hold-delta: FORBIDDEN as positive evidence (X2-01). An
                    #    external same-side bump on a RESTING limit could otherwise
                    #    verify a genuinely unprotected order — the exact class this
                    #    verify was built to catch. Use order-own evidence instead
                    #    (order_by_external_oid → dealVol/state); any ambiguity → False.
                    # When in doubt → keep UNKNOWN (loud), never silently verify.
                    fill_desc = ""
                    if mexc_fill_from_report:
                        fill_is_ours = (
                            mexc_new_fill is not None and mexc_new_fill > vol_eps
                        )
                        if mexc_new_fill is not None:
                            fill_desc = f"fill≈{mexc_new_fill:g}"
                    elif entry_order_type == "market":
                        fill_is_ours = (
                            mexc_new_fill is not None
                            and mexc_new_fill >= rounded_vol - vol_eps
                        )
                        if mexc_new_fill is not None:
                            fill_desc = f"fill≈{mexc_new_fill:g}"
                    else:
                        # LIMIT: order-own evidence only — never the hold delta.
                        fill_is_ours = await self._mexc_order_fill_confirmed(
                            symbol, external_oid, rounded_vol, vol_eps
                        )
                        fill_desc = "order fill confirmed by externalOid"
                    if body_had_sl and fill_is_ours:
                        sl_verified = True
                        sl_checked = True
                        sl_detail = (
                            "MEXC SL is position-bound — accepted atomically in "
                            "the create request and active for the filled entry "
                            f"({fill_desc}); no separate plan order is visible"
                        )
                        warnings.append(
                            "INFO: MEXC SL is position-bound (no separate plan "
                            "order) and active for the filled entry."
                        )

                if not sl_verified and not sl_checked:
                    # UNKNOWN is not the same as MISSING: never flatten blind,
                    # or a broken lookup endpoint closes every protected trade.
                    warnings.append(
                        f"SL STATUS UNKNOWN ({sl_detail}) — verification was not "
                        "possible. Check the position manually on the exchange."
                    )
                elif not sl_verified:
                    warnings.append(
                        f"CRITICAL: SL not verified after place ({sl_detail}). "
                        "Position may be unprotected."
                    )
        except Exception as e:  # noqa: BLE001 — order is live, must not bubble
            post_errors.append(f"post-place SL verify failed: {e}")
            sl_verified = False
            sl_checked = False
            warnings.append(
                "Post-placement SL verification failed — the order IS placed. "
                "Check the SL and position on the exchange now."
            )

        # M1: a partially-filled resting GTC limit is only protected up to the
        # ACTUAL fill. The HL adapter sizes the reduce-only SL to entryFilledSz
        # (F-02), so the resting remainder is UNPROTECTED if it fills later. We
        # do NOT watch the fill (no fill-watcher by design) and must NOT flatten
        # the already-protected filled portion — instead we report honestly:
        # the position is not fully SL-verified and the trader is warned to
        # manage the resting remainder. Market orders are IOC (no resting
        # remainder) and are unaffected.
        order_type = (getattr(ticket, "order_type", "") or "").lower()
        if (
            not manual_sltp
            and order_type == "limit"
            and sl is not None
            and float(sl) > 0
            and not self.settings.allow_unprotected_entry
            and isinstance(resp, dict)
        ):
            filled = resp.get("entryFilledSz")
            requested = float(gate.rounded_vol)
            eps = max(requested * 1e-4, 1e-9)
            if (
                filled is not None
                and float(filled) > 0
                and float(filled) + eps < requested
            ):
                sl_fully_verified = False
                warnings.append(
                    "PARTIALLY FILLED: Limit order filled only partially "
                    f"({float(filled):g} of {requested:g}). The exchange-side SL "
                    "covers ONLY the filled part; the resting remainder is "
                    "UNPROTECTED if it fills later. Monitor and protect the "
                    "remainder manually, or cancel the resting order."
                )

        # Flatten/cancel is separate so its failures never wipe SL flags.
        # Never auto-flatten a manual-mode order — that IS the point of manual.
        if (
            not manual_sltp
            and not unfilled_resting
            and sl is not None
            and float(sl) > 0
            and not self.settings.allow_unprotected_entry
            and not sl_verified
            and sl_checked
            and self.settings.auto_flatten_if_sl_unverified
        ):
            try:
                hold_now, pos_open_type, hold_now_ok = (
                    await self._same_side_hold_vol_ok(symbol, ticket.side)
                )
                # Determine OUR fill. Prefer the exchange's reported fill from
                # the order response — that is bot-safe: it counts only this
                # order's volume, so a concurrent external bot adding same-side
                # size in this window cannot inflate what we close.
                reported_fill = _extract_filled_vol(resp)
                new_fill: float | None
                fill_source = ""
                if reported_fill is not None:
                    new_fill = min(float(gate.rounded_vol), reported_fill)
                    fill_source = "order response"
                elif not pre_hold_ok:
                    # FAIL-CLOSED: no reported fill AND the pre-trade quantity
                    # was never reliably read. The hold difference would then be
                    # measured against a fake pre_hold=0, so a pre-existing OLD
                    # position looks like a fresh fill and could be market-closed
                    # up to our order size. Execute NEITHER close NOR
                    # differential-flatten — warn and leave it to manual review.
                    new_fill = None
                    flatten_result = {
                        "action": "skipped_pre_hold_unknown",
                        "error": "pre_hold unavailable and no reported fill",
                        "pre_hold_checked": False,
                    }
                    warnings.append(
                        "AUTO_FLATTEN skipped: pre-trade quantity is unknown "
                        "(position lookup failed) and the order response has no "
                        "fill quantity, so NOTHING will be closed. Check the "
                        "position and SL on the exchange NOW."
                    )
                elif not hold_now_ok:
                    # Defect A / FAIL-CLOSED: the POST-place hold read is
                    # UNRELIABLE (positions() query failed) and there is no
                    # reported fill. A dropped reliability flag would surface
                    # hold_now=0.0 → look "flat" → route to cancel-resting and
                    # falsely warn "no fill / cancelled resting", leaving a
                    # FILLED, unprotected position OPEN. Never treat an unreadable
                    # hold as flat: close NOTHING, cancel NOTHING, warn to check.
                    new_fill = None
                    flatten_result = {
                        "action": "skipped_post_hold_unknown",
                        "error": "post-place hold unreadable and no reported fill",
                        "pre_hold_checked": True,
                        "post_hold_checked": False,
                    }
                    warnings.append(
                        "AUTO_FLATTEN skipped: post-trade quantity is unavailable "
                        "(position lookup failed) and the order response has no "
                        "fill quantity, so NOTHING will be closed or cancelled as "
                        "'unfilled'. Check the position and SL on the exchange NOW."
                    )
                else:
                    # FALLBACK ONLY (response carried no fill field): the hold
                    # difference. This can include an external bot's same-side
                    # fills from the same window; the min(rounded_vol, …) cap
                    # below still guarantees we never close MORE than our own
                    # order volume, but it may over-attribute a partial fill.
                    new_fill = max(0.0, hold_now - pre_hold)
                    fill_source = "hold difference (fallback)"
                if new_fill is None:
                    pass  # fail-closed above — no close/cancel action taken
                elif new_fill <= 1e-12:
                    # Unfilled resting entry: cancel it, do not close old pos
                    cancel_ids: list[Any] = []
                    if isinstance(resp, dict):
                        for k in ("orderId", "order_id", "oid"):
                            if resp.get(k) is not None:
                                cancel_ids.append(resp.get(k))
                                break
                    is_hl = (
                        getattr(self.client, "exchange_id", "") == "hyperliquid"
                    )
                    cancelled: list[Any] = []
                    cancel_errors: list[str] = []
                    for coid in cancel_ids:
                        try:
                            if is_hl:
                                cancel_response = await self.client.cancel_order(
                                    [{"orderId": coid, "symbol": symbol}]
                                )
                            else:
                                cancel_response = await self.client.cancel_order([coid])
                            cancel_error = _close_response_error(cancel_response)
                            if cancel_error:
                                raise OrderError(cancel_error)
                            cancelled.append(coid)
                        except Exception as ce:  # noqa: BLE001
                            cancel_errors.append(f"{coid}: {ce}")
                    flatten_result = {
                        "action": "cancel_resting",
                        "cancelled": cancelled,
                        "cancel_errors": cancel_errors,
                        "new_fill": 0.0,
                        "pre_hold": pre_hold,
                    }
                    if cancelled:
                        warnings.append(
                            "AUTO_FLATTEN: no fill yet — cancelled resting "
                            f"entry order(s) {cancelled} (pre-existing "
                            f"hold {pre_hold} left untouched)"
                        )
                    elif cancel_errors:
                        warnings.append(
                            "AUTO_FLATTEN: unfilled entry order could NOT be "
                            "cancelled ("
                            + "; ".join(cancel_errors)
                            + ") — it may remain resting and fill later without "
                            "protection. Check the order on the exchange NOW."
                        )
                    else:
                        warnings.append(
                            "AUTO_FLATTEN: no fill and no orderId to cancel "
                            "— check exchange for resting entry"
                        )
                else:
                    vol = min(float(gate.rounded_vol), new_fill)
                    # X2-06: pass the entry externalOid so the O-08 close-cloid
                    # recovery is wired — a timeout on this flatten can be
                    # recovered via order_by_external_oid("close:"+oid).
                    flatten_recovery_warning: str | None = None
                    try:
                        flatten_result = await self.client.close_position_market(
                            symbol,
                            side=ticket.side,
                            vol=vol,
                            open_type=pos_open_type or open_type,
                            external_oid=external_oid,
                        )
                    except ExchangeError as fe:
                        recovery_oid = f"close:{external_oid}"
                        recovered = None
                        if _is_uncertain_order_error(fe):
                            try:
                                recovered = await self.client.order_by_external_oid(
                                    symbol, recovery_oid
                                )
                            except ExchangeError:
                                recovered = None
                        if not _recovery_is_match(recovered, recovery_oid):
                            raise
                        flatten_result = recovered
                        flatten_recovery_warning = (
                            "AUTO_FLATTEN transport response was uncertain, but "
                            "the close order was recovered using externalOid="
                            f"{recovery_oid}. Do not close again; the live "
                            "position will be verified."
                        )
                    warnings.append(
                        f"AUTO_FLATTEN: market close vol={vol} "
                        f"(new_fill={new_fill} via {fill_source}, "
                        f"pre_hold={pre_hold}) after unverified SL"
                    )
                    if flatten_recovery_warning:
                        warnings.append(flatten_recovery_warning)
                    # F-03 follow-up: verify the flatten actually reduced the
                    # position — a partial IOC fill leaves residual OPEN and
                    # unprotected. Best-effort; never claim a clean flatten on
                    # a residual, never overwrite the send result on doubt.
                    try:
                        resid, _r_ot, resid_ok = await self._same_side_hold_vol_ok(
                            symbol, ticket.side
                        )
                        # Expected residual after flatten is the untouched
                        # pre-existing hold; anything meaningfully above it means
                        # our flatten under-filled.
                        eps = max(float(gate.rounded_vol) * 1e-4, 1e-9)
                        if not resid_ok:
                            warnings.append(
                                "AUTO_FLATTEN: residual position could not be "
                                "verified (position lookup failed). Check it on "
                                "the exchange; the close may have filled partially."
                            )
                            if isinstance(flatten_result, dict):
                                flatten_result = {
                                    **flatten_result,
                                    "residual_vol": None,
                                    "unverified": True,
                                }
                        elif resid - pre_hold > eps:
                            warnings.append(
                                f"AUTO_FLATTEN INCOMPLETE: residual {resid} remains "
                                f"(expected ~{pre_hold}). Close filled partially; "
                                "verify or close the position manually."
                            )
                            if isinstance(flatten_result, dict):
                                flatten_result = {
                                    **flatten_result,
                                    "residual_vol": resid,
                                    "incomplete": True,
                                }
                        elif pre_hold - resid > eps:
                            warnings.append(
                                f"AUTO_FLATTEN OVERFILLED: residual {resid} is "
                                f"below the untouched pre-existing hold "
                                f"~{pre_hold}. More than the new fill was closed; "
                                "verify the remaining position and exposure on "
                                "the exchange NOW."
                            )
                            if isinstance(flatten_result, dict):
                                flatten_result = {
                                    **flatten_result,
                                    "residual_vol": resid,
                                    "overfilled": True,
                                }
                    except Exception:  # noqa: BLE001 — order live, must not bubble
                        warnings.append(
                            "AUTO_FLATTEN: residual verification failed — check "
                            "the position on the exchange."
                        )
                        if isinstance(flatten_result, dict):
                            flatten_result = {
                                **flatten_result,
                                "residual_vol": None,
                                "unverified": True,
                            }
            except Exception as fe:  # noqa: BLE001
                warnings.append(
                    f"AUTO_FLATTEN failed: {fe} — close manually on the exchange"
                )
                flatten_result = {"error": str(fe)}

        status = "recovered_placed" if recovered_from_timeout else "placed"
        if manual_sltp:
            status = "placed_manual"
        elif unfilled_resting:
            status = (
                "recovered_placed_unfilled_resting"
                if recovered_from_timeout
                else "placed_unfilled_resting"
            )
        elif sl is not None and float(sl) > 0 and not sl_verified:
            if not sl_checked:
                status = (
                    "recovered_placed_sl_unknown"
                    if recovered_from_timeout
                    else "placed_sl_unknown"
                )
            else:
                status = (
                    "recovered_placed_sl_unverified"
                    if recovered_from_timeout
                    else "placed_sl_unverified"
                )
                if flatten_result and not (
                    isinstance(flatten_result, dict) and flatten_result.get("error")
                ):
                    status = status + "_flatten_sent"
        # M1: mark a partially-filled limit whose placed SL only covers the fill.
        # (sl_verified is True here — the trigger IS real — so the block above is
        # skipped; append the honest partial marker so the status is not a clean
        # "placed".)
        if sl_verified and not sl_fully_verified:
            status = status + "_partial_fill"

        audit_error = await self._audit_order_best_effort(
            symbol=symbol,
            side=ticket.side,
            request_json=_audit_order_request(body, ticket),
            response_json={
                "place": resp if isinstance(resp, dict) else {"data": resp},
                "sl_verified": sl_verified,
                "sl_fully_verified": sl_fully_verified,
                "sl_checked": sl_checked,
                "sl_detail": sl_detail,
                "flatten": flatten_result,
                "post_errors": post_errors,
            },
            status=status,
            error=None if sl_verified else status,
        )
        if audit_error:
            post_errors.append(audit_error)

        return {
            "ok": True,
            "external_oid": external_oid,
            "request": body,
            "response": resp,
            "gate": gate.to_dict(),
            "sl_verified": sl_verified,
            "sl_fully_verified": sl_fully_verified,
            "sl_checked": sl_checked,
            "sl_detail": sl_detail,
            "flatten": flatten_result,
            "status": status,
            "warnings": warnings,
            "post_errors": post_errors,
        }

    async def close_position(
        self,
        *,
        symbol: str,
        side: str,
        vol: float | None = None,
        fraction: float | None = None,
    ) -> dict[str, Any]:
        """Market-close an open position (full or partial). Hard-gated on arming.

        fraction (0<f<=1): close that share of the CURRENT hold (server-side, so
        the amount is never based on a stale UI value). vol still supported.
        """
        async with self._trade_lock:
            return await self._close_position_locked(
                symbol=symbol, side=side, vol=vol, fraction=fraction
            )

    async def _close_position_locked(
        self,
        *,
        symbol: str,
        side: str,
        vol: float | None = None,
        fraction: float | None = None,
    ) -> dict[str, Any]:
        if not self.settings.trading_enabled:
            raise OrderError(
                "DISARMED: TRADING_ENABLED=false — close blocked. "
                "Set TRADING_ENABLED=true in .env to arm live trading."
            )
        symbol = symbol.upper().strip()
        side = (side or "").lower()
        if side not in ("long", "short"):
            raise OrderError("side must be 'long' or 'short'")
        if vol is not None and fraction is not None:
            raise OrderError("provide either vol or fraction, not both")
        vol = _validated_close_amount(vol, field="vol")
        fraction = _validated_close_amount(
            fraction, field="fraction", maximum=1.0
        )

        try:
            positions = await self.client.positions(symbol, fresh=True)
        except ExchangeError as e:
            raise OrderError(f"positions lookup failed: {e}") from e
        if not isinstance(positions, list) or any(
            not isinstance(raw, dict) for raw in positions
        ):
            raise OrderError("position response is invalid — close blocked")

        # Exact symbol match; base-coin fallback ONLY on Hyperliquid (bare
        # coins) — on MEXC BTC_USDT and BTC_USDC must never match each other.
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        hold = 0.0
        open_type = 1
        for raw in positions:
            p = map_position(raw) if "hold_vol" not in raw else raw
            sym_match = _symbols_match(
                p.get("symbol"), symbol, allow_bare_base_alias=is_hl
            )
            if sym_match and str(p.get("side") or "").lower() == side:
                parsed_hold = _coerce_float(p.get("hold_vol"))
                if parsed_hold is None or parsed_hold < 0:
                    raise OrderError(
                        "invalid hold_vol in live position response — close blocked"
                    )
                hold = parsed_hold
                ot = p.get("open_type")
                if ot in (2, "2", "cross"):
                    open_type = 2
                break
        if hold <= 0:
            raise OrderError(f"no open {side} position on {symbol}")
        verification_hold = hold

        # Fraction is applied to the CURRENT hold; vol is capped at hold.
        if fraction is not None:
            close_vol = hold * fraction
        elif vol is not None:
            close_vol = min(vol, hold)
        else:
            close_vol = hold

        # Round to the exchange lot step so a partial close is not rejected.
        contract_problem: str | None = None
        try:
            contract = await self.client.contract_meta(symbol)
            parsed_vol_unit = _coerce_float(contract.vol_unit)
            parsed_min_vol = _coerce_float(contract.min_vol)
        except ExchangeError:
            contract_known = False
            vol_unit = 0.0
            min_vol = 0.0
            contract_problem = "contract metadata unavailable"
        else:
            contract_known = (
                parsed_vol_unit is not None
                and parsed_vol_unit > 0
                and parsed_min_vol is not None
                and parsed_min_vol > 0
            )
            vol_unit = parsed_vol_unit or 0.0
            min_vol = parsed_min_vol or 0.0
            if not contract_known:
                contract_problem = "contract sizing metadata is invalid"
        # Never round a full close down (would leave dust); only partials.
        is_full = close_vol >= hold - 1e-12
        if not is_full and not contract_known:
            raise OrderError(
                f"partial close blocked: {contract_problem} — lot size "
                "and minimum amount are unknown; retry or close the full position"
            )
        if not is_full and vol_unit > 0:
            close_vol = round_down_to_unit(close_vol, vol_unit)
        if close_vol <= 0 or (not is_full and min_vol > 0 and close_vol < min_vol):
            raise OrderError(
                f"close amount {close_vol} below exchange minimum {min_vol} — "
                "choose a larger share or close the full position"
            )

        # O-09 TOCTOU: contract_meta() yields after the first positions read, so
        # the live side or size can change in that window. Re-read immediately
        # before sending. Hyperliquid also rechecks inside its adapter, but only
        # the service knows the requested fraction and can recompute it after a
        # position growth; MEXC needs this check to avoid closing the wrong side.
        live_hold, live_open_type, live_ok = await self._same_side_hold_vol_ok(
            symbol, side, fresh=True
        )
        if not live_ok:
            raise OrderError(
                f"close aborted: could not re-verify the live {side} position "
                f"on {symbol} before sending (positions lookup failed) — verify "
                "on the exchange before retrying"
            )
        if live_hold <= 0:
            raise OrderError(
                f"close aborted: the {side} position on {symbol} disappeared or "
                "flipped between check and send — refusing to close the wrong "
                "side"
            )
        if not is_hl and live_open_type not in (1, 2):
            raise OrderError(
                "close aborted: invalid open_type in live MEXC position response"
            )
        verification_hold = live_hold
        open_type = live_open_type
        # Re-apply the requested fraction to the current hold. Absolute/full
        # closes are capped so an externally shrunk position is never oversized.
        if fraction is not None:
            close_vol = live_hold * fraction
        else:
            close_vol = min(close_vol, live_hold)
        # Keep the (possibly reduced) partial lot-aligned; never round a full
        # close down into dust.
        is_full = close_vol >= live_hold - 1e-12
        if not is_full and not contract_known:
            raise OrderError(
                f"partial close blocked: {contract_problem} — lot "
                "size and minimum amount are unknown; retry or close the full "
                "position"
            )
        if not is_full and vol_unit > 0:
            close_vol = round_down_to_unit(close_vol, vol_unit)
        # After re-clamping, a partial can fall below the exchange minimum. Reject
        # it here rather than shipping a sub-minimum order to the exchange.
        if close_vol <= 0 or (not is_full and min_vol > 0 and close_vol < min_vol):
            raise OrderError(
                f"close aborted: after re-checking the live {side} position on "
                f"{symbol} the closable amount {close_vol} is below the exchange "
                f"minimum {min_vol} — choose a larger share or close the full "
                "position"
            )

        # X2-06: deterministic close externalOid so the O-08 close-cloid recovery
        # is wired (the client namespaces it "close:"+oid) — a timeout during the
        # send can be recovered via order_by_external_oid instead of guessing.
        close_oid = f"mlt-close-{uuid.uuid4().hex[:20]}"
        close_recovery_warning: str | None = None
        try:
            resp = await self.client.close_position_market(
                symbol,
                side=side,
                vol=close_vol,
                open_type=open_type,
                external_oid=close_oid,
            )
        except ExchangeError as e:
            recovery_oid = f"close:{close_oid}"
            recovered = None
            if _is_uncertain_order_error(e):
                try:
                    recovered = await self.client.order_by_external_oid(
                        symbol, recovery_oid
                    )
                except ExchangeError:
                    recovered = None
            if _recovery_is_match(recovered, recovery_oid):
                resp = recovered
                close_recovery_warning = (
                    "Close transport response was uncertain, but the close order "
                    f"was recovered using externalOid={recovery_oid}. Do not close "
                    "again; the live position was verified afterward."
                )
            else:
                audit_error = await self._audit_order_best_effort(
                    symbol=symbol,
                    side=side,
                    request_json={"action": "manual_close", "vol": close_vol},
                    response_json=getattr(e, "raw", None),
                    status="close_error",
                    error=str(e),
                )
                audit_suffix = f" ({audit_error})" if audit_error else ""
                raise OrderError(f"close failed: {e}{audit_suffix}") from e

        # F-03: a transport-200 response can still carry an INNER rejection
        # (Hyperliquid nests errors inside statuses[]). Semantically check it —
        # otherwise we log `closed` / answer ok while the position is still open.
        close_err = _close_response_error(resp)
        if close_err:
            audit_error = await self._audit_order_best_effort(
                symbol=symbol,
                side=side,
                request_json={"action": "manual_close", "vol": close_vol},
                response_json=resp if isinstance(resp, dict) else {"data": resp},
                status="close_error",
                error=close_err,
            )
            audit_suffix = f" ({audit_error})" if audit_error else ""
            raise OrderError(
                f"close rejected by exchange: {close_err} — position may still be "
                f"open; verify on the exchange{audit_suffix}"
            )

        # F-03 FOLLOW-UP: the inner-error check above only proves the exchange
        # ACCEPTED the close — a marketable IOC can still PARTIALLY fill (a
        # `filled` is present, no inner error) and leave a residual position
        # open. Re-read the live position and confirm the residual is what we
        # intended to leave (0 for a full close, pre-send hold-close_vol for a
        # partial).
        expected_residual = max(0.0, verification_hold - close_vol)
        # Dust tolerance: one lot step, else a tiny relative epsilon (HL sizes
        # can be stepless). Never flags a genuine full close (residual ~0).
        epsilon = max(vol_unit, verification_hold * 1e-4, 1e-9)
        # M-A: a correct close can read back as still-open if the exchange
        # (Hyperliquid) has not reflected the fill the instant we re-read,
        # mislabelling a clean close as "partial". Re-read up to
        # CLOSE_VERIFY_ATTEMPTS times with a short settle delay, stopping as soon
        # as the residual has settled to what we intended to leave. Defaults to a
        # single attempt with no delay, so behaviour is unchanged unless the user
        # opts in via config.
        attempts = max(1, int(getattr(self.settings, "close_verify_attempts", 1) or 1))
        delay = max(0.0, float(getattr(self.settings, "close_verify_delay_s", 0.0) or 0.0))
        residual, _rot, reread_ok = await self._same_side_hold_vol_ok(
            symbol, side, fresh=True
        )
        for _ in range(attempts - 1):
            # Only keep polling while the position still looks unsettled; a good
            # read (settled residual) or a failed query ends the loop immediately.
            if not reread_ok or residual - expected_residual <= epsilon:
                break
            if delay > 0:
                await asyncio.sleep(delay)
            residual, _rot, reread_ok = await self._same_side_hold_vol_ok(
                symbol, side, fresh=True
            )

        if not reread_ok:
            # Fail-safe: the verification query failed. Do NOT claim fully
            # closed — surface that completion could not be confirmed.
            warn = (
                "Close sent, but position verification failed — status UNKNOWN. "
                "Check the position on the exchange NOW; the close may have "
                "filled partially."
            )
            audit_error = await self._audit_order_best_effort(
                symbol=symbol,
                side=side,
                request_json={"action": "manual_close", "vol": close_vol},
                response_json=resp if isinstance(resp, dict) else {"data": resp},
                status="close_unverified",
                error="post-close position reread failed",
            )
            result_warnings = (
                [close_recovery_warning, warn]
                if close_recovery_warning
                else [warn]
            )
            if audit_error:
                result_warnings.append(audit_error)
            return {
                "ok": False,
                "status": "close_unverified",
                "closed_vol": close_vol,
                "hold_vol": verification_hold,
                "residual_vol": None,
                "verified": False,
                "response": resp,
                "warnings": result_warnings,
            }

        if residual - expected_residual > epsilon:
            # PARTIAL fill: a meaningful residual beyond what we meant to leave
            # is still open. Do NOT report fully closed.
            warn = (
                f"PARTIAL FILL: Close for {close_vol} was sent, but {residual} "
                f"remains open (expected ~{expected_residual}). The position is "
                "NOT fully closed; verify or close the remainder manually."
            )
            audit_error = await self._audit_order_best_effort(
                symbol=symbol,
                side=side,
                request_json={"action": "manual_close", "vol": close_vol},
                response_json=resp if isinstance(resp, dict) else {"data": resp},
                status="close_incomplete",
                error=f"residual {residual} remains after close",
            )
            result_warnings = (
                [close_recovery_warning, warn]
                if close_recovery_warning
                else [warn]
            )
            if audit_error:
                result_warnings.append(audit_error)
            return {
                "ok": False,
                "status": "partial",
                "closed_vol": close_vol,
                "hold_vol": verification_hold,
                "residual_vol": residual,
                "verified": False,
                "response": resp,
                "warnings": result_warnings,
            }

        if expected_residual - residual > epsilon:
            warn = (
                f"CLOSE OVERFILLED: Close for {close_vol} was sent, but only "
                f"{residual} remains open (expected ~{expected_residual}). The "
                "position was reduced more than requested; verify the position "
                "and exposure on the exchange NOW."
            )
            audit_error = await self._audit_order_best_effort(
                symbol=symbol,
                side=side,
                request_json={"action": "manual_close", "vol": close_vol},
                response_json=resp if isinstance(resp, dict) else {"data": resp},
                status="close_overfilled",
                error=(
                    f"residual {residual} below expected {expected_residual} "
                    "after close"
                ),
            )
            result_warnings = (
                [close_recovery_warning, warn]
                if close_recovery_warning
                else [warn]
            )
            if audit_error:
                result_warnings.append(audit_error)
            return {
                "ok": False,
                "status": "overfilled",
                "closed_vol": close_vol,
                "hold_vol": verification_hold,
                "residual_vol": residual,
                "verified": False,
                "response": resp,
                "warnings": result_warnings,
            }

        audit_error = await self._audit_order_best_effort(
            symbol=symbol,
            side=side,
            request_json={"action": "manual_close", "vol": close_vol},
            response_json=resp if isinstance(resp, dict) else {"data": resp},
            status="closed",
            error=None,
        )
        result = {
            "ok": True,
            "status": "closed",
            "closed_vol": close_vol,
            "hold_vol": verification_hold,
            "residual_vol": residual,
            "verified": True,
            "response": resp,
        }
        result_warnings = []
        if close_recovery_warning:
            result_warnings.append(close_recovery_warning)
        if audit_error:
            result_warnings.append(audit_error)
        if result_warnings:
            result["warnings"] = result_warnings
        return result

    async def cancel(
        self,
        *,
        order_id: str | int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        async with self._trade_lock:
            return await self._cancel_locked(order_id=order_id, symbol=symbol)

    async def _cancel_locked(
        self,
        *,
        order_id: str | int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Cancel via official POST /api/v1/private/order/cancel.

        Fail-closed: require the id to appear in open orders (and symbol match
        when provided) so a stale/wrong id is not blindly sent to the exchange.
        """
        if order_id is None:
            raise OrderError("order_id required")
        order_id_text = str(order_id)
        if isinstance(order_id, bool) or not order_id_text.isdigit():
            raise OrderError("order_id must be a positive numeric ID")
        try:
            oid = int(order_id_text)
        except ValueError as exc:
            raise OrderError("order_id must be a positive numeric ID") from exc
        if oid <= 0:
            raise OrderError("order_id must be a positive numeric ID")

        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        if is_hl and not symbol:
            raise OrderError("symbol required for Hyperliquid cancel")

        try:
            open_rows = await self.client.open_orders(symbol)
        except ExchangeError as e:
            raise OrderError(
                f"open orders lookup failed — cancel blocked: {e}"
            ) from e
        if not isinstance(open_rows, list) or any(
            not isinstance(row, dict) for row in open_rows
        ):
            raise OrderError("open orders response is invalid — cancel blocked")

        matched = False
        for r in open_rows:
            id_keys = [
                key for key in ("orderId", "order_id", "oid") if key in r
            ]
            if not id_keys:
                continue
            if len(id_keys) > 1:
                if not _has_consistent_positive_order_id(r):
                    raise OrderError(
                        "open orders response contains an invalid order identity "
                        "— cancel blocked"
                    )
                rid = str(int(str(r.get(id_keys[0]))))
            else:
                rid = r.get(id_keys[0])
            if str(rid) != str(oid):
                continue
            if symbol and not _symbols_match(
                r.get("symbol"), symbol, allow_bare_base_alias=is_hl
            ):
                continue
            matched = True
            break
        if not matched:
            raise OrderError(
                f"order {order_id} not found in open orders"
                + (f" for {symbol}" if symbol else "")
                + " — cancel blocked"
            )

        try:
            if is_hl:
                resp = await self.client.cancel_order(
                    [{"orderId": oid, "symbol": symbol}]
                )
            else:
                resp = await self.client.cancel_order([oid])
        except ExchangeError as e:
            audit_error = await self._audit_order_best_effort(
                symbol=symbol or "",
                side=None,
                request_json={"orderId": order_id, "symbol": symbol},
                response_json=getattr(e, "raw", None),
                status="cancel_error",
                error=str(e),
            )
            audit_suffix = f" ({audit_error})" if audit_error else ""
            raise OrderError(f"cancel failed: {e}{audit_suffix}") from e

        # An outwardly successful Hyperliquid response can still carry an
        # inner statuses[].error. Treat all exchange-shaped rejections as a
        # failed cancel; never report a false success to the operator.
        if not isinstance(resp, (dict, list)) or not resp:
            response_error = "unrecognized cancel response"
        else:
            response_error = _close_response_error(resp)
        cancel_ok = response_error is None
        detail = resp

        audit_error = await self._audit_order_best_effort(
            symbol=symbol or "",
            side=None,
            request_json={"orderId": order_id, "symbol": symbol},
            response_json=resp if isinstance(resp, (dict, list)) else {"data": resp},
            status="cancelled" if cancel_ok else "cancel_partial_error",
            error=None if cancel_ok else response_error,
        )
        if not cancel_ok:
            audit_suffix = f" ({audit_error})" if audit_error else ""
            raise OrderError(
                f"cancel rejected by exchange: {response_error}{audit_suffix}"
            )
        result = {"ok": True, "response": detail}
        if audit_error:
            result["warnings"] = [audit_error]
        return result

    # ── Projekt H / Task 1: modify_stop_loss (money-critical) ────────────────

    async def _existing_sl_orders(
        self, symbol: str, side: str
    ) -> tuple[list[Any], float | None]:
        """OIDs of open SL-ish trigger orders + the MOST-protective resting SL.

        Returns ``(oids, most_protective_sl)``. ``most_protective_sl`` is derived
        from the SAME fetched stop list via the shared ``classify_protection``
        SSOT (C1: long → highest, short → lowest), so the modify guard cannot
        drift from the extractor/verifier. ``None`` when no SL rests or the
        lookup failed.

        Fail-open on lookup error: return ([], None) so modify still places a
        fresh stop (the old one, if any, simply stays — never unprotected).
        """
        try:
            stops = await self.client.open_stop_orders(symbol)
        except ExchangeError:
            return [], None
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        matching_stops: list[dict[str, Any]] = []
        for s in stops or []:
            if not isinstance(s, dict):
                continue
            if not _symbols_match(
                s.get("symbol"), symbol, allow_bare_base_alias=is_hl
            ):
                continue
            matching_stops.append(s)
        most_protective, _tp = classify_protection(matching_stops, side=side)
        out: list[Any] = []
        seen_oids: set[int] = set()
        for s in matching_stops:
            row_sl, _row_tp = classify_protection([s], side=side)
            if row_sl is None:
                continue  # only classified SL orders may be replaced
            oid = _consistent_stop_order_id(s)
            if oid is not None and oid not in seen_oids:
                out.append(oid)
                seen_oids.add(oid)
        return out, most_protective

    async def _verify_sl_oid(
        self,
        symbol: str,
        new_oid: Any,
        expected_sl: float,
        expected_vol: float,
        *,
        side: str,
    ) -> tuple[bool, str, bool]:
        """Confirm the concrete new_oid rests as the expected SL on the exchange.

        OID match is necessary but not sufficient. A price-only match is unsafe
        when the SL step is smaller than the price tolerance (~0.15%): the OLD
        stop alone could satisfy a price check, so we would cancel it while the
        NEW stop might not rest → unprotected. Gating on the concrete new_oid
        avoids this. The matched OID must also classify as SL, match its price,
        and cover both the placed size and a fresh position read immediately
        before the old stop is released.

        Retries because a freshly placed trigger reflects with a short delay.
        Returns (verified, detail, checked). `checked` is True iff the stop-order
        lookup succeeded at least once, so a broken endpoint never yields a false
        "gone" that would trip a cancel.
        """
        attempts = max(1, int(getattr(self.settings, "sl_verify_attempts", 3)))
        delay_s = max(0.0, float(getattr(self.settings, "sl_verify_delay_s", 0.7)))
        checked = False
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        expected_oid = _consistent_stop_order_id({"orderId": new_oid})
        last_detail = (
            f"new SL oid {new_oid} not found among open stop orders"
            if expected_oid is not None
            else "new SL has an invalid order identity"
        )
        for attempt in range(attempts):
            try:
                stops = await self.client.open_stop_orders(symbol)
                if not isinstance(stops, list) or not all(
                    isinstance(stop, dict) for stop in stops
                ):
                    last_detail = (
                        "open stop orders returned an invalid response shape "
                        f"({type(stops).__name__})"
                    )
                    stops = []
                else:
                    checked = True
            except ExchangeError as e:
                last_detail = str(e)
                stops = None
            for s in stops or []:
                if not _symbols_match(
                    s.get("symbol"), symbol, allow_bare_base_alias=is_hl
                ):
                    continue
                oid = _consistent_stop_order_id(s)
                if oid is None or oid != expected_oid:
                    continue
                trigger_price, _trigger_fields = _consistent_positive_price_aliases(
                    s, ("triggerPrice", "trigger_price")
                )
                price_ok = _sl_matches(expected_sl, trigger_price)
                row_sl, _row_tp = classify_protection([s], side=side)
                if row_sl is None:
                    last_detail = f"oid {new_oid} is not classified as a stop-loss"
                    continue
                if not price_ok:
                    last_detail = f"oid {new_oid} rests at an unexpected trigger price"
                    continue
                resting_vol = _coerce_float(s.get("vol"))
                coverage_epsilon = max(expected_vol * 1e-6, 1e-12)
                if resting_vol is None or resting_vol <= 0:
                    last_detail = f"oid {new_oid} has unknown stop coverage"
                    continue
                if resting_vol + coverage_epsilon < expected_vol:
                    last_detail = (
                        f"oid {new_oid} stop coverage {resting_vol} is below "
                        f"position size {expected_vol}"
                    )
                    continue
                current_hold, _open_type, current_hold_checked = (
                    await self._same_side_hold_vol_ok(symbol, side, fresh=True)
                )
                if not current_hold_checked:
                    last_detail = (
                        f"oid {new_oid} coverage against the current position "
                        "could not be verified"
                    )
                    continue
                if current_hold <= 0:
                    last_detail = (
                        f"oid {new_oid} coverage cannot be verified because the "
                        "current position is no longer open"
                    )
                    continue
                current_epsilon = max(current_hold * 1e-6, 1e-12)
                if resting_vol + current_epsilon < current_hold:
                    last_detail = (
                        f"oid {new_oid} stop coverage {resting_vol} is below "
                        f"current position size {current_hold}"
                    )
                    continue
                return (
                    True,
                    f"new SL oid {new_oid} resting (price and coverage matched)",
                    True,
                )
            if attempt < attempts - 1 and delay_s > 0:
                await asyncio.sleep(delay_s)
        return False, last_detail, checked

    async def _audit_modify(
        self, symbol, side, new_sl, response_json, status, error
    ) -> None:
        await self._audit_order_best_effort(
            symbol=symbol,
            side=side,
            request_json={"action": "modify_sl", "new_sl": new_sl},
            response_json=response_json
            if isinstance(response_json, (dict, list))
            else {"data": str(response_json)},
            status=status,
            error=error,
        )

    async def modify_stop_loss(
        self,
        *,
        symbol: str,
        side: str,
        new_sl: float,
        required_armed_rule: str | None = None,
    ) -> dict[str, Any]:
        async with self._trade_lock:
            return await self._modify_stop_loss_locked(
                symbol=symbol,
                side=side,
                new_sl=new_sl,
                required_armed_rule=required_armed_rule,
            )

    async def _modify_stop_loss_locked(
        self,
        *,
        symbol: str,
        side: str,
        new_sl: float,
        required_armed_rule: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.trading_enabled:
            raise OrderError(
                "DISARMED: TRADING_ENABLED=false — modify-SL blocked. "
                "Set TRADING_ENABLED=true in .env to arm live trading."
            )
        if not hasattr(self.client, "place_stop_order"):
            raise OrderError("Moving the SL is only available on Hyperliquid")
        symbol = symbol.upper().strip()
        side = (side or "").lower()
        if side not in ("long", "short"):
            raise OrderError("side must be 'long' or 'short'")
        if required_armed_rule is not None:
            if required_armed_rule not in {"auto_be", "auto_trail"}:
                raise OrderError("invalid required autonomous rule")
            if self.db is None:
                raise OrderError("autonomous rule state unavailable — modify-SL blocked")
            row = await self.db.get_open_position_mgmt(symbol, side)
            armed = (row or {}).get("armed_rules") or {}
            if armed.get(required_armed_rule) is not True:
                raise OrderError(
                    f"{required_armed_rule} is no longer armed — autonomous "
                    "modify-SL cancelled"
                )
        if isinstance(new_sl, bool):
            raise OrderError("new_sl must be numeric, not boolean")
        try:
            new_sl = float(new_sl)
        except (TypeError, ValueError, OverflowError) as exc:
            raise OrderError("new_sl must be numeric") from exc
        if not math.isfinite(new_sl):
            raise OrderError("new_sl must be finite")
        if new_sl <= 0:
            raise OrderError("new_sl must be > 0")

        # Position must exist (and give us the size for the reduce-only stop).
        # F-E1: distinguish a genuinely FLAT position from a positions-LOOKUP
        # FAILURE — both surface as hold<=0 here, but only the former means
        # "no open position". Reporting a failed lookup as "no open position"
        # risks the user assuming they are flat when the true state is unknown.
        # fresh=True: the new reduce-only stop is sized to `hold` below, so it
        # must reflect the LIVE position — a <=2s-stale hold that missed an
        # external same-side add would under-cover once the old stop is cancelled.
        hold, _open_type, hold_checked = await self._same_side_hold_vol_ok(
            symbol, side, fresh=True
        )
        if hold <= 0:
            if not hold_checked:
                raise OrderError(
                    f"positions lookup failed — could not verify {side} position "
                    f"on {symbol}. SL NOT modified. Retry, or verify on the "
                    "exchange before assuming you are flat."
                )
            raise OrderError(f"no open {side} position on {symbol}")

        # Mark price for side geometry.
        try:
            ticker = await self.client.ticker(symbol)
            mark = _coerce_float(ticker.last_price)
        except ExchangeError as e:
            raise OrderError(f"ticker failed — modify-SL blocked: {e}") from e
        if mark is None or not math.isfinite(mark) or mark <= 0:
            raise OrderError(
                "mark price unavailable — modify-SL blocked (cannot validate geometry)"
            )

        # Side geometry: long SL below mark, short SL above.
        if side == "long" and not (new_sl < mark):
            raise OrderError(f"long SL {new_sl} must be BELOW mark {mark}")
        if side == "short" and not (new_sl > mark):
            raise OrderError(f"short SL {new_sl} must be ABOVE mark {mark}")

        # Side-aware conservative tick rounding (no-op on HL price_unit=0; the
        # client re-rounds via round_hl_price on placement).
        try:
            contract = await self.client.contract_meta(symbol)
        except ExchangeError:
            price_unit = 0.0
        else:
            price_unit = _coerce_float(contract.price_unit)
            if price_unit is None or price_unit < 0:
                raise OrderError("contract price unit is invalid — modify-SL blocked")
        rounded_sl = (
            round_trigger_to_unit(new_sl, price_unit, side=side, kind="sl")
            if price_unit > 0
            else new_sl
        )
        if not math.isfinite(rounded_sl) or rounded_sl <= 0:
            raise OrderError("rounded SL is invalid — modify-SL blocked")
        # Rounding on coarse ticks could cross the mark — re-check.
        if side == "long" and not (rounded_sl < mark):
            raise OrderError(f"rounded long SL {rounded_sl} not below mark {mark}")
        if side == "short" and not (rounded_sl > mark):
            raise OrderError(f"rounded short SL {rounded_sl} not above mark {mark}")

        old_oids, existing_sl = await self._existing_sl_orders(symbol, side)

        # ── C1b defense-in-depth: NEVER LOOSEN existing protection ──
        # Even if a caller passes a bad new_sl, refuse a move that would make the
        # position LESS protected than the most-protective stop already resting
        # (long: new below existing; short: new above). Raised BEFORE placing or
        # cancelling anything, so the old, tighter stop stays live (never
        # unprotected). The legitimate TIGHTEN path is unaffected.
        if existing_sl is not None and existing_sl > 0:
            loosens = (side == "long" and rounded_sl < existing_sl) or (
                side == "short" and rounded_sl > existing_sl
            )
            if loosens:
                await self._audit_modify(
                    symbol, side, rounded_sl,
                    {"existing_sl": existing_sl, "requested_sl": rounded_sl},
                    "modify_sl_refused_loosen",
                    "would loosen existing protection",
                )
                raise OrderError(
                    f"modify-SL REFUSED — new SL {rounded_sl} would LOOSEN the "
                    f"existing most-protective stop {existing_sl} ({side}). "
                    "Old stop left in place (still protected); pass a more "
                    "protective SL to tighten."
                )

        # TOCTOU: ticker/contract/stop reads above yield after the first live
        # position snapshot. Re-read size and mark immediately before placing so
        # an external add cannot leave the replacement stop under-sized and a
        # crossed market cannot turn the requested SL geometry invalid.
        latest_hold, _latest_open_type, latest_hold_checked = (
            await self._same_side_hold_vol_ok(symbol, side, fresh=True)
        )
        if not latest_hold_checked:
            raise OrderError(
                "latest position read failed or returned invalid data — "
                "modify-SL blocked; old SL left in place"
            )
        if latest_hold <= 0:
            raise OrderError(
                f"the {side} position on {symbol} is no longer open — "
                "modify-SL blocked; old SL left in place"
            )
        hold = latest_hold

        try:
            latest_ticker = await self.client.ticker(symbol)
        except ExchangeError as e:
            raise OrderError(
                f"latest ticker failed — modify-SL blocked; old SL left in place: {e}"
            ) from e
        latest_mark = _coerce_float(latest_ticker.last_price)
        if latest_mark is None or latest_mark <= 0:
            raise OrderError(
                "latest mark unavailable — modify-SL blocked; old SL left in place"
            )
        if side == "long" and not (rounded_sl < latest_mark):
            raise OrderError(
                f"rounded long SL {rounded_sl} not below latest mark {latest_mark}"
            )
        if side == "short" and not (rounded_sl > latest_mark):
            raise OrderError(
                f"rounded short SL {rounded_sl} not above latest mark {latest_mark}"
            )

        # ── FAIL-SAFE STEP 1: place the NEW stop BEFORE removing the old one ──
        try:
            placed = await self.client.place_stop_order(
                symbol,
                position_side=side,
                vol=hold,
                trigger_px=rounded_sl,
                tpsl="sl",
                reduce_only=True,
            )
        except ExchangeError as e:
            await self._audit_modify(
                symbol, side, rounded_sl, {"error": str(e)},
                "modify_sl_place_failed", str(e),
            )
            raise OrderError(
                f"new SL placement failed — old SL left in place (still protected): {e}"
            ) from e

        new_oid = _consistent_stop_order_id(placed)
        place_err = placed.get("error") if isinstance(placed, dict) else "unknown"
        if new_oid is None or place_err is not None:
            rejection_detail = (
                str(place_err) if place_err is not None else "invalid response"
            )
            await self._audit_modify(
                symbol, side, rounded_sl, placed,
                "modify_sl_place_rejected", rejection_detail,
            )
            raise OrderError(
                "new SL rejected or returned an invalid response — old SL left "
                f"in place (still protected): {rejection_detail}"
            )

        # ── STEP 2: VERIFY the concrete new_oid is really resting (by OID, not
        # by price — the old, price-close stop must never count as the new). ──
        verified, detail, checked = await self._verify_sl_oid(
            symbol, new_oid, rounded_sl, hold, side=side
        )

        warnings: list[str] = []
        cancelled: list[Any] = []
        failed: list[Any] = []

        if not verified:
            # New stop unconfirmed. NEVER cancel the old one on doubt — keeping
            # both (or old only) is over-protected, never unprotected.
            warnings.append(
                f"New SL placed (oid={new_oid}) but NOT verified ({detail}); "
                "the old SL was NOT removed. Check both stops on the exchange."
            )
            status = (
                "modify_sl_unverified_old_kept"
                if checked
                else "modify_sl_unknown_old_kept"
            )
        else:
            # ── STEP 3: only NOW cancel the old stop(s). ──
            is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
            for oid in old_oids:
                if str(oid) == str(new_oid):
                    continue
                try:
                    if is_hl:
                        cancel_response = await self.client.cancel_order(
                            [{"orderId": oid, "symbol": symbol}]
                        )
                    else:
                        cancel_response = await self.client.cancel_order([oid])
                    cancel_error = _close_response_error(cancel_response)
                    if cancel_error:
                        raise OrderError(cancel_error)
                    cancelled.append(oid)
                except Exception as e:  # noqa: BLE001 — new stop is live; must not bubble
                    failed.append(oid)
                    warnings.append(
                        f"Old SL {oid} could not be cancelled ({e}) and remains "
                        "active. The position is OVER-protected (two stops), not "
                        "unprotected; remove the old stop manually on the exchange."
                    )
            status = "modify_sl_ok" if not failed else "modify_sl_ok_old_cancel_failed"

        await self._audit_modify(
            symbol, side, rounded_sl,
            {
                "new_oid": new_oid,
                "cancelled": cancelled,
                "failed": failed,
                "verified": verified,
                "detail": detail,
                "place": placed,
            },
            status,
            None if (verified and not failed) else status,
        )

        return {
            "ok": True,
            "symbol": symbol,
            "side": side,
            "new_sl": rounded_sl,
            "new_oid": new_oid,
            "cancelled_old": cancelled,
            "failed_cancel": failed,
            "verified": verified,
            "detail": detail,
            "status": status,
            "warnings": warnings,
        }


def _confirm_summary(
    ticket: OrderTicket,
    gate: GateResult,
    contract: ContractMeta,
    equity: float,
    last_price: float | None,
) -> dict[str, Any]:
    return {
        "symbol": ticket.symbol,
        "side": ticket.side,
        "order_type": ticket.order_type,
        "vol": gate.rounded_vol,
        "price": gate.rounded_price,
        "leverage": ticket.leverage,
        "entry_for_risk": gate.entry_for_risk,
        "stop_loss": gate.rounded_stop if gate.rounded_stop is not None else ticket.stop_loss,
        "take_profit": gate.rounded_tp if gate.rounded_tp is not None else ticket.take_profit,
        "risk_usdt": gate.risk_usdt,
        "risk_pct": gate.risk_pct,
        "rrr": gate.rrr,
        "notional_usdt": gate.notional_usdt,
        "equity_usdt": equity,
        "last_price": last_price,
        "contract_size": contract.contract_size,
        "api_allowed": contract.api_allowed,
        "open_type": ticket.open_type,
        # Bind the SL/TP mode to the preview so the confirm modal shows the
        # warning for what the token ACTUALLY does, not the live UI toggle.
        "trigger_mode": getattr(ticket, "trigger_mode", "auto"),
        "irreversible": True,
        "live": True,
    }


