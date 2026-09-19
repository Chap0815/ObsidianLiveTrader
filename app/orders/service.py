"""Order preview → confirm → cancel orchestration.

No place without unused, unexpired preview token AND TRADING_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from app.config import Settings
from app.hyperliquid.errors import HyperliquidError
from app.mexc.client import MexcClient, map_position, usdt_balances
from app.mexc.errors import MexcError
from app.models import ContractMeta, OrderTicket
from app.orders.protection import (
    classify_order_label_fields,
    classify_position_side_fields,
    classify_protection,
    classify_reduce_only_fields,
)
from app.orders.tokens import PreviewStore, TokenError
from app.risk.gates import GateResult, validate_order
from app.risk.sizing import round_down_to_unit, round_trigger_to_unit
from app.security import valid_normalized_position_symbol

ExchangeError = (MexcError, HyperliquidError)
log = logging.getLogger(__name__)

# MEXC futures order enums (official create docs / common practice):
# side: 1 open long, 2 close short, 3 open short, 4 close long
# type: 1 limit (price+vol), 2 post-only, 3 IOC, 4 FOK, 5 market
# openType: 1 isolated, 2 cross
MEXC_SIDE_OPEN_LONG = 1
MEXC_SIDE_OPEN_SHORT = 3
MEXC_TYPE_LIMIT = 1
MEXC_TYPE_MARKET = 5
_PREVIEW_EXTERNAL_OID_RE = re.compile(r"^mlt-[0-9a-f]{20}$")
_AUDIT_MAX_DEPTH = 8
_AUDIT_MAX_ITEMS = 100
_AUDIT_MAX_STRING = 2_000
_POSITION_IDENTITY_UNSET: Any = object()
_MAX_FILL_FUTURE_SKEW_MS = 5 * 60 * 1000
_AUDIT_SECRET_LABEL_RE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:token|secret|credential|cookie)[a-z0-9_-]*|"
    r"api[ _-]?key|signature|authorization|private[ _-]?key|password|"
    r"passphrase)\b(\s*[:=]\s*)([^\r\n,;}\]]+)"
)


def _identity_finite_real_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _hl_fill_matches_position_symbol(fill: dict[str, Any], symbol: str) -> bool:
    observed = fill.get("symbol")
    if not valid_normalized_position_symbol(observed, exchange="hyperliquid"):
        return False
    requested = symbol.strip().upper().replace("-", "_").split("_", 1)[0]
    return observed == requested


def hl_flat_open_epoch(
    fill: dict[str, Any], symbol: str, side: str
) -> int | None:
    """Return one exact, symbol-bound Flat-to-Open epoch from a normalized fill."""
    if not _hl_fill_matches_position_symbol(fill, symbol):
        return None
    direction = fill.get("dir")
    if not isinstance(direction, str) or direction.lower() != f"open {side}":
        return None
    start_position = fill.get("start_position")
    timestamp = fill.get("time")
    if isinstance(start_position, bool) or isinstance(timestamp, bool):
        return None
    try:
        start = float(start_position)
        epoch = int(timestamp or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(start) or start != 0.0 or epoch <= 0:
        return None
    return epoch


def _valid_normalized_hl_fill_evidence(fill: object, symbol: str) -> bool:
    if not isinstance(fill, dict) or not _hl_fill_matches_position_symbol(
        fill, symbol
    ):
        return False
    if not isinstance(fill.get("dir"), str):
        return False
    timestamp = fill.get("time")
    latest_fill_time = int(time.time() * 1000) + _MAX_FILL_FUTURE_SKEW_MS
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
        or timestamp > latest_fill_time
    ):
        return False
    start_position = fill.get("start_position")
    return start_position is None or _identity_finite_real_number(start_position)


async def safe_user_fills(
    client: Any, symbol: str
) -> list[dict[str, Any]] | None:
    """Return a fresh, fully validated Hyperliquid fill window or ``None``."""
    if not hasattr(client, "user_fills"):
        return None
    try:
        fills = await client.user_fills(symbol, fresh=True)
    except Exception:
        return None
    if not isinstance(fills, list) or not all(
        _valid_normalized_hl_fill_evidence(fill, symbol) for fill in fills
    ):
        return None
    return fills


def position_id_signature(pos: Any) -> int | None:
    """Return a positive stable snapshot position ID, if present."""
    if not isinstance(pos, dict):
        return None
    pid = pos.get("position_id")
    if isinstance(pid, bool):
        return None
    if isinstance(pid, int):
        return pid if pid > 0 else None
    if isinstance(pid, str):
        text = pid.strip()
        if text.isdigit():
            try:
                parsed = int(text)
            except ValueError:
                return None
            return parsed if parsed > 0 else None
    return None


def client_uses_hyperliquid_semantics(client: Any) -> bool:
    """Honor a declared venue before considering legacy adapter capability."""
    exchange_id = getattr(client, "exchange_id", None)
    if isinstance(exchange_id, str):
        return exchange_id == "hyperliquid"
    return hasattr(client, "place_stop_order")


async def hl_epoch_signature(
    client: Any,
    symbol: str,
    side: str,
    *,
    fills: Any = _POSITION_IDENTITY_UNSET,
) -> int | None:
    """Return the newest validated Hyperliquid Flat-to-Open fill timestamp."""
    if fills is _POSITION_IDENTITY_UNSET:
        fills = await safe_user_fills(client, symbol)
    if not isinstance(fills, list):
        return None
    epochs = [
        epoch
        for fill in fills
        if isinstance(fill, dict)
        and (epoch := hl_flat_open_epoch(fill, symbol, side)) is not None
    ]
    return max(epochs) if epochs else None


async def open_position_signature(
    client: Any,
    pos: Any,
    symbol: str,
    side: str,
    *,
    fills: Any = _POSITION_IDENTITY_UNSET,
) -> int | None:
    """Return the stable identity of the current position epoch, if provable."""
    if client_uses_hyperliquid_semantics(client):
        return await hl_epoch_signature(client, symbol, side, fills=fills)
    return position_id_signature(pos)


def _audit_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(
        marker in normalized
        for marker in (
            "apikey",
            "apisecret",
            "privatekey",
            "signature",
            "authorization",
            "token",
            "secret",
            "credential",
            "cookie",
            "password",
            "passphrase",
        )
    ) or normalized in {"body", "headers"}


def _audit_safe_value(
    value: Any,
    *,
    secret_values: tuple[str, ...],
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    """Return bounded JSON-safe audit data with credentials removed."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite]"
    if isinstance(value, str):
        text = value
        for secret in secret_values:
            text = text.replace(secret, "[redacted]")
        text = _AUDIT_SECRET_LABEL_RE.sub(
            lambda match: match.group(1) + match.group(2) + "[redacted]", text
        )
        if len(text) > _AUDIT_MAX_STRING:
            text = text[:_AUDIT_MAX_STRING] + "…[truncated]"
        return text
    if depth >= _AUDIT_MAX_DEPTH:
        return "[max-depth]"

    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "[cycle]"
    if isinstance(value, dict):
        seen.add(identity)
        try:
            result: dict[str, Any] = {}
            for index, (key, child) in enumerate(value.items()):
                if index >= _AUDIT_MAX_ITEMS:
                    result["__truncated__"] = True
                    break
                name = str(key)
                result[name] = (
                    "[redacted]"
                    if _audit_sensitive_key(name)
                    else _audit_safe_value(
                        child,
                        secret_values=secret_values,
                        depth=depth + 1,
                        seen=seen,
                    )
                )
            return result
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        seen.add(identity)
        try:
            result = [
                _audit_safe_value(
                    child,
                    secret_values=secret_values,
                    depth=depth + 1,
                    seen=seen,
                )
                for child in value[:_AUDIT_MAX_ITEMS]
            ]
            if len(value) > _AUDIT_MAX_ITEMS:
                result.append("[truncated]")
            return result
        finally:
            seen.remove(identity)
    return f"[{type(value).__name__}]"


class OrderError(Exception):
    """Business-level order flow error (gates, arming, token, policy)."""

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]


class OrderRejectedByExchange(OrderError):
    """Definite provider rejection whose diagnostic text is not API-safe."""


class OrderOutcomeUnknown(OrderError):
    """The exchange mutation may have completed despite a lost response.

    API callers must map this separately from an ordinary rejected request so
    clients reconcile exchange state and block blind retries.
    """


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


def _valid_normalized_position_side(value: object) -> bool:
    """Whether an adapter position side is already in canonical model form."""
    return isinstance(value, str) and value in ("long", "short")


def _normalized_position_open_type(value: object) -> int | None:
    """Map a canonical adapter margin mode to its exchange enum.

    ``0`` represents an absent mode; callers that require the mode already
    reject it.  Any present non-canonical value is invalid at this model
    boundary rather than being reinterpreted as a raw exchange enum.
    """
    if value is None:
        return 0
    if not isinstance(value, str):
        return None
    if value == "isolated":
        return 1
    if value == "cross":
        return 2
    return None


def _position_sl_price(p: dict[str, Any]) -> float | None:
    """Own stop-loss of an open position, if it carries one (R-01).

    Position rows from different adapters/lookups may expose the SL under
    different keys; take the first finite, positive value.
    """
    for key in ("stop_loss", "sl_price", "sl", "stopLossPrice", "stop_price"):
        v = _normalized_float(p.get(key))
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

    Per position, prefer the loss to its OWN stop-loss when it is on the
    protective side of entry (long: at/below entry, short: at/above entry).
    A wrong-side stop is not credited because its distance could understate an
    unprotected position's risk; use the full known liquidation distance
    instead. The liquidation price must also be below entry for a long or above
    entry for a short. Missing or contradictory liquidation data blocks: a
    percentage of entry notional is not a conservative upper bound for an
    unprotected position and could understate aggregate MAX_RISK_PCT.

    Returns ``(total_risk_usdt, warnings)``; the stable tuple shape is retained
    for callers, although the fail-closed calculation currently emits no
    fallback warnings.
    """
    if not isinstance(positions, list):
        raise ValueError("open position data is unavailable or invalid")
    contract_size_value = _normalized_float(contract_size)
    if contract_size_value is None or contract_size_value <= 0:
        raise ValueError("contract_size is unavailable or invalid")
    exchange = "hyperliquid" if allow_base_symbol_alias else "mexc"
    total = 0.0
    warnings: list[str] = []
    for raw in positions:
        if not isinstance(raw, dict):
            raise ValueError("open position row is invalid")
        p = raw if "hold_vol" in raw else map_position(raw)
        symbol_raw = p.get("symbol")
        if not valid_normalized_position_symbol(
            symbol_raw, exchange=exchange
        ):
            raise ValueError("open position has invalid symbol/side identity")
        symbol_matches = _symbols_match(
            symbol_raw,
            symbol,
            allow_bare_base_alias=allow_base_symbol_alias,
        )
        if not symbol_matches:
            continue
        side_raw = p.get("side")
        if not _valid_normalized_position_side(side_raw):
            raise ValueError("open position has invalid symbol/side identity")
        if side_raw != side:
            continue
        vol = _normalized_float(p.get("hold_vol"))
        if vol is None or vol <= 0:
            raise ValueError(f"open {side} position on {symbol} has invalid hold_vol")
        entry = _normalized_float(p.get("entry_price"))
        if entry is None or entry <= 0:
            raise ValueError(f"open {side} position on {symbol} has invalid entry_price")
        sl = _position_sl_price(p)
        sl_is_protective = sl is not None and (
            (side == "long" and sl <= entry) or (side == "short" and sl >= entry)
        )
        if sl_is_protective:
            # Loss to this position's own stop — the realistic exposure.
            dist = abs(entry - sl)
        else:
            liq = _normalized_float(p.get("liquidate_price"))
            if liq is None or liq <= 0:
                raise ValueError(
                    f"open {side} position on {symbol} has invalid or missing "
                    "liquidate_price — "
                    "cannot enforce aggregate MAX_RISK_PCT (close or wait for liq data)"
                )
            dist = entry - liq if side == "long" else liq - entry
            if dist == 0:
                raise ValueError(
                    f"open {side} position on {symbol} has invalid liquidate_price — "
                    "liquidation distance is zero"
                )
            if dist < 0:
                raise ValueError(
                    f"open {side} position on {symbol} has invalid liquidate_price — "
                    "liquidation price is on the wrong side of entry"
                )
        position_risk = dist * contract_size_value * vol
        if not math.isfinite(position_risk):
            raise ValueError(
                f"open {side} position on {symbol} calculated risk is non-finite"
            )
        next_total = total + position_risk
        if not math.isfinite(next_total):
            raise ValueError(
                f"open {side} positions on {symbol} aggregate risk is non-finite"
            )
        total = next_total
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


def _normalized_float(v: Any) -> float | None:
    """Return a finite number only from an already-normalized model field.

    Raw exchange payloads may legitimately encode numbers as strings and use
    ``_coerce_float``. Money-path model boundaries retain their typed contract
    so a malformed or replaced adapter cannot silently regain trust.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        value = float(v)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _validated_close_amount(
    value: Any, *, field: str, maximum: float | None = None
) -> float | None:
    """Normalize one explicit close amount without falling back to full close."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise OrderError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise OrderError(f"{field} must be finite")
    if parsed <= 0:
        raise OrderError(f"{field} must be > 0")
    if maximum is not None and parsed > maximum:
        raise OrderError(f"{field} must be <= {maximum:g}")
    return parsed


_FILL_VOLUME_ALIASES = (
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
)


def _consistent_nonnegative_aliases(
    row: Any, aliases: tuple[str, ...]
) -> tuple[float | None, bool]:
    """Return one finite non-negative value only when all present aliases agree."""
    if not isinstance(row, dict):
        return None, False
    values: list[float] = []
    found = False
    for key in aliases:
        if key not in row:
            continue
        found = True
        value = _coerce_float(row.get(key))
        if value is None or value < 0:
            return None, True
        values.append(value)
    if not found:
        return None, False
    if len(set(values)) != 1:
        return None, True
    return values[0], True


def _classify_trigger_errors(value: Any) -> tuple[bool, bool]:
    """Return (SL failure, TP/other failure) without exposing provider detail."""
    if value is None or value == []:
        return False, False
    if not isinstance(value, list):
        return True, True
    sl_failure = False
    other_failure = False
    for item in value:
        if not isinstance(item, str) or not item.strip():
            sl_failure = True
            other_failure = True
            continue
        label = item.strip().lower()
        if label.startswith("sl:"):
            sl_failure = True
        elif label.startswith(("tp:", "tp2:")):
            other_failure = True
        else:
            sl_failure = True
            other_failure = True
    return sl_failure, other_failure


def _reported_fill_vol(resp: Any) -> tuple[float | None, bool]:
    """Return (valid fill, fill evidence present) for THIS placed order.

    Keeping presence separate from validity prevents an invalid or contradictory
    exchange report from being treated as if no report existed. Only true
    absence may fall back to a position delta; malformed explicit evidence must
    leave the outcome unknown.

    A key that is present but 0 is trusted as "reported unfilled" (returns 0.0)
    — that is the fail-closed choice: we would rather cancel a resting entry
    than market-close volume that may not be ours. A negative report is invalid
    and returns None; it must never be normalized into zero-fill evidence.
    """
    if not isinstance(resp, dict):
        return None, False
    reported_values: list[float] = []
    evidence_present = False

    entry_fill_present = "entryFilledSz" in resp
    entry_fill = (
        _normalized_float(resp.get("entryFilledSz")) if entry_fill_present else None
    )
    if entry_fill_present and (entry_fill is None or entry_fill < 0):
        return None, True
    if entry_fill_present and entry_fill is not None:
        evidence_present = True
        reported_values.append(entry_fill)

    # Flat quantity keys seen across MEXC revisions / internal adapters. Every
    # present alias must be usable and agree; choosing the first one could turn
    # a contradictory exchange response into false fill evidence.
    fill, fill_fields = _consistent_nonnegative_aliases(resp, _FILL_VOLUME_ALIASES)
    if fill_fields and fill is None:
        return None, True
    if fill_fields and fill is not None:
        evidence_present = True
        reported_values.append(fill)

    if "unfilled" in resp:
        evidence_present = True
        unfilled = resp.get("unfilled")
        if not isinstance(unfilled, bool):
            return None, True
        if unfilled is True:
            if not entry_fill_present:
                return None, True
            reported_values.append(0.0)

    recovered_order = resp.get("order")
    if isinstance(recovered_order, dict):
        recovered_fill, recovered_fields = _reported_fill_vol(recovered_order)
        if recovered_fields:
            evidence_present = True
            if recovered_fill is None:
                return None, True
            reported_values.append(recovered_fill)

    # Hyperliquid nested SDK shape: response.data.statuses[].filled.totalSz
    seen_status_lists: set[int] = set()
    for container in (resp, resp.get("response")):
        if not isinstance(container, dict):
            continue
        nested_response = container.get("response")
        data = (
            nested_response.get("data")
            if isinstance(nested_response, dict)
            else container.get("data")
        )
        if not isinstance(data, dict) or "statuses" not in data:
            continue
        statuses = data.get("statuses")
        if not isinstance(statuses, list):
            return None, True
        status_list_id = id(statuses)
        if status_list_id in seen_status_lists:
            continue
        seen_status_lists.add(status_list_id)
        total = 0.0
        found = False
        for st in statuses:
            if not isinstance(st, dict) or "filled" not in st:
                continue
            found = True
            filled = st.get("filled")
            if not isinstance(filled, dict) or "totalSz" not in filled:
                return None, True
            fv = _coerce_float(filled.get("totalSz"))
            if fv is None or fv < 0:
                return None, True
            total += fv
            if not math.isfinite(total):
                return None, True
        if found:
            evidence_present = True
            reported_values.append(total)

    entry_order_id, entry_identity_present = _reported_entry_order_id(resp)
    if entry_identity_present and entry_order_id is None:
        return None, True
    if not evidence_present:
        return None, False
    if not reported_values or len(set(reported_values)) != 1:
        return None, True
    return reported_values[0], True


def _reported_mexc_fill_vol(resp: Any) -> tuple[float | None, bool]:
    """Return only MEXC-shaped fill evidence from an order response."""
    if not isinstance(resp, dict):
        return None, False
    reported_values: list[float] = []
    evidence_present = False

    fill, fill_fields = _consistent_nonnegative_aliases(resp, _FILL_VOLUME_ALIASES)
    if fill_fields:
        evidence_present = True
        if fill is None:
            return None, True
        reported_values.append(fill)

    recovered_order = resp.get("order")
    if isinstance(recovered_order, dict):
        recovered_fill, recovered_fields = _reported_mexc_fill_vol(recovered_order)
        if recovered_fields:
            evidence_present = True
            if recovered_fill is None:
                return None, True
            reported_values.append(recovered_fill)

    entry_order_id, entry_identity_present = _reported_entry_order_id(resp)
    if entry_identity_present and entry_order_id is None:
        return None, True
    if not evidence_present:
        return None, False
    if not reported_values or len(set(reported_values)) != 1:
        return None, True
    return reported_values[0], True


def _extract_filled_vol(resp: Any) -> float | None:
    """Filled quantity of this order, or None when absent or invalid."""
    return _reported_fill_vol(resp)[0]


def _explicit_response_error(value: Any) -> str | None:
    """Return the first explicit ``error`` marker anywhere in a response tree."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.strip().lower() == "error":
                if isinstance(child, str):
                    return child.strip() or "unknown exchange error"
                return "exchange returned an explicit error marker"
            nested = _explicit_response_error(child)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _explicit_response_error(child)
            if nested:
                return nested
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
    explicit_error = _explicit_response_error(resp)
    if explicit_error:
        return explicit_error
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


def _cancel_response_identity_error(response: Any, expected_order_id: int) -> str | None:
    """Reject contradictory MEXC cancel identities at the Money-Path boundary."""
    if isinstance(response, list):
        if len(response) != 1 or not isinstance(response[0], dict):
            return "cancel response does not uniquely identify the requested order"
        returned_id = _consistent_positive_order_id(response[0])
        if returned_id != expected_order_id:
            return "cancel response does not identify the requested order"
        return None
    if isinstance(response, dict) and any(
        key in response for key in ("orderId", "order_id", "oid")
    ):
        returned_id = _consistent_positive_order_id(response)
        if returned_id != expected_order_id:
            return "cancel response does not identify the requested order"
    return None


def _cancel_response_error(
    response: Any, *, expected_order_id: int, is_hyperliquid: bool
) -> str | None:
    error = _close_response_error(response)
    if error is None and not is_hyperliquid:
        error = _cancel_response_identity_error(response, expected_order_id)
    return error


def _recovery_symbol_identity_matches(
    value: Any,
    expected_symbol: str,
    *,
    allow_bare_base_alias: bool = False,
    _seen: set[int] | None = None,
) -> bool:
    """Reject every explicit symbol/coin that belongs to another contract."""
    if not isinstance(value, (dict, list)):
        return True
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return False
    _seen.add(value_id)
    try:
        if isinstance(value, list):
            return all(
                _recovery_symbol_identity_matches(
                    item,
                    expected_symbol,
                    allow_bare_base_alias=allow_bare_base_alias,
                    _seen=_seen,
                )
                for item in value
                if isinstance(item, (dict, list))
            )
        for key in ("symbol", "coin"):
            if key in value and not _symbols_match(
                value.get(key),
                expected_symbol,
                allow_bare_base_alias=allow_bare_base_alias,
            ):
                return False
        for key in ("order", "raw"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)) and not _recovery_symbol_identity_matches(
                nested,
                expected_symbol,
                allow_bare_base_alias=allow_bare_base_alias,
                _seen=_seen,
            ):
                return False
        return True
    finally:
        _seen.remove(value_id)


def _explicit_integer_aliases_match(
    row: dict[str, Any], aliases: tuple[str, ...], expected: int
) -> bool:
    values: list[int] = []
    for key in aliases:
        if key not in row:
            continue
        parsed = _coerce_float(row.get(key))
        if parsed is None or not parsed.is_integer():
            return False
        values.append(int(parsed))
    return not values or len(set(values)) == 1 and values[0] == expected


def _explicit_positive_price_aliases_match(
    row: dict[str, Any], aliases: tuple[str, ...], expected: float
) -> bool:
    reported, present = _consistent_nonnegative_aliases(row, aliases)
    return not present or (
        reported is not None
        and reported > 0
        and math.isclose(reported, expected, rel_tol=1e-9, abs_tol=1e-12)
    )


def _mexc_recovery_matches_request(
    value: Any,
    *,
    expected_side: int,
    expected_vol: float,
    expected_type: int,
    expected_open_type: int,
    expected_price: float | None,
    expected_leverage: int | None,
    expected_stop_loss: float | None,
    expected_take_profit: float | None,
    _seen: set[int] | None = None,
) -> bool:
    """Reject explicit MEXC order terms that conflict with the exact send."""
    if (
        expected_side not in (1, 2, 3, 4)
        or expected_vol <= 0
        or not math.isfinite(expected_vol)
        or expected_type not in (1, 2, 3, 4, 5)
        or expected_open_type not in (1, 2)
        or expected_price is not None
        and (expected_price <= 0 or not math.isfinite(expected_price))
        or expected_leverage is not None
        and expected_leverage <= 0
        or expected_stop_loss is not None
        and (expected_stop_loss <= 0 or not math.isfinite(expected_stop_loss))
        or expected_take_profit is not None
        and (expected_take_profit <= 0 or not math.isfinite(expected_take_profit))
    ):
        return False
    if not isinstance(value, (dict, list)):
        return True
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return False
    _seen.add(value_id)
    try:
        if isinstance(value, list):
            return all(
                _mexc_recovery_matches_request(
                    item,
                    expected_side=expected_side,
                    expected_vol=expected_vol,
                    expected_type=expected_type,
                    expected_open_type=expected_open_type,
                    expected_price=expected_price,
                    expected_leverage=expected_leverage,
                    expected_stop_loss=expected_stop_loss,
                    expected_take_profit=expected_take_profit,
                    _seen=_seen,
                )
                for item in value
                if isinstance(item, (dict, list))
            )
        if not _explicit_integer_aliases_match(value, ("side",), expected_side):
            return False
        reported_vol, vol_present = _consistent_nonnegative_aliases(
            value, ("vol", "orderVol", "order_vol")
        )
        if vol_present and (
            reported_vol is None
            or reported_vol <= 0
            or not math.isclose(
                reported_vol, expected_vol, rel_tol=1e-9, abs_tol=1e-12
            )
        ):
            return False
        if not _explicit_integer_aliases_match(
            value, ("type", "order_type"), expected_type
        ):
            return False
        if not _explicit_integer_aliases_match(
            value, ("openType", "open_type"), expected_open_type
        ):
            return False
        if expected_leverage is not None and not _explicit_integer_aliases_match(
            value, ("leverage",), expected_leverage
        ):
            return False
        if expected_stop_loss is not None and not _explicit_positive_price_aliases_match(
            value,
            ("stopLossPrice", "stop_loss_price"),
            expected_stop_loss,
        ):
            return False
        if expected_take_profit is not None and not _explicit_positive_price_aliases_match(
            value,
            ("takeProfitPrice", "take_profit_price"),
            expected_take_profit,
        ):
            return False
        reported_price, price_present = _consistent_nonnegative_aliases(
            value, ("price", "orderPrice", "order_price")
        )
        if price_present and (
            reported_price is None
            or expected_price is not None
            and not math.isclose(
                reported_price, expected_price, rel_tol=1e-9, abs_tol=1e-12
            )
        ):
            return False
        for key in ("order", "raw"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)) and not _mexc_recovery_matches_request(
                nested,
                expected_side=expected_side,
                expected_vol=expected_vol,
                expected_type=expected_type,
                expected_open_type=expected_open_type,
                expected_price=expected_price,
                expected_leverage=expected_leverage,
                expected_stop_loss=expected_stop_loss,
                expected_take_profit=expected_take_profit,
                _seen=_seen,
            ):
                return False
        return True
    finally:
        _seen.remove(value_id)


def _hl_recovery_matches_request(
    value: Any,
    *,
    expected_side: str,
    expected_vol: float,
    expected_reduce_only: bool,
    _seen: set[int] | None = None,
) -> bool:
    """Reject explicit Hyperliquid economics that conflict with the exact send."""
    if (
        expected_side not in ("A", "B")
        or expected_vol <= 0
        or not math.isfinite(expected_vol)
        or not isinstance(expected_reduce_only, bool)
    ):
        return False
    if not isinstance(value, (dict, list)):
        return True
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return False
    _seen.add(value_id)
    try:
        if isinstance(value, list):
            return all(
                _hl_recovery_matches_request(
                    item,
                    expected_side=expected_side,
                    expected_vol=expected_vol,
                    expected_reduce_only=expected_reduce_only,
                    _seen=_seen,
                )
                for item in value
                if isinstance(item, (dict, list))
            )
        if "side" in value and value.get("side") != expected_side:
            return False
        reduce_only_values: list[bool] = []
        for key in ("reduceOnly", "reduce_only"):
            if key not in value:
                continue
            reduce_only = value.get(key)
            if not isinstance(reduce_only, bool):
                return False
            reduce_only_values.append(reduce_only)
        if reduce_only_values and (
            len(set(reduce_only_values)) != 1
            or reduce_only_values[0] is not expected_reduce_only
        ):
            return False
        original_vol, original_present = _consistent_nonnegative_aliases(
            value, ("origSz", "orig_sz", "origVol", "orig_vol")
        )
        if original_present and (
            original_vol is None
            or original_vol <= 0
            or not math.isclose(
                original_vol, expected_vol, rel_tol=1e-9, abs_tol=1e-12
            )
        ):
            return False
        remaining_vol, remaining_present = _consistent_nonnegative_aliases(
            value, ("sz", "vol")
        )
        if remaining_present and (
            remaining_vol is None
            or remaining_vol > expected_vol + max(1e-12, expected_vol * 1e-9)
        ):
            return False
        for key in ("order", "raw"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)) and not _hl_recovery_matches_request(
                nested,
                expected_side=expected_side,
                expected_vol=expected_vol,
                expected_reduce_only=expected_reduce_only,
                _seen=_seen,
            ):
                return False
        return True
    finally:
        _seen.remove(value_id)


def _recovery_is_match(
    recovered: Any,
    external_oid: str,
    *,
    expected_symbol: str | None = None,
    allow_bare_base_alias: bool = False,
) -> bool:
    """True if `recovered` is trustworthy evidence our order is already live.

    O-05: accepts either an exchange-client MATCH MARKER — a dict carrying an
    allowed string ``match`` field, which the client sets only after matching OUR
    oid/cloid (MEXC ``history``/``open`` and HL ``cloid`` are field-filtered) —
    or, lacking a marker, an exact external-oid field on a dict/list row (the HL
    list-of-hits fallback). Every list row must identify that same external oid
    and a concrete order. Marker fields inside that raw-row list and explicit
    error fields anywhere in the evidence are rejected. Free-text substring
    matches are not evidence: a
    diagnostic like ``<oid> not found`` must remain a failed recovery.
    """
    if not recovered:
        return False
    if _explicit_response_error(recovered):
        return False
    if isinstance(recovered, dict) and "match" in recovered:
        marker_value = recovered.get("match")
        if not isinstance(marker_value, str):
            return False
        marker = marker_value.lower()
        if marker not in {"direct", "history", "open", "cloid"}:
            return False
        if not _has_exact_external_oid(recovered, external_oid):
            return False
        if expected_symbol is not None and not _recovery_symbol_identity_matches(
            recovered,
            expected_symbol,
            allow_bare_base_alias=allow_bare_base_alias,
        ):
            return False
        recovered_order_id, identity_present = _reported_entry_order_id(recovered)
        if not identity_present or recovered_order_id is None:
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
    if isinstance(recovered, list):
        if not all(isinstance(row, dict) for row in recovered):
            return False
        if any("match" in row for row in recovered):
            return False
    rows = recovered if isinstance(recovered, list) else [recovered]
    matched_order_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not _has_exact_external_oid(row, external_oid):
            return False
        if expected_symbol is not None and not _recovery_symbol_identity_matches(
            row,
            expected_symbol,
            allow_bare_base_alias=allow_bare_base_alias,
        ):
            return False
        order_id = _consistent_positive_order_id(row)
        if order_id is None:
            return False
        matched_order_ids.add(order_id)
    return len(matched_order_ids) == 1


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


def _consistent_positive_order_id(row: Any) -> int | None:
    if not isinstance(row, dict):
        return None
    values: list[int] = []
    for key in ("orderId", "order_id", "oid"):
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool):
            return None
        try:
            text = str(value)
        except (ValueError, OverflowError):
            return None
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


def _has_consistent_positive_order_id(row: Any) -> bool:
    return _consistent_positive_order_id(row) is not None


def _reported_entry_order_id(
    response: Any, _seen: set[int] | None = None
) -> tuple[int | None, bool]:
    """Return (consistent entry-order ID, identity evidence present)."""
    if not isinstance(response, dict):
        return None, False
    if _seen is None:
        _seen = set()
    response_id = id(response)
    if response_id in _seen:
        return None, True
    _seen.add(response_id)

    values: list[int] = []
    evidence_present = False
    if any(key in response for key in ("orderId", "order_id", "oid")):
        evidence_present = True
        normalized_id = _consistent_positive_order_id(response)
        if normalized_id is None:
            return None, True
        values.append(normalized_id)

    recovered_order = response.get("order")
    if isinstance(recovered_order, dict):
        nested_id, nested_present = _reported_entry_order_id(recovered_order, _seen)
        if nested_present:
            evidence_present = True
            if nested_id is None:
                return None, True
            values.append(nested_id)

    # Hyperliquid's raw entry result is retained under ``response`` and carries
    # the actual order ID in response.data.statuses[].filled/resting. The
    # normalized top-level ID must agree before it is safe to cancel or expose.
    seen_status_lists: set[int] = set()
    for container in (response, response.get("response")):
        if not isinstance(container, dict):
            continue
        nested_response = container.get("response")
        data = (
            nested_response.get("data")
            if isinstance(nested_response, dict)
            else container.get("data")
        )
        if not isinstance(data, dict) or "statuses" not in data:
            continue
        statuses = data.get("statuses")
        if not isinstance(statuses, list):
            return None, True
        status_list_id = id(statuses)
        if status_list_id in seen_status_lists:
            continue
        seen_status_lists.add(status_list_id)
        for status in statuses:
            if not isinstance(status, dict):
                continue
            variants = [key for key in ("filled", "resting") if key in status]
            if not variants:
                continue
            evidence_present = True
            if len(variants) != 1 or not isinstance(status.get(variants[0]), dict):
                return None, True
            embedded_id = _consistent_positive_order_id(status[variants[0]])
            if embedded_id is None:
                return None, True
            values.append(embedded_id)

    if not evidence_present:
        return None, False
    if not values or len(set(values)) != 1:
        return None, True
    return values[0], True


def _consistent_entry_order_id(response: Any) -> int | None:
    """Return one entry-order ID across normalized and embedded evidence."""
    return _reported_entry_order_id(response)[0]


def _consistent_stop_order_id(row: Any) -> int | None:
    """Return one unambiguous positive ID from a normalized stop-order row."""
    if not isinstance(row, dict):
        return None
    raw = row.get("raw")
    sources = (row, raw) if isinstance(raw, dict) else (row,)
    values: list[int] = []
    for source in sources:
        for key in ("orderId", "order_id", "oid", "id"):
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


def _public_order_ack(
    response: Any,
    *,
    order_id: int | None = None,
    recovered: bool = False,
    include_trigger_ids: bool = False,
) -> dict[str, Any]:
    """Return the small allowlisted mutation receipt exposed by the API."""
    result: dict[str, Any] = {"accepted": True}
    if order_id is not None:
        result["orderId"] = order_id
    if isinstance(response, dict):
        if order_id is None:
            response_order_id = _consistent_entry_order_id(response)
            if response_order_id is not None:
                result["orderId"] = response_order_id
        if include_trigger_ids:
            for key in ("slTriggerOid", "tpTriggerOid", "tpTriggerOid2"):
                value = response.get(key)
                parsed = _consistent_stop_order_id({"orderId": value})
                if parsed is not None:
                    result[key] = parsed
    if recovered:
        result["recovered"] = True
    return result


def _is_uncertain_order_error(exc: Exception) -> bool:
    raw = getattr(exc, "raw", None)
    status = raw.get("status") if isinstance(raw, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 408 or status >= 500:
            return True
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
            "uncertain cancel response",
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
    tp1 = _normalized_float(ticket.take_profit)
    tp2 = ticket.tp2
    if tp1 is None or tp1 <= 0:
        errs.append("scale-out requires TP1 (take_profit)")
    tp2_f = _normalized_float(tp2)
    if tp2_f is None or tp2_f <= 0:
        errs.append("scale-out requires a positive finite TP2 (tp2)")
    share = _normalized_float(getattr(ticket, "tp1_share", None))
    if share is None or not (0.0 < share < 1.0):
        errs.append("tp1_share must be between 0 and 1")
    if errs:
        return errs
    side = (ticket.side or "").lower()
    e = float(entry) if entry else None
    if side == "long":
        if not (tp2_f > tp1):
            errs.append("long scale-out: TP2 must be above TP1")
        if e is not None and not (tp1 > e):
            errs.append("long scale-out: TP1 must be above entry")
    elif side == "short":
        if not (tp2_f < tp1):
            errs.append("short scale-out: TP2 must be below TP1")
        if e is not None and not (tp1 < e):
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
        client_is_active: Callable[[Any], bool] | None = None,
    ):
        self.client = client
        self.settings = settings
        self.store = store
        self.db = db
        self._client_is_active = client_is_active
        # Serialize confirm/close so parallel places cannot both see risk=0.
        # MUST be shared across requests: a new OrderService is built per
        # request, so a per-instance lock would serialize nothing. The caller
        # passes an app-global lock; the fallback only helps single-instance
        # unit tests.
        self._trade_lock = trade_lock if trade_lock is not None else asyncio.Lock()

    def _require_active_client(self) -> None:
        """Fail closed if the caller reports a replaced runtime context."""
        if self._client_is_active is None:
            return
        try:
            active = self._client_is_active(self.client)
        except Exception as exc:
            raise OrderError(
                "Exchange configuration changed. Retry the request."
            ) from exc
        if active is not True:
            raise OrderError("Exchange configuration changed. Retry the request.")

    @staticmethod
    def _ticker_price(
        ticker: Any,
        symbol: str,
        *,
        allow_bare_base_alias: bool = False,
    ) -> float:
        """Return a price only when the ticker belongs to the requested symbol."""
        ticker_symbol = getattr(ticker, "symbol", None)
        if not _symbols_match(
            ticker_symbol,
            symbol,
            allow_bare_base_alias=allow_bare_base_alias,
        ):
            raise OrderError("ticker symbol does not match requested order symbol")
        last_price = _normalized_float(getattr(ticker, "last_price", None))
        if last_price is None or last_price <= 0:
            raise OrderError("ticker price unavailable or invalid")
        return last_price

    async def _audit_order_best_effort(self, **fields: Any) -> str | None:
        """Persist a bounded, credential-free audit without hiding the outcome."""
        if self.db is None:
            return None
        secret_values = tuple(
            value
            for name in (
                "mexc_api_key",
                "mexc_api_secret",
                "hl_private_key",
                "local_api_token",
                "anthropic_api_key",
                "xai_api_key",
                "openai_api_key",
            )
            if len(value := str(getattr(self.settings, name, "") or "").strip()) >= 4
        )
        safe_fields = _audit_safe_value(fields, secret_values=secret_values)
        try:
            await self.db.insert_order(**safe_fields)
        except Exception:  # noqa: BLE001 — exchange result stays authoritative
            return "audit log failed"
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
        self._require_active_client()
        try:
            ticket = OrderTicket.model_validate(ticket.model_dump(warnings=False))
        except (AttributeError, TypeError, ValueError) as exc:
            raise OrderError("preview has invalid ticket") from exc
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
        # Setup may replace the exchange while the independent reads are in
        # flight. Never derive confirm authority from a detached client or old
        # settings, even though Confirm would run fresh gates later.
        self._require_active_client()

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
        last_price = self._ticker_price(ticker_r, symbol)

        # Account read + its mappings share the fail-closed dict the old separate
        # _balances / _existing_risk failures returned (Preview shows gate errors,
        # never a 500). A fresh-read fetch failure maps to the same "equity
        # unavailable" OrderError class the balances path raised.
        try:
            if isinstance(account_r, BaseException):
                if isinstance(account_r, ExchangeError):
                    raise OrderError(
                        "equity unavailable; positions unavailable — exchange "
                        "account data could not be read"
                    ) from account_r
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

        try:
            if self.db is not None:
                # Mirror PreviewStore.create()'s clamp (app/orders/tokens.py) so the
                # DB-preview row's expiry never diverges from the in-memory token's
                # for ttl<=0 (both must agree on when a preview is actually gone).
                expires = datetime.now(timezone.utc).timestamp() + max(1, int(ttl))
                await self.db.insert_preview(
                    token_hash=token_hash,
                    payload_json=payload,
                    expires_at=_utc_now_iso_from_ts(expires),
                )
            # The local audit write above yields control. Recheck the generation
            # immediately before the token can leave the service.
            self._require_active_client()
            remaining_ttl = self.store.remaining_seconds(token)
            if remaining_ttl is None:
                raise OrderError("Preview was superseded or expired. Retry preview.")
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
            "expires_in_seconds": remaining_ttl,
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
            stop_match_detail: str | None = None
            seen_stop_ids: set[int] = set()

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
                    stop_id = _consistent_stop_order_id(s)
                    if stop_id is None or stop_id in seen_stop_ids:
                        invalid_stop_evidence = True
                        last_detail = (
                            "stop order has an invalid or duplicate identity"
                        )
                        continue
                    seen_stop_ids.add(stop_id)
                    if not _symbols_match(
                        s.get("symbol"), symbol, allow_bare_base_alias=is_hl
                    ):
                        invalid_stop_evidence = True
                        last_detail = "stop order has an invalid symbol identity"
                        continue
                    reduce_only, reduce_only_valid = classify_reduce_only_fields(s)
                    if not reduce_only_valid or reduce_only is False:
                        invalid_stop_evidence = True
                        last_detail = (
                            "stop order has invalid reduce-only geometry"
                        )
                        continue
                    position_side, position_side_valid = (
                        classify_position_side_fields(s)
                    )
                    if not position_side_valid:
                        invalid_stop_evidence = True
                        last_detail = (
                            "stop order has an invalid position-side identity"
                        )
                        continue
                    if position_side is not None and position_side != side.lower():
                        continue
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
                            stop_match_detail = "explicit stop-loss field matched"
                        continue
                    # Shared backend classifier (Q-05): evaluate every label
                    # alias and reject an ambiguous SL/TP identity. Explicit SL
                    # fields above retain their documented priority.
                    label, labels_valid = classify_order_label_fields(s)
                    if not labels_valid:
                        invalid_stop_evidence = True
                        last_detail = (
                            "stop order labels were invalid or conflicting"
                        )
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
                            label == "sl" or label is None
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
                            stop_match_detail = f"stop order field {key} matched"
            except ExchangeError:
                last_detail = "open stop orders lookup failed"

            # A matching row is trustworthy only after the complete snapshot
            # has been checked for contradictory stop evidence.
            if stop_match_detail is not None and not invalid_stop_evidence:
                return True, stop_match_detail, True

            # 2) Position row fields — ADDITIONAL positive evidence only. Their
            # absence never proves the SL is missing, so it must not set checked.
            position_match_detail: str | None = None
            matching_position_rows = 0
            try:
                # Post-placement protection evidence must bypass the adapter's
                # short display cache. A pre-submit row can carry an old
                # same-price SL and must never verify this new entry.
                positions = await self.client.positions(symbol, fresh=True)
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
                    if not _symbols_match(
                        p.get("symbol"), symbol, allow_bare_base_alias=is_hl
                    ):
                        invalid_position_evidence = True
                        last_detail = "position has an invalid symbol identity"
                        continue
                    side_raw = p.get("side")
                    position_side = (
                        side_raw.strip().lower()
                        if isinstance(side_raw, str)
                        else ""
                    )
                    hold_vol = _normalized_float(p.get("hold_vol"))
                    if position_side not in ("long", "short") or (
                        hold_vol is None or hold_vol <= 0
                    ):
                        invalid_position_evidence = True
                        last_detail = "position has an invalid identity or volume"
                        continue
                    if position_side != side.lower():
                        continue
                    matching_position_rows += 1
                    if matching_position_rows > 1:
                        invalid_position_evidence = True
                        last_detail = "position snapshot has duplicate matching rows"
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
                        position_match_detail = "position SL field matched"
            except ExchangeError:
                last_detail = last_detail or "positions lookup failed"

            if (
                position_match_detail is not None
                and not invalid_stop_evidence
                and not invalid_position_evidence
            ):
                return True, position_match_detail, True

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
    ) -> float | None:
        """Order-own fill evidence for a MEXC LIMIT without a reported fill (X2-01).

        A hold-delta can be an EXTERNAL same-side bump on a still-resting limit, so
        it must NEVER verify a limit (that is exactly the class of unprotected order
        the positive verify was built to guard). Instead ask the exchange about OUR
        order by ``externalOid``: only a ``dealVol`` within the ordered size — or a
        smaller dealVol paired with an explicit fully-filled state — counts as OUR
        fill. Any lookup failure or ambiguity returns no fill, so the caller keeps
        UNKNOWN (loud), never a silent verify and never a new flatten path.
        """
        try:
            found = await self.client.order_by_external_oid(symbol, external_oid)
        except Exception:  # noqa: BLE001 — any lookup failure → UNKNOWN, fail-closed
            return None
        if not isinstance(found, dict) or not found:
            return None
        if not _recovery_is_match(
            found, external_oid, expected_symbol=symbol
        ):
            return None
        order = found.get("order")
        if not isinstance(order, dict):
            return None
        if "symbol" in order and not _symbols_match(order.get("symbol"), symbol):
            return None
        deal, deal_fields = _consistent_nonnegative_aliases(
            order, ("dealVol", "deal_vol", "dealVolume")
        )
        if not deal_fields or deal is None:
            return None
        if deal > rounded_vol + vol_eps:
            return None
        states: list[str] = []
        for key in ("state", "orderState"):
            if key not in order:
                continue
            raw_state = order.get(key)
            if isinstance(raw_state, bool):
                return None
            normalized_state = str(raw_state).strip().lower()
            if not normalized_state:
                return None
            states.append(normalized_state)
        if len(set(states)) > 1:
            return None
        # Primary signal: our order filled at/above the ordered size.
        if deal >= rounded_vol - vol_eps:
            return deal
        # Corroborated signal: a partial dealVol PLUS an explicit fully-filled
        # state (MEXC futures state 3 = completed/filled).
        state = states[0] if states else ""
        if deal > vol_eps and state in ("3", "filled", "completed", "done"):
            return deal
        return None

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
        exchange = "hyperliquid" if is_hl else "mexc"
        matched: tuple[float, int] | None = None
        for raw in positions:
            if not isinstance(raw, dict):
                return 0.0, 1, False
            p = map_position(raw) if "hold_vol" not in raw else raw
            symbol_raw = p.get("symbol")
            if not valid_normalized_position_symbol(
                symbol_raw, exchange=exchange
            ):
                return 0.0, 1, False
            if not _symbols_match(
                symbol_raw, symbol, allow_bare_base_alias=is_hl
            ):
                continue
            side_raw = p.get("side")
            if not _valid_normalized_position_side(side_raw):
                return 0.0, 1, False
            if side_raw != side:
                continue
            hv = _normalized_float(p.get("hold_vol"))
            ot_raw = p.get("open_type")
            ot = _normalized_position_open_type(ot_raw)
            if ot is None:
                return 0.0, 0, False
            if hv is None or hv <= 0:
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
        if not isinstance(positions, list) or any(
            not isinstance(raw, dict) for raw in positions
        ):
            return (0.0, 1, False), None
        matched: tuple[tuple[float, int, bool], int | None] | None = None
        for raw in positions:
            p = map_position(raw) if "hold_vol" not in raw else raw
            position_symbol = p.get("symbol")
            if not valid_normalized_position_symbol(
                position_symbol, exchange="mexc"
            ):
                return (0.0, 1, False), None
            if position_symbol != symbol:
                continue
            position_side = p.get("side")
            if not _valid_normalized_position_side(position_side):
                return (0.0, 1, False), None
            if position_side != side:
                continue
            hv_raw = _normalized_float(p.get("hold_vol"))
            ot_raw = p.get("open_type")
            ot = _normalized_position_open_type(ot_raw)
            if ot is None:
                return (0.0, 0, False), None
            if hv_raw is None or hv_raw <= 0:
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

    @staticmethod
    async def _drain_task_after_cancellation(task: asyncio.Task[Any]) -> None:
        """Wait for a shielded task despite repeated caller cancellation."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            try:
                task.result()
            except BaseException:
                pass

    async def _await_pre_submit_mutation(
        self, operation: Coroutine[Any, Any, Any]
    ) -> Any:
        """Finish a started setup mutation, then propagate caller cancellation."""
        operation_task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            await self._drain_task_after_cancellation(operation_task)
            raise

    async def _run_locked_mutation(
        self,
        operation: Coroutine[Any, Any, dict[str, Any]],
        *,
        mutation_started: asyncio.Event,
    ) -> dict[str, Any]:
        """Defer caller cancellation after the first exchange mutation starts."""
        operation_task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            if not mutation_started.is_set():
                # No exchange mutation has started, so cancellation is still
                # safe. Stop the internal task before it can cross that line.
                operation_task.cancel()

            # Once the workflow's primary mutation starts, its exchange outcome
            # is uncertain even if the request task is cancelled: a worker thread
            # or remote transport may still complete it. Keep the trade lock and
            # let the internal operation finish reconciliation, verification and
            # audit before propagating cancellation. Repeated cancellation
            # requests must not interrupt that safety work.
            await self._drain_task_after_cancellation(operation_task)
            raise

    async def confirm(self, token: str) -> dict[str, Any]:
        """Consume token, re-check arming + gates, set leverage, place order."""
        async with self._trade_lock:
            self._require_active_client()
            mutation_started = asyncio.Event()
            return await self._run_locked_mutation(
                self._confirm_locked(token, mutation_started=mutation_started),
                mutation_started=mutation_started,
            )

    async def _confirm_locked(
        self, token: str, *, mutation_started: asyncio.Event | None = None
    ) -> dict[str, Any]:
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
        preview_last = _normalized_float(payload.get("last_price"))
        if preview_last is None or preview_last <= 0:
            raise OrderError("preview payload has invalid or missing last_price")

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
        last_price = self._ticker_price(ticker_r, symbol)

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
            await self._await_pre_submit_mutation(
                self.client.set_leverage(
                    symbol,
                    int(ticket.leverage),
                    open_type,
                    position_type=position_type,
                    position_id=position_id,
                )
            )
        except ExchangeError as e:
            raise OrderError(f"set_leverage failed — order blocked: {e}") from e

        # Hold before place — used so auto-flatten never closes pre-existing size.
        # pre_hold_ok records whether the query was RELIABLE; a failed lookup
        # must fail-closed (no differential-flatten) rather than assume 0.
        # On MEXC this reuses the read taken above (no duplicate positions call).

        recovered_from_timeout = False
        if mutation_started is not None:
            mutation_started.set()
        try:
            resp = await self.client.place_order(body)
        except ExchangeError as e:
            # Timeout / network / uncertain-response: try recover by externalOid
            # before declaring failure. An unparseable 2xx body (F-04) is just as
            # uncertain as a timeout — the order may already be live — so it must
            # go through the same reconciliation path, not a hard failure.
            recovered = None
            outcome_unknown = _is_uncertain_order_error(e)
            if outcome_unknown:
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
                recovery_matches = _recovery_is_match(
                    recovered,
                    external_oid,
                    expected_symbol=symbol,
                    allow_bare_base_alias=(
                        getattr(self.client, "exchange_id", "") == "hyperliquid"
                    ),
                )
                if (
                    recovery_matches
                    and getattr(self.client, "exchange_id", "") != "hyperliquid"
                    and not _mexc_recovery_matches_request(
                        recovered,
                        expected_side=int(body["side"]),
                        expected_vol=float(body["vol"]),
                        expected_type=int(body["type"]),
                        expected_open_type=int(body["openType"]),
                        expected_price=(
                            float(body["price"])
                            if int(body["type"]) == MEXC_TYPE_LIMIT
                            else None
                        ),
                        expected_leverage=int(body["leverage"]),
                        expected_stop_loss=(
                            float(body["stopLossPrice"])
                            if "stopLossPrice" in body
                            else None
                        ),
                        expected_take_profit=(
                            float(body["takeProfitPrice"])
                            if "takeProfitPrice" in body
                            else None
                        ),
                    )
                ):
                    recovery_matches = False
                if (
                    recovery_matches
                    and getattr(self.client, "exchange_id", "") == "hyperliquid"
                    and not _hl_recovery_matches_request(
                        recovered,
                        expected_side="B" if ticket.side == "long" else "A",
                        expected_vol=float(body["vol"]),
                        expected_reduce_only=False,
                    )
                ):
                    recovery_matches = False
                if not recovery_matches:
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
                if outcome_unknown:
                    raise OrderOutcomeUnknown(
                        "Order submission outcome is unknown for "
                        f"externalOid={external_oid}. The order may be live; reconcile "
                        "positions and open orders before another preview."
                    ) from e
                audit_suffix = f" ({audit_error})" if audit_error else ""
                raise OrderError(f"place_order failed: {e}{audit_suffix}") from e
            # Order is (likely) live — fall through to SL verify/flatten (not early-return)
            recovered_from_timeout = True
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
                "verify on the exchange."
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
        sl_coverage_unknown: bool = False
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

        exchange_id = getattr(self.client, "exchange_id", "")
        is_mexc = exchange_id == "mexc"
        is_hl = exchange_id == "hyperliquid"

        # A non-marketable LIMIT entry that Hyperliquid RESTS carries no filled
        # position to protect: the HL adapter places NO SL by design (F-02) and
        # returns unfilled=True / entryFilledSz=0. That is NOT an SL failure — we
        # must not "verify" a stop that cannot exist yet, and must not
        # auto-flatten/cancel a perfectly valid resting order. (Regression fix:
        # before F-02 the SL trigger rested unconditionally with an oid, so this
        # path never mislabelled a resting limit as "SL nicht verifiziert".)
        response_fill, response_fill_present = (
            (None, False) if is_mexc else _reported_fill_vol(resp)
        )
        entry_fill = (
            _normalized_float(resp.get("entryFilledSz"))
            if isinstance(resp, dict) and "entryFilledSz" in resp
            else None
        )
        unfilled_resting = (
            not is_mexc
            and isinstance(resp, dict)
            and resp.get("unfilled") is True
            and entry_fill == 0.0
            and response_fill_present
            and response_fill == 0.0
        )

        # ── MEXC fill evidence (O-01 positive verify + O-02 resting) ──────────
        # MEXC never returns an slTriggerOid and the SL is position-bound in the
        # create body, so `open_stop_orders` is [] on every normal trade (→
        # _verify_sl_attached now yields UNKNOWN there, not MISSING). The ONLY
        # reliable positive signal is whether the entry actually FILLED: MEXC
        # accepts the create body with stopLossPrice atomically (fill ⟹ SL
        # accepted). We derive the fill from the response, else from the hold
        # delta against the reliable pre-trade quantity.
        entry_order_type = (getattr(ticket, "order_type", "") or "").lower()
        mexc_new_fill: float | None = None
        mexc_fill_known = False
        mexc_fill_for_coverage: float | None = None
        # Provenance of the fill evidence. `reported` comes from the order
        # response and is bot-safe (counts ONLY this order's volume). The hold
        # delta is NOT: a concurrent same-side position increase by another bot
        # can inflate it, so hold-delta evidence needs the attribution guard in
        # the positive-verify block below before it may fully verify.
        mexc_fill_from_report = False
        mexc_fill_report_present = False
        if is_mexc and not manual_sltp and not unfilled_resting:
            reported, fill_reported = _reported_mexc_fill_vol(resp)
            mexc_fill_report_present = fill_reported
            fill_eps = max(float(gate.rounded_vol) * 1e-4, 1e-9)
            if (
                reported is not None
                and reported <= float(gate.rounded_vol) + fill_eps
            ):
                mexc_new_fill = reported
                mexc_fill_known = True
                mexc_fill_from_report = True
                mexc_fill_for_coverage = reported
            elif fill_reported:
                # Explicit but invalid/contradictory fill evidence is UNKNOWN.
                # Do not reinterpret a concurrent same-side hold increase as
                # this order's market fill.
                pass
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
                raw_sl_trigger_oid = (
                    resp.get("slTriggerOid")
                    if is_hl and isinstance(resp, dict)
                    else None
                )
                sl_trigger_oid = (
                    _consistent_stop_order_id({"orderId": raw_sl_trigger_oid})
                    if raw_sl_trigger_oid is not None
                    else None
                )
                invalid_sl_trigger_oid = (
                    raw_sl_trigger_oid is not None and sl_trigger_oid is None
                )
                trigger_errors = (
                    resp.get("triggerErrors")
                    if is_hl and isinstance(resp, dict)
                    else None
                )
                sl_trigger_failed, other_trigger_failed = _classify_trigger_errors(
                    trigger_errors
                )
                if sl_trigger_oid is not None and not sl_trigger_failed:
                    sl_verified = True
                    sl_detail = f"exchange accepted SL trigger (oid={sl_trigger_oid})"
                elif sl_trigger_oid is not None:
                    sl_verified, sl_detail, sl_checked = await self._verify_sl_oid(
                        symbol,
                        sl_trigger_oid,
                        float(sl),
                        float(gate.rounded_vol),
                        side=ticket.side,
                    )
                else:
                    sl_verified, sl_detail, sl_checked = await self._verify_sl_attached(
                        symbol=symbol,
                        expected_sl=float(sl),
                        side=ticket.side,
                        pre_existing_same_side=bool(pre_hold_ok and pre_hold > 0),
                    )
                if sl_trigger_failed or invalid_sl_trigger_oid:
                    sl_detail += "; exchange reported an SL trigger error"
                if other_trigger_failed:
                    warnings.append(
                        "One or more exchange take-profit triggers were not accepted. "
                        "Review the position and add the missing take-profit protection."
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
                    if mexc_fill_report_present:
                        fill_is_ours = (
                            mexc_fill_from_report
                            and mexc_new_fill is not None
                            and vol_eps < mexc_new_fill <= rounded_vol + vol_eps
                        )
                        if mexc_new_fill is not None:
                            fill_desc = f"fill≈{mexc_new_fill:g}"
                    elif entry_order_type == "market":
                        fill_is_ours = (
                            mexc_new_fill is not None
                            and mexc_new_fill >= rounded_vol - vol_eps
                            and mexc_new_fill <= rounded_vol + vol_eps
                        )
                        if mexc_new_fill is not None:
                            fill_desc = f"fill≈{mexc_new_fill:g}"
                    else:
                        # LIMIT: order-own evidence only — never the hold delta.
                        confirmed_fill = await self._mexc_order_fill_confirmed(
                            symbol, external_oid, rounded_vol, vol_eps
                        )
                        fill_is_ours = confirmed_fill is not None
                        if confirmed_fill is not None:
                            mexc_fill_for_coverage = confirmed_fill
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
        except Exception:  # noqa: BLE001 — order is live, must not bubble
            post_errors.append("post-place SL verification failed")
            sl_verified = False
            sl_checked = False
            warnings.append(
                "Post-placement SL verification failed — the order IS placed. "
                "Check the SL and position on the exchange now."
            )

        # M1: a partially-filled resting GTC limit has verified protection only
        # for the current fill. We do not watch later fills, so future coverage
        # of the resting remainder cannot be claimed. Market orders are IOC and
        # are unaffected.
        order_type = (getattr(ticket, "order_type", "") or "").lower()
        if (
            not manual_sltp
            and not unfilled_resting
            and order_type == "limit"
            and sl is not None
            and float(sl) > 0
            and not self.settings.allow_unprotected_entry
            and isinstance(resp, dict)
        ):
            requested = float(gate.rounded_vol)
            eps = max(requested * 1e-4, 1e-9)
            if is_mexc and mexc_fill_for_coverage is not None:
                filled_present = True
                filled = mexc_fill_for_coverage
            else:
                filled_present = "entryFilledSz" in resp
                filled = (
                    _normalized_float(resp.get("entryFilledSz"))
                    if filled_present
                    else None
                )
            if filled_present and (
                filled is None or filled <= 0 or filled > requested + eps
            ):
                sl_fully_verified = False
                sl_coverage_unknown = True
                warnings.append(
                    "FILL COVERAGE UNKNOWN: The exchange returned an invalid or "
                    "inconsistent entry fill size. The accepted SL may not cover "
                    "the intended position; verify the entry and stop on the "
                    "exchange NOW."
                )
            elif filled is not None and filled + eps < requested:
                sl_fully_verified = False
                if is_mexc:
                    warnings.append(
                        "PARTIALLY FILLED: Limit order filled only partially "
                        f"({filled:g} of {requested:g}). The position-bound SL "
                        "is verified for the current fill, but protection after "
                        "a later remainder fill is UNKNOWN. Monitor and verify "
                        "the final position protection, or cancel the remainder."
                    )
                else:
                    warnings.append(
                        "PARTIALLY FILLED: Limit order filled only partially "
                        f"({filled:g} of {requested:g}). The exchange-side SL "
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
                if is_mexc:
                    reported_fill, fill_reported = _reported_mexc_fill_vol(resp)
                else:
                    reported_fill, fill_reported = _reported_fill_vol(resp)
                new_fill: float | None
                fill_source = ""
                if reported_fill is not None:
                    new_fill = min(float(gate.rounded_vol), reported_fill)
                    fill_source = "order response"
                elif fill_reported:
                    new_fill = None
                    flatten_result = {
                        "action": "skipped_invalid_fill_report",
                        "error": "exchange fill report is invalid or contradictory",
                    }
                    warnings.append(
                        "AUTO_FLATTEN skipped: the exchange fill report is invalid "
                        "or contradictory, so NOTHING will be closed or cancelled. "
                        "Check the position, entry order and SL on the exchange NOW."
                    )
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
                    entry_order_id = _consistent_entry_order_id(resp)
                    cancel_ids: list[int] = (
                        [entry_order_id] if entry_order_id is not None else []
                    )
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
                            cancel_error = _cancel_response_error(
                                cancel_response,
                                expected_order_id=coid,
                                is_hyperliquid=is_hl,
                            )
                            if cancel_error:
                                raise OrderError(cancel_error)
                            cancelled.append(coid)
                        except Exception:  # noqa: BLE001
                            cancel_errors.append(f"{coid}: cancellation failed")
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
                            "AUTO_FLATTEN: no fill and no unambiguous orderId to "
                            "cancel — check exchange for resting entry"
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
                        recovery_matches = _recovery_is_match(
                            recovered,
                            recovery_oid,
                            expected_symbol=symbol,
                            allow_bare_base_alias=(
                                getattr(self.client, "exchange_id", "")
                                == "hyperliquid"
                            ),
                        )
                        if (
                            recovery_matches
                            and getattr(self.client, "exchange_id", "")
                            != "hyperliquid"
                            and not _mexc_recovery_matches_request(
                                recovered,
                                expected_side=4 if ticket.side == "long" else 2,
                                expected_vol=vol,
                                expected_type=MEXC_TYPE_MARKET,
                                expected_open_type=pos_open_type or open_type,
                                expected_price=None,
                                expected_leverage=None,
                                expected_stop_loss=None,
                                expected_take_profit=None,
                            )
                        ):
                            recovery_matches = False
                        if (
                            recovery_matches
                            and getattr(self.client, "exchange_id", "")
                            == "hyperliquid"
                            and not _hl_recovery_matches_request(
                                recovered,
                                expected_side=(
                                    "A" if ticket.side == "long" else "B"
                                ),
                                expected_vol=vol,
                                expected_reduce_only=True,
                            )
                        ):
                            recovery_matches = False
                        if not recovery_matches:
                            raise
                        flatten_result = recovered
                        flatten_recovery_warning = (
                            "AUTO_FLATTEN transport response was uncertain, but "
                            "the close order was recovered using externalOid="
                            f"{recovery_oid}. Do not close again; the live "
                            "position will be verified."
                        )
                    flatten_error = (
                        _close_response_error(flatten_result)
                        if flatten_recovery_warning is None
                        else None
                    )
                    if flatten_error:
                        raise OrderRejectedByExchange(
                            "automatic close was rejected by the exchange"
                        )
                    flatten_result = _public_order_ack(
                        flatten_result,
                        recovered=flatten_recovery_warning is not None,
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
            except Exception:  # noqa: BLE001
                warnings.append(
                    "AUTO_FLATTEN failed — close manually on the exchange"
                )
                flatten_result = {"error": "automatic close failed"}

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
        # M1: mark incomplete or unverifiable SL coverage. `sl_verified` means
        # the trigger is real; it does not prove coverage of the intended size.
        if sl_verified and not sl_fully_verified:
            status = status + (
                "_coverage_unknown" if sl_coverage_unknown else "_partial_fill"
            )

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
            "response": _public_order_ack(
                resp,
                recovered=recovered_from_timeout,
                include_trigger_ids=is_hl,
            ),
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
            self._require_active_client()
            mutation_started = asyncio.Event()
            return await self._run_locked_mutation(
                self._close_position_locked(
                    symbol=symbol,
                    side=side,
                    vol=vol,
                    fraction=fraction,
                    mutation_started=mutation_started,
                ),
                mutation_started=mutation_started,
            )

    async def _close_position_locked(
        self,
        *,
        symbol: str,
        side: str,
        vol: float | None = None,
        fraction: float | None = None,
        mutation_started: asyncio.Event | None = None,
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
                parsed_hold = _normalized_float(p.get("hold_vol"))
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
            parsed_vol_unit = _normalized_float(contract.vol_unit)
            parsed_min_vol = _normalized_float(contract.min_vol)
        except ExchangeError:
            contract_known = False
            vol_unit = 0.0
            min_vol = 0.0
            contract_problem = "contract metadata unavailable"
        else:
            contract_symbol_matches = _symbols_match(
                getattr(contract, "symbol", None),
                symbol,
                allow_bare_base_alias=is_hl,
            )
            contract_known = (
                contract_symbol_matches
                and parsed_vol_unit is not None
                and parsed_vol_unit > 0
                and parsed_min_vol is not None
                and parsed_min_vol > 0
            )
            vol_unit = parsed_vol_unit or 0.0
            min_vol = parsed_min_vol or 0.0
            if not contract_symbol_matches:
                contract_problem = (
                    "contract metadata symbol does not match close symbol"
                )
            elif not contract_known:
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
        if mutation_started is not None:
            mutation_started.set()
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
            outcome_unknown = _is_uncertain_order_error(e)
            if outcome_unknown:
                try:
                    recovered = await self.client.order_by_external_oid(
                        symbol, recovery_oid
                    )
                except ExchangeError:
                    recovered = None
            recovery_matches = _recovery_is_match(
                recovered,
                recovery_oid,
                expected_symbol=symbol,
                allow_bare_base_alias=is_hl,
            )
            if (
                recovery_matches
                and not is_hl
                and not _mexc_recovery_matches_request(
                    recovered,
                    expected_side=4 if side == "long" else 2,
                    expected_vol=close_vol,
                    expected_type=MEXC_TYPE_MARKET,
                    expected_open_type=open_type,
                    expected_price=None,
                    expected_leverage=None,
                    expected_stop_loss=None,
                    expected_take_profit=None,
                )
            ):
                recovery_matches = False
            if (
                recovery_matches
                and is_hl
                and not _hl_recovery_matches_request(
                    recovered,
                    expected_side="A" if side == "long" else "B",
                    expected_vol=close_vol,
                    expected_reduce_only=True,
                )
            ):
                recovery_matches = False
            if recovery_matches:
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
                if outcome_unknown:
                    raise OrderOutcomeUnknown(
                        "Close outcome is unknown. The position may already have "
                        "changed; reconcile positions and open orders before retrying."
                    ) from e
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
            raise OrderRejectedByExchange(
                f"close rejected by exchange: {close_err} — position may still be "
                f"open; verify on the exchange{audit_suffix}"
            )
        public_response = _public_order_ack(
            resp, recovered=close_recovery_warning is not None
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
                "response": public_response,
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
                "response": public_response,
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
                "response": public_response,
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
            "response": public_response,
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
            self._require_active_client()
            mutation_started = asyncio.Event()
            return await self._run_locked_mutation(
                self._cancel_locked(
                    order_id=order_id,
                    symbol=symbol,
                    mutation_started=mutation_started,
                ),
                mutation_started=mutation_started,
            )

    async def _cancel_locked(
        self,
        *,
        order_id: str | int | None = None,
        symbol: str | None = None,
        mutation_started: asyncio.Event | None = None,
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

        validated_rows: list[tuple[dict[str, Any], int]] = []
        seen_order_ids: set[int] = set()
        for r in open_rows:
            if not _has_consistent_positive_order_id(r):
                raise OrderError(
                    "open orders response contains an invalid order identity "
                    "— cancel blocked"
                )
            id_key = next(
                key for key in ("orderId", "order_id", "oid") if key in r
            )
            rid = int(str(r.get(id_key)))
            if rid in seen_order_ids:
                raise OrderError(
                    "open orders response contains an invalid order identity "
                    "— cancel blocked"
                )
            seen_order_ids.add(rid)
            validated_rows.append((r, rid))

        matched = False
        for r, rid in validated_rows:
            if rid != oid:
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

        if mutation_started is not None:
            mutation_started.set()
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
            if _is_uncertain_order_error(e):
                raise OrderOutcomeUnknown(
                    "Cancel outcome is unknown. The order may already be cancelled or "
                    "filled; reconcile open orders before retrying."
                ) from e
            audit_suffix = f" ({audit_error})" if audit_error else ""
            raise OrderError(f"cancel failed: {e}{audit_suffix}") from e

        # An outwardly successful Hyperliquid response can still carry an
        # inner statuses[].error. Treat all exchange-shaped rejections as a
        # failed cancel; never report a false success to the operator.
        if not isinstance(resp, (dict, list)) or not resp:
            response_error = "unrecognized cancel response"
        else:
            response_error = _cancel_response_error(
                resp, expected_order_id=oid, is_hyperliquid=is_hl
            )
        cancel_ok = response_error is None
        detail = _public_order_ack(resp, order_id=oid)

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
            raise OrderRejectedByExchange(
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
                    seen_stop_ids: set[int] = set()
                    identities_valid = True
                    for stop in stops:
                        stop_id = _consistent_stop_order_id(stop)
                        trigger_price, trigger_fields = (
                            _consistent_positive_price_aliases(
                                stop, ("triggerPrice", "trigger_price")
                            )
                        )
                        reduce_only, reduce_only_valid = (
                            classify_reduce_only_fields(stop)
                        )
                        position_side, position_side_valid = (
                            classify_position_side_fields(stop)
                        )
                        explicit_price, explicit_fields = (
                            _consistent_positive_price_aliases(
                                stop, ("stopLossPrice", "stop_loss_price")
                            )
                        )
                        _label, labels_valid = classify_order_label_fields(stop)
                        stop_kind_valid = (
                            explicit_price is not None
                            if explicit_fields
                            else labels_valid
                        )
                        if (
                            stop_id is None
                            or stop_id in seen_stop_ids
                            or not _symbols_match(
                                stop.get("symbol"),
                                symbol,
                                allow_bare_base_alias=is_hl,
                            )
                            or not trigger_fields
                            or trigger_price is None
                            or not reduce_only_valid
                            or reduce_only is False
                            or not position_side_valid
                            or not stop_kind_valid
                        ):
                            identities_valid = False
                            break
                        seen_stop_ids.add(stop_id)
                    if identities_valid:
                        checked = True
                    else:
                        last_detail = (
                            "open stop orders contained an invalid identity or "
                            "trigger geometry"
                        )
                        stops = []
            except ExchangeError:
                last_detail = "open stop orders lookup failed"
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
        expected_position_signature: int | None = None,
        record_user_override: bool = False,
    ) -> dict[str, Any]:
        async with self._trade_lock:
            self._require_active_client()
            mutation_started = asyncio.Event()
            return await self._run_locked_mutation(
                self._modify_stop_loss_and_record_locked(
                    symbol=symbol,
                    side=side,
                    new_sl=new_sl,
                    required_armed_rule=required_armed_rule,
                    expected_position_signature=expected_position_signature,
                    record_user_override=record_user_override,
                    mutation_started=mutation_started,
                ),
                mutation_started=mutation_started,
            )

    async def _modify_stop_loss_and_record_locked(
        self,
        *,
        symbol: str,
        side: str,
        new_sl: float,
        required_armed_rule: str | None,
        expected_position_signature: int | None,
        record_user_override: bool,
        mutation_started: asyncio.Event,
    ) -> dict[str, Any]:
        result = await self._modify_stop_loss_locked(
            symbol=symbol,
            side=side,
            new_sl=new_sl,
            required_armed_rule=required_armed_rule,
            expected_position_signature=expected_position_signature,
            mutation_started=mutation_started,
        )
        if (
            record_user_override
            and result.get("verified") is True
            and self.db is not None
        ):
            try:
                await self.db.set_user_override_hw(symbol, side)
            except Exception as exc:
                log.warning(
                    "modify-sl: set_user_override_hw failed type=%s",
                    type(exc).__name__,
                )
                warnings = result.get("warnings")
                if not isinstance(warnings, list):
                    warnings = []
                    result["warnings"] = warnings
                warnings.append(
                    "Stop moved, but the manual override marker could not be "
                    "saved; automatic trailing may move it again."
                )
        return result

    async def _modify_stop_loss_locked(
        self,
        *,
        symbol: str,
        side: str,
        new_sl: float,
        required_armed_rule: str | None = None,
        expected_position_signature: int | None = None,
        mutation_started: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        if not self.settings.trading_enabled:
            raise OrderError(
                "DISARMED: TRADING_ENABLED=false — modify-SL blocked. "
                "Set TRADING_ENABLED=true in .env to arm live trading."
            )
        if (
            getattr(self.client, "exchange_id", "") != "hyperliquid"
            or not hasattr(self.client, "place_stop_order")
        ):
            raise OrderError("Moving the SL is only available on Hyperliquid")
        symbol = symbol.upper().strip()
        side = (side or "").lower()
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
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
            if required_armed_rule == "auto_trail":
                raw_override = (row or {}).get("user_override_hw")
                if raw_override is not None:
                    override = _coerce_float(raw_override)
                    high_water = _coerce_float((row or {}).get("high_water"))
                    if (
                        override is None
                        or override <= 0
                        or high_water is None
                        or high_water <= 0
                    ):
                        raise OrderError(
                            "auto_trail manual override state is invalid — "
                            "autonomous modify-SL blocked"
                        )
                    override_active = (
                        high_water <= override
                        if side == "long"
                        else high_water >= override
                    )
                    if override_active:
                        raise OrderError(
                            "auto_trail is paused by a manual stop override — "
                            "autonomous modify-SL cancelled"
                        )
            if (
                type(expected_position_signature) is not int
                or expected_position_signature <= 0
            ):
                raise OrderError(
                    "current position identity is unavailable — autonomous "
                    "modify-SL blocked"
                )
            stored_signature = (row or {}).get("open_sig")
            if (
                type(stored_signature) is not int
                or stored_signature <= 0
                or stored_signature != expected_position_signature
            ):
                raise OrderError(
                    "position identity changed in local state — autonomous "
                    "modify-SL cancelled"
                )
        if isinstance(new_sl, bool) or not isinstance(new_sl, (int, float)):
            raise OrderError("new_sl must be numeric")
        try:
            new_sl = float(new_sl)
        except OverflowError as exc:
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
        except ExchangeError as e:
            raise OrderError(f"ticker failed — modify-SL blocked: {e}") from e
        if not _symbols_match(
            getattr(ticker, "symbol", None),
            symbol,
            allow_bare_base_alias=is_hl,
        ):
            raise OrderError("ticker symbol does not match requested order symbol")
        try:
            mark = self._ticker_price(
                ticker,
                symbol,
                allow_bare_base_alias=is_hl,
            )
        except OrderError as e:
            raise OrderError(
                "mark price unavailable — modify-SL blocked (cannot validate geometry)"
            ) from e

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
            if not _symbols_match(
                getattr(contract, "symbol", None),
                symbol,
                allow_bare_base_alias=is_hl,
            ):
                raise OrderError(
                    "contract metadata symbol does not match requested stop symbol"
                )
            price_unit = _normalized_float(contract.price_unit)
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
        if not _symbols_match(
            getattr(latest_ticker, "symbol", None),
            symbol,
            allow_bare_base_alias=is_hl,
        ):
            raise OrderError("ticker symbol does not match requested order symbol")
        try:
            latest_mark = self._ticker_price(
                latest_ticker,
                symbol,
                allow_bare_base_alias=is_hl,
            )
        except OrderError as e:
            raise OrderError(
                "latest mark unavailable — modify-SL blocked; old SL left in place"
            ) from e
        if side == "long" and not (rounded_sl < latest_mark):
            raise OrderError(
                f"rounded long SL {rounded_sl} not below latest mark {latest_mark}"
            )
        if side == "short" and not (rounded_sl > latest_mark):
            raise OrderError(
                f"rounded short SL {rounded_sl} not above latest mark {latest_mark}"
            )

        # Recheck the exact trade epoch immediately before the first mutation.
        # A close+reopen can retain the same symbol, side and entry while this
        # request waits on the shared lock; applying the old baseline's stop to
        # that fresh position would be an unauthorized autonomous action.
        if required_armed_rule is not None:
            live_signature = await hl_epoch_signature(self.client, symbol, side)
            if live_signature is None:
                raise OrderError(
                    "current position identity could not be verified — "
                    "autonomous modify-SL blocked"
                )
            if live_signature != expected_position_signature:
                raise OrderError(
                    "position changed while waiting — autonomous modify-SL "
                    "cancelled"
                )

        # ── FAIL-SAFE STEP 1: place the NEW stop BEFORE removing the old one ──
        if mutation_started is not None:
            mutation_started.set()
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
            failure_detail = "exchange request failed"
            await self._audit_modify(
                symbol,
                side,
                rounded_sl,
                {"error": failure_detail},
                "modify_sl_place_failed",
                failure_detail,
            )
            if _is_uncertain_order_error(e):
                raise OrderOutcomeUnknown(
                    "New stop placement outcome is unknown. The old stop was not "
                    "removed; reconcile the position and stop orders before retrying."
                ) from e
            raise OrderError(
                "new SL placement failed — old SL left in place (still protected)"
            ) from e

        new_oid = _consistent_stop_order_id(placed)
        place_err = placed.get("error") if isinstance(placed, dict) else "unknown"
        if new_oid is None or place_err is not None:
            rejection_detail = (
                "exchange rejected the replacement stop"
                if place_err is not None
                else "invalid response"
            )
            await self._audit_modify(
                symbol,
                side,
                rounded_sl,
                {"orderId": new_oid, "error": rejection_detail},
                "modify_sl_place_rejected",
                rejection_detail,
            )
            raise OrderRejectedByExchange(
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
                    cancel_error = _cancel_response_error(
                        cancel_response,
                        expected_order_id=oid,
                        is_hyperliquid=is_hl,
                    )
                    if cancel_error:
                        raise OrderError(cancel_error)
                    cancelled.append(oid)
                except Exception:  # noqa: BLE001 — new stop is live; must not bubble
                    failed.append(oid)
                    warnings.append(
                        f"Old SL {oid} could not be cancelled and remains "
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


