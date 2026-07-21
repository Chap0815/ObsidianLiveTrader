"""Order preview → confirm → cancel orchestration.

No place without unused, unexpired preview token AND TRADING_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import math
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _utc_now_iso_from_ts(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
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
    strict: bool = True,
    pos_risk_cap_pct: float = 2.0,
) -> tuple[float, list[str]]:
    """Open same-side risk (USDT), realistic per R-01.

    Per position, prefer the loss to its OWN stop-loss when present
    (``abs(entry - sl) * contract_size * vol``) — the loss-to-liquidation
    over-states risk massively and used to starve the aggregate MAX_RISK_PCT
    budget so nearly every add-on/second position got blocked. Without an SL,
    fall back to the liquidation distance but cap it at ``entry *
    pos_risk_cap_pct/100`` so an extremely wide liq can't dominate the budget.

    Missing ``liquidate_price``:
      * ``strict=False`` (default wiring): use a conservative fallback
        (``entry * pos_risk_cap_pct/100 * contract_size * vol``) and return a
        warning instead of hard-blocking every same-side trade.
      * ``strict=True``: fail-closed — raise ValueError (unknown exposure must
        never be treated as 0, which would understate MAX_RISK_PCT).

    Returns ``(total_risk_usdt, warnings)``. The function authors the warning
    itself because only it knows which position (symbol/side) triggered the
    fallback; call-sites just surface the returned messages.
    """
    total = 0.0
    warnings: list[str] = []
    cap_frac = max(0.0, pos_risk_cap_pct) / 100.0
    for raw in positions:
        p = raw if "hold_vol" in raw else map_position(raw)
        if str(p.get("symbol") or "").upper() != symbol.upper():
            continue
        if str(p.get("side") or "").lower() != side.lower():
            continue
        vol = float(p.get("hold_vol") or 0)
        entry = float(p.get("entry_price") or 0)
        if vol <= 0 or entry <= 0 or contract_size <= 0:
            continue
        sl = _position_sl_price(p)
        if sl is not None:
            # Loss to this position's own stop — the realistic exposure.
            total += abs(entry - sl) * contract_size * vol
            continue
        liq = p.get("liquidate_price")
        if liq is not None and float(liq) > 0:
            dist = min(abs(entry - float(liq)), entry * cap_frac)
            total += dist * contract_size * vol
        elif strict:
            raise ValueError(
                f"open {side} position on {symbol} has no liquidate_price — "
                "cannot enforce aggregate MAX_RISK_PCT (close or wait for liq data)"
            )
        else:
            # Non-strict: conservative fallback + warning instead of a block.
            total += entry * cap_frac * contract_size * vol
            warnings.append(
                f"open {side} position on {symbol} has no liquidate_price — "
                f"using conservative fallback risk (~{pos_risk_cap_pct:.2f}% "
                "of entry notional); set a stop or enable STRICT_AGGREGATE_RISK "
                "to hard-block instead."
            )
    return total, warnings


def _coerce_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _extract_filled_vol(resp: Any) -> float | None:
    """Filled quantity of THIS order from the exchange place response.

    Returns the exchange-reported fill (contracts/coins) for our order, or None
    when the response does not carry it. Used to bound auto-flatten to our OWN
    fill so a concurrent external bot that adds same-side volume in the same
    window is never partially closed by us.

    A key that is present but 0 is trusted as "reported unfilled" (returns 0.0)
    — that is the fail-closed choice: we would rather cancel a resting entry
    than market-close volume that may not be ours.
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
                return max(0.0, f)
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
                    total += fv
                    found = True
        if found:
            return max(0.0, total)
    return None


def _close_response_error(resp: Any) -> str | None:
    """Detect an inner rejection in a market-close response.

    Mirrors the Hyperliquid ``_status_error`` pattern but is exchange-agnostic:
    Hyperliquid returns order errors INSIDE an outwardly-200 response
    (``status != ok`` or a nested ``statuses[].error``), and MEXC surfaces
    ``success: false`` / a non-zero ``code``. Returning the error text here lets
    the close path report a FAILED close instead of a false ``closed``/``ok``.
    """
    if not isinstance(resp, dict):
        return None
    # MEXC-shaped rejections.
    if resp.get("success") is False:
        return str(resp.get("message") or resp.get("code") or "close not successful")
    code = resp.get("code")
    if code not in (None, 0, "0", 200, "200"):
        return f"code={code} {resp.get('message') or ''}".strip()
    # Hyperliquid-shaped rejections (same shape as client._status_error).
    status = resp.get("status")
    if status is not None and status != "ok":
        return str(status)
    try:
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        for st in statuses:
            if isinstance(st, dict) and "error" in st:
                return str(st["error"])
    except Exception:  # noqa: BLE001
        return None
    return None


def _recovery_is_match(recovered: Any, external_oid: str) -> bool:
    """True if `recovered` is trustworthy evidence our order is already live.

    O-05: accepts either an exchange-client MATCH MARKER — a dict carrying a
    truthy ``match`` field, which the client sets only after matching OUR
    oid/cloid (MEXC ``history``/``open`` are field-filtered, MEXC ``direct`` and
    HL ``cloid`` require the oid/cloid in the raw payload) — or, lacking a
    marker, a literal substring echo of our externalOid (covers the HL
    list-of-hits fallback and any legacy shape). Fail-closed: falsy input
    (``{}`` / ``[]`` / ``None``) returns False so the caller raises a hard error
    instead of blindly re-placing a possibly-live order.
    """
    if not recovered:
        return False
    if isinstance(recovered, dict) and recovered.get("match"):
        # Defense-in-depth: ein Marker mit FALSCHER externalOid (Client-Bug /
        # stale Cache) darf keine fremde Order als "recovered" ausgeben.
        # Valide Marker setzen externalOid=oid per Konstruktion; None bleibt
        # erlaubt (Marker-Shapes ohne das Feld).
        marker_oid = recovered.get("externalOid")
        return marker_oid is None or str(marker_oid) == str(external_oid)
    return str(external_oid) in str(recovered)


def _sl_matches(expected: float, candidate: float | None, tol_pct: float = 0.15) -> bool:
    if candidate is None or expected <= 0:
        return False
    c = float(candidate)
    if c <= 0:
        return False
    return abs(c - expected) / expected * 100.0 <= tol_pct


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
        return ["Scale-Out nur auf Hyperliquid verfügbar"]
    errs: list[str] = []
    tp1 = ticket.take_profit
    tp2 = ticket.tp2
    if tp1 is None or float(tp1) <= 0:
        errs.append("scale-out requires TP1 (take_profit)")
    if tp2 is None or float(tp2) <= 0:
        errs.append("scale-out requires TP2 (tp2)")
    share = float(getattr(ticket, "tp1_share", 0.5) or 0.0)
    if not (0.0 < share < 1.0):
        errs.append("tp1_share must be between 0 and 1")
    if errs:
        return errs
    side = (ticket.side or "").lower()
    e = float(entry) if entry else None
    if side == "long":
        if not (float(tp2) > float(tp1)):
            errs.append("long scale-out: TP2 must be above TP1")
        if e is not None and not (float(tp1) > e):
            errs.append("long scale-out: TP1 must be above entry")
    elif side == "short":
        if not (float(tp2) < float(tp1)):
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

    async def _balances(self) -> tuple[float, float]:
        """Return (equity, available). Fail-closed on API/mapping errors.

        fresh=True: this equity feeds order SIZING/gating, so it must never come
        from the 429 stale-serve cache (which could hand back optimistic-high
        equity during a volatile rate-limit burst and let an oversized order pass
        MAX_RISK_PCT). On a 429 here we fail closed instead."""
        try:
            assets = await self.client.assets(fresh=True)
            equity, available = usdt_balances(assets)
        except ExchangeError as e:
            raise OrderError(f"equity unavailable: {e}") from e
        if equity is None or float(equity) <= 0:
            raise OrderError(
                "equity unknown/zero — fail-closed (cannot enforce MAX_RISK_PCT)"
            )
        return float(equity), float(available)

    async def _existing_risk(
        self, symbol: str, side: str, contract_size: float
    ) -> tuple[float, list[str]]:
        """Same-side open risk + warnings. Positions API failure is fail-closed.

        `strict`/`pos_risk_cap_pct` flow from settings so Preview, Confirm and
        the sizing endpoint all use identical aggregate semantics (R-01).
        """
        try:
            # fresh=True: aggregate-exposure input to the risk gate — never the
            # 429 stale-serve cache (stale positions could understate open risk).
            positions = await self.client.positions(symbol, fresh=True)
        except ExchangeError as e:
            # Fail-closed: treating unknown exposure as 0 would understate MAX_RISK_PCT
            raise OrderError(
                f"positions unavailable — cannot enforce aggregate same-side risk: {e}"
            ) from e
        try:
            return estimate_same_side_risk_usdt(
                positions,
                symbol=symbol,
                side=side,
                contract_size=contract_size,
                strict=bool(getattr(self.settings, "strict_aggregate_risk", False)),
                pos_risk_cap_pct=float(
                    getattr(self.settings, "aggregate_pos_risk_cap_pct", 2.0)
                ),
            )
        except ValueError as e:
            raise OrderError(str(e)) from e

    async def preview(self, ticket: OrderTicket) -> dict[str, Any]:
        """Run gates, optionally issue one-time token + persist preview hash."""
        symbol = ticket.symbol.upper().strip()
        ticket = ticket.model_copy(update={"symbol": symbol})

        try:
            contract = await self.client.contract_meta(symbol)
        except ExchangeError as e:
            raise OrderError(f"contract meta failed: {e}") from e

        last_price: float | None = None
        try:
            ticker = await self.client.ticker(symbol)
            last_price = float(ticker.last_price) if ticker.last_price else None
        except ExchangeError as e:
            raise OrderError(f"ticker failed: {e}") from e

        try:
            equity, available = await self._balances()
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

        try:
            existing, existing_warnings = await self._existing_risk(
                symbol, ticket.side, contract.contract_size
            )
        except OrderError as e:
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
            expires = datetime.now(timezone.utc).timestamp() + ttl
            await self.db.insert_preview(
                token_hash=token_hash,
                payload_json=payload,
                expires_at=_utc_now_iso_from_ts(expires),
            )

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
        is_mexc = getattr(self.client, "exchange_id", "") == "mexc"
        saw_sl_field = False
        last_detail = ""
        attempts = max(1, int(getattr(self.settings, "sl_verify_attempts", 3)))
        delay_s = max(0.0, float(getattr(self.settings, "sl_verify_delay_s", 0.7)))
        for attempt in range(attempts):
            ambiguous_price_match = False

            # 1) Stop / plan orders — the authoritative SL source.
            try:
                stops = await self.client.open_stop_orders(symbol)
                stops_ever_ok = True
                for s in stops:
                    if not isinstance(s, dict):
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
                    if label == "tp":
                        continue
                    for key in (
                        "stopLossPrice",
                        "stop_loss_price",
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
                        saw_sl_field = True
                        if _sl_matches(expected_sl, s.get(key)):
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
                for raw in positions:
                    p = map_position(raw) if "hold_vol" not in raw else raw
                    if str(p.get("symbol") or "").upper() != symbol.upper():
                        continue
                    if str(p.get("side") or "").lower() != side.lower():
                        continue
                    for key in sl_keys:
                        val = None
                        if key in raw:
                            val = raw.get(key)
                        elif key in p:
                            val = p.get(key)
                        if val is not None:
                            saw_sl_field = True
                        if _sl_matches(expected_sl, val):
                            if pre_existing_same_side:
                                ambiguous_price_match = True
                                last_detail = (
                                    f"price-only match on position field {key} "
                                    "— cannot confirm it protects the newly added "
                                    "size (a same-side position pre-existed); not "
                                    "counted as verified"
                                )
                                continue
                            return True, f"position field {key} matched", True
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
        checked = saw_sl_field if is_mexc else stops_ever_ok
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
        """Return (hold_vol, open_type 1|2, checked) for same-side position.

        ``checked`` is True only if the positions query SUCCEEDED. On lookup
        failure it is False so callers can fail-closed instead of trusting the
        silent 0.0 — a fake "no pre-existing size" would let auto-flatten treat
        an OLD position as a fresh fill and market-close it.

        ``fresh`` forces a live positions read (bypassing the client's ~2s cache).
        The SL-modify path needs it: a reduce-only stop sized to a <=2s-stale hold
        under-covers a position that grew externally, and the modify then cancels
        the old (larger) stop → net under-protection.
        """
        try:
            positions = await self.client.positions(symbol, fresh=fresh)
        except ExchangeError:
            return 0.0, 1, False
        for raw in positions or []:
            p = map_position(raw) if "hold_vol" not in raw else raw
            if str(p.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(p.get("side") or "").lower() != side.lower():
                continue
            hv = float(p.get("hold_vol") or 0)
            ot_raw = p.get("open_type")
            if ot_raw in (2, "2", "cross"):
                ot = 2
            else:
                ot = 1
            return max(hv, 0.0), ot, True
        return 0.0, 1, True

    async def _mexc_pre_hold_and_position_id(
        self, symbol: str, side: str
    ) -> tuple[tuple[float, int, bool], int | None]:
        """MEXC-only: one positions read → (pre_hold triple, same-side positionId).

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
        pre_hold path; a missing/garbage id returns None → set_leverage uses the
        no-position form (correct when flat; if a position truly exists but its
        id was unreadable, MEXC still rejects and the order fails closed rather
        than placing silently mis-levered).
        """
        try:
            positions = await self.client.positions(symbol)
        except ExchangeError:
            return (0.0, 1, False), None
        for raw in positions or []:
            p = map_position(raw) if "hold_vol" not in raw else raw
            if str(p.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(p.get("side") or "").lower() != side.lower():
                continue
            hv = max(float(p.get("hold_vol") or 0), 0.0)
            ot = 2 if p.get("open_type") in (2, "2", "cross") else 1
            pid: int | None
            raw_pid = p.get("position_id")
            if raw_pid is None:
                pid = None
            else:
                try:
                    pid = int(raw_pid)
                except (TypeError, ValueError):
                    pid = None
            return (hv, ot, True), pid
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

        ticket = OrderTicket.model_validate(payload["ticket"])
        symbol = ticket.symbol.upper().strip()
        external_oid = str(payload.get("external_oid") or f"mlt-{uuid.uuid4().hex[:20]}")
        preview_last = payload.get("last_price")
        if preview_last is not None:
            preview_last = float(preview_last)

        try:
            contract = await self.client.contract_meta(symbol)
        except ExchangeError as e:
            raise OrderError(f"contract meta failed: {e}") from e

        last_price: float | None = None
        try:
            ticker = await self.client.ticker(symbol)
            if ticker.last_price:
                last_price = float(ticker.last_price)
        except ExchangeError as e:
            raise OrderError(f"ticker failed on confirm: {e}") from e

        equity, available = await self._balances()
        existing, existing_warnings = await self._existing_risk(
            symbol, ticket.side, contract.contract_size
        )

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
        # R-06: .lower() for consistency with gates.py:100 — a capitalized
        # trigger_mode (e.g. from a non-HTTP caller bypassing the Literal
        # validator) must be treated identically here and in the gate,
        # otherwise SL/TP-attach decisions could diverge from the gate's view.
        manual_sltp = (getattr(ticket, "trigger_mode", "auto") or "auto").lower() == "manual"

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
        # (Updated after M-B) For a market order the gate now validates
        # gate.rounded_stop's geometry and the risk MAGNITUDE against raw `last`
        # (the market_entry_slippage entry-shift was removed from risk in
        # app/risk/gates.py). The realised risk at an adverse fill can therefore
        # be slightly ABOVE the computed MAX_RISK_PCT (bounded by the exchange
        # slippage cap on the actual order) — a deliberate trade-off so tight-stop
        # scalps are not wrongly rejected. See the M-B note in gates.py.
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
        # The positionId comes from the SAME positions read that backs pre_hold
        # below (moved ahead of set_leverage on MEXC only), so this costs no
        # extra API call. HL is byte-for-byte unchanged: position_id stays None
        # (HL ignores it) and its pre_hold read keeps its original position.
        position_id: int | None = None
        pre_hold_precomputed: tuple[float, int, bool] | None = None
        if getattr(self.client, "exchange_id", "") == "mexc":
            pre_hold_precomputed, position_id = (
                await self._mexc_pre_hold_and_position_id(symbol, ticket.side)
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
        if pre_hold_precomputed is not None:
            pre_hold, _, pre_hold_ok = pre_hold_precomputed
        else:
            pre_hold, _, pre_hold_ok = await self._same_side_hold_vol_ok(
                symbol, ticket.side
            )

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
            err_l = str(e).lower()
            if any(
                x in err_l
                for x in (
                    "timeout",
                    "timed out",
                    "connect",
                    "network",
                    "invalid json",
                )
            ):
                try:
                    recovered = await self.client.order_by_external_oid(
                        symbol, external_oid
                    )
                except ExchangeError:
                    recovered = None
                # O-05: trust the exchange client's MATCH MARKER as the recovery
                # signal (MEXC "direct"/"history"/"open"; HL "cloid") — the client
                # sets it only after matching OUR oid/cloid, so a genuine fill is
                # not discarded merely because the raw provider payload omits the
                # oid string. Absent a marker, fall back to the literal-echo check;
                # a falsy result ({} / None) stays fail-closed (hard error below).
                if not _recovery_is_match(recovered, external_oid):
                    recovered = None
                # X2-03 positions-delta fallback. If the oid lookup can't confirm
                # the order (index-lag: absent from BOTH history and open → {}),
                # but the same-side hold has GROWN by ≈the ordered size since the
                # reliable pre_hold read, the order is almost certainly live. Emit
                # a "delta" marker so the caller treats it as "probably live"
                # (WARNING, fall-through to verify) instead of a hard error that
                # would bait the user into re-previewing → double position. This
                # marker satisfies _recovery_is_match by construction (match set,
                # externalOid==oid) and NEVER triggers a re-place — it only
                # suppresses the double-position. Fail-closed guards: MEXC only, a
                # RELIABLE pre_hold, and a genuine ≈rounded_vol delta; anything
                # short stays the hard error below.
                if (
                    recovered is None
                    and pre_hold_ok
                    and getattr(self.client, "exchange_id", "") == "mexc"
                ):
                    hold_now, _ot, hold_ok = await self._same_side_hold_vol_ok(
                        symbol, ticket.side
                    )
                    if hold_ok:
                        rvol = float(gate.rounded_vol)
                        vol_eps = max(rvol * 1e-4, 1e-9)
                        delta = max(0.0, hold_now - pre_hold)
                        if rvol > 0 and delta >= rvol - vol_eps:
                            recovered = {
                                "match": "delta",
                                "externalOid": external_oid,
                                "order": None,
                                "positionDelta": delta,
                            }
            if not recovered:
                if self.db is not None:
                    await self.db.insert_order(
                        symbol=symbol,
                        side=ticket.side,
                        request_json=body,
                        response_json={
                            "error": getattr(e, "raw", None),
                            "recovery": None,
                        },
                        status="error",
                        error=str(e),
                    )
                raise OrderError(
                    f"place_order failed: {e}. "
                    f"If timeout, check the exchange for externalOid={external_oid} "
                    "before retry (do not blind re-preview)."
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
            via_delta = isinstance(resp, dict) and resp.get("match") == "delta"
            evidence = (
                "position grew by ≈the ordered size (positions-delta signal; "
                "the order index had not caught up yet)"
                if via_delta
                else "order found by externalOid"
            )
            warnings.append(
                "place_order transport error but order recovered "
                f"({evidence}, externalOid={external_oid}). DO NOT re-preview — "
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
                "MANUELL: Kein Börsen-SL/TP platziert — du musst die Position "
                "selbst schließen. Bei geschlossenem Browser ist sie ungeschützt."
            )
            # F-F1: scale_out places NO exchange triggers in manual mode either
            # (attach_triggers=False covers TP1/TP2 too) — surface that the
            # configured TP ladder was silently skipped, not just the SL.
            if getattr(ticket, "scale_out", False):
                warnings.append(
                    "SCALE-OUT IGNORIERT: trigger_mode=manual platziert keine "
                    "Börsen-Trigger — die TP-Staffel (TP1/TP2) wurde NICHT "
                    "gesetzt. Du musst die Teilausstiege selbst verwalten."
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
                "LIMIT RUHT: Einstieg noch nicht ausgeführt — es ist KEIN "
                "Börsen-SL gesetzt, bis die Order füllt (kein Fill-Watcher). "
                "Nach dem Fill selbst absichern oder die ruhende Order stornieren."
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
                        fill_desc = "Order-Fill per externalOid bestätigt"
                    if body_had_sl and fill_is_ours:
                        sl_verified = True
                        sl_checked = True
                        sl_detail = (
                            "MEXC SL positionsgebunden — im Create-Body atomar "
                            "akzeptiert und beim gefüllten Entry aktiv "
                            f"({fill_desc}); kein separater Plan-Order sichtbar"
                        )
                        warnings.append(
                            "INFO: MEXC-SL ist positionsgebunden (kein separater "
                            "Plan-Order) und beim gefüllten Entry aktiv."
                        )

                if not sl_verified and not sl_checked:
                    # UNKNOWN is not the same as MISSING: never flatten blind,
                    # or a broken lookup endpoint closes every protected trade.
                    warnings.append(
                        f"SL-Status UNBEKANNT ({sl_detail}) — Verifikation nicht "
                        "möglich. Position auf der Börse manuell prüfen!"
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
                "Post-Place-SL-Prüfung abgestürzt — Order IST platziert. "
                "SL/Position jetzt manuell auf der Börse kontrollieren!"
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
                    "TEILGEFÜLLT: Limit-Order nur teilweise ausgeführt "
                    f"({float(filled):g} von {requested:g}). Der Börsen-SL deckt "
                    "NUR den gefüllten Teil — der ruhende Rest ist UNGESCHÜTZT, "
                    "falls er später ausgeführt wird. Rest manuell überwachen, "
                    "absichern oder die ruhende Order stornieren."
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
                        "AUTO_FLATTEN übersprungen: Vor-Handels-Menge unbekannt "
                        "(positions-Abfrage fehlgeschlagen) und keine Fill-Menge "
                        "in der Order-Antwort — es wird NICHTS geschlossen. "
                        "Position und SL JETZT manuell auf der Börse prüfen."
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
                        "AUTO_FLATTEN übersprungen: Nach-Handels-Menge nicht "
                        "lesbar (positions-Abfrage fehlgeschlagen) und keine "
                        "Fill-Menge in der Order-Antwort — es wird NICHTS "
                        "geschlossen und NICHTS als 'kein Fill' storniert. "
                        "Position und SL JETZT manuell auf der Börse prüfen."
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
                    for coid in cancel_ids:
                        try:
                            if is_hl:
                                await self.client.cancel_order(
                                    [{"orderId": coid, "symbol": symbol}]
                                )
                            else:
                                await self.client.cancel_order([coid])
                            cancelled.append(coid)
                        except Exception:  # noqa: BLE001
                            pass
                    flatten_result = {
                        "action": "cancel_resting",
                        "cancelled": cancelled,
                        "new_fill": 0.0,
                        "pre_hold": pre_hold,
                    }
                    if cancelled:
                        warnings.append(
                            "AUTO_FLATTEN: no fill yet — cancelled resting "
                            f"entry order(s) {cancelled} (pre-existing "
                            f"hold {pre_hold} left untouched)"
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
                    flatten_result = await self.client.close_position_market(
                        symbol,
                        side=ticket.side,
                        vol=vol,
                        open_type=pos_open_type or open_type,
                        external_oid=external_oid,
                    )
                    warnings.append(
                        f"AUTO_FLATTEN: market close vol={vol} "
                        f"(new_fill={new_fill} via {fill_source}, "
                        f"pre_hold={pre_hold}) after unverified SL"
                    )
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
                                "AUTO_FLATTEN: Rest-Position nicht nachprüfbar "
                                "(positions-Abfrage fehlgeschlagen) — Position auf "
                                "der Börse prüfen (evtl. Teilausführung)."
                            )
                        elif resid - pre_hold > eps:
                            warnings.append(
                                f"AUTO_FLATTEN UNVOLLSTÄNDIG: Rest {resid} offen "
                                f"(erwartet ~{pre_hold}) — Teilausführung, Position "
                                "manuell schließen/prüfen."
                            )
                            if isinstance(flatten_result, dict):
                                flatten_result = {
                                    **flatten_result,
                                    "residual_vol": resid,
                                    "incomplete": True,
                                }
                    except Exception:  # noqa: BLE001 — order live, must not bubble
                        warnings.append(
                            "AUTO_FLATTEN: Rest-Prüfung fehlgeschlagen — Position "
                            "auf der Börse prüfen."
                        )
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

        # Audit log must survive its own failures too
        try:
            if self.db is not None:
                await self.db.insert_order(
                    symbol=symbol,
                    side=ticket.side,
                    request_json=body,
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
        except Exception as e:  # noqa: BLE001
            post_errors.append(f"audit log failed: {e}")

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

        try:
            positions = await self.client.positions(symbol)
        except ExchangeError as e:
            raise OrderError(f"positions lookup failed: {e}") from e

        # Exact symbol match; base-coin fallback ONLY on Hyperliquid (bare
        # coins) — on MEXC BTC_USDT and BTC_USDC must never match each other.
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        hold = 0.0
        open_type = 1
        for raw in positions:
            p = map_position(raw) if "hold_vol" not in raw else raw
            psym = str(p.get("symbol") or "").upper()
            sym_match = psym == symbol or (
                is_hl and psym.split("_")[0] == symbol.split("_")[0]
            )
            if sym_match and str(p.get("side") or "").lower() == side:
                hold = float(p.get("hold_vol") or 0)
                ot = p.get("open_type")
                if ot in (2, "2", "cross"):
                    open_type = 2
                break
        if hold <= 0:
            raise OrderError(f"no open {side} position on {symbol}")

        # Fraction is applied to the CURRENT hold; vol is capped at hold.
        if fraction is not None and float(fraction) > 0:
            f = min(1.0, float(fraction))
            close_vol = hold * f
        elif vol and float(vol) > 0:
            close_vol = min(float(vol), hold)
        else:
            close_vol = hold

        # Round to the exchange lot step so a partial close is not rejected.
        try:
            contract = await self.client.contract_meta(symbol)
            vol_unit = float(contract.vol_unit or 0)
            min_vol = float(contract.min_vol or 0)
        except ExchangeError:
            vol_unit = 0.0
            min_vol = 0.0
        # Never round a full close down (would leave dust); only partials.
        is_full = close_vol >= hold - 1e-12
        if not is_full and vol_unit > 0:
            close_vol = round_down_to_unit(close_vol, vol_unit)
        if close_vol <= 0 or (not is_full and min_vol > 0 and close_vol < min_vol):
            raise OrderError(
                f"close amount {close_vol} below exchange minimum {min_vol} — "
                "choose a larger share or close the full position"
            )

        # O-09 TOCTOU: the positions() read above happened BEFORE the
        # contract_meta() yield, so the live side could have flipped or closed in
        # that window. Unlike Hyperliquid (whose client re-verifies the live side
        # itself), MEXC's close_position_market sends whatever side we pass — so
        # re-read the live same-side hold immediately before sending and refuse on
        # flip/disappearance rather than market-close the wrong side.
        if not is_hl:
            live_hold, _lot, live_ok = await self._same_side_hold_vol_ok(symbol, side)
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
            # X2-07: the position may have SHRUNK between the stale positions()
            # read used to size close_vol and this fresh recheck. Re-apply the
            # close fraction to the live hold (or clamp an absolute/full vol to
            # it) so a shrunk external position never receives an oversized
            # close. No-op when the position is unchanged. HL clamps client-side.
            if fraction is not None and float(fraction) > 0:
                close_vol = min(close_vol, live_hold * min(1.0, float(fraction)))
            else:
                close_vol = min(close_vol, live_hold)
            # Keep the (possibly reduced) partial lot-aligned; never round a full
            # close down into dust.
            is_full = close_vol >= live_hold - 1e-12
            if not is_full and vol_unit > 0:
                close_vol = round_down_to_unit(close_vol, vol_unit)
            # Mirror the pre-shrink min_vol gate: after re-clamping to the shrunk
            # live hold a partial can drop below the exchange minimum. Reject it
            # HERE (fail-closed, no exchange round-trip) with the same clear
            # message as the pre-shrink path, instead of shipping a sub-minimum
            # order that the exchange bounces with a confusing native error.
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
        try:
            resp = await self.client.close_position_market(
                symbol,
                side=side,
                vol=close_vol,
                open_type=open_type,
                external_oid=close_oid,
            )
        except ExchangeError as e:
            if self.db is not None:
                await self.db.insert_order(
                    symbol=symbol,
                    side=side,
                    request_json={"action": "manual_close", "vol": close_vol},
                    response_json=getattr(e, "raw", None),
                    status="close_error",
                    error=str(e),
                )
            raise OrderError(f"close failed: {e}") from e

        # F-03: a transport-200 response can still carry an INNER rejection
        # (Hyperliquid nests errors inside statuses[]). Semantically check it —
        # otherwise we log `closed` / answer ok while the position is still open.
        close_err = _close_response_error(resp)
        if close_err:
            if self.db is not None:
                await self.db.insert_order(
                    symbol=symbol,
                    side=side,
                    request_json={"action": "manual_close", "vol": close_vol},
                    response_json=resp if isinstance(resp, dict) else {"data": resp},
                    status="close_error",
                    error=close_err,
                )
            raise OrderError(
                f"close rejected by exchange: {close_err} — position may still be "
                "open; verify on the exchange"
            )

        # F-03 FOLLOW-UP: the inner-error check above only proves the exchange
        # ACCEPTED the close — a marketable IOC can still PARTIALLY fill (a
        # `filled` is present, no inner error) and leave a residual position
        # open. Re-read the live position and confirm the residual is what we
        # intended to leave (0 for a full close, hold-close_vol for a partial).
        expected_residual = max(0.0, hold - close_vol)
        # Dust tolerance: one lot step, else a tiny relative epsilon (HL sizes
        # can be stepless). Never flags a genuine full close (residual ~0).
        epsilon = max(vol_unit, hold * 1e-4, 1e-9)
        # M-A: a correct close can read back as still-open if the exchange
        # (Hyperliquid) has not reflected the fill the instant we re-read,
        # mislabelling a clean close as "partial". Re-read up to
        # CLOSE_VERIFY_ATTEMPTS times with a short settle delay, stopping as soon
        # as the residual has settled to what we intended to leave. Defaults to a
        # single attempt with no delay, so behaviour is unchanged unless the user
        # opts in via config.
        attempts = max(1, int(getattr(self.settings, "close_verify_attempts", 1) or 1))
        delay = max(0.0, float(getattr(self.settings, "close_verify_delay_s", 0.0) or 0.0))
        residual, _rot, reread_ok = await self._same_side_hold_vol_ok(symbol, side)
        for _ in range(attempts - 1):
            # Only keep polling while the position still looks unsettled; a good
            # read (settled residual) or a failed query ends the loop immediately.
            if not reread_ok or residual - expected_residual <= epsilon:
                break
            if delay > 0:
                await asyncio.sleep(delay)
            residual, _rot, reread_ok = await self._same_side_hold_vol_ok(symbol, side)

        if not reread_ok:
            # Fail-safe: the verification query failed. Do NOT claim fully
            # closed — surface that completion could not be confirmed.
            warn = (
                "Schließen gesendet, aber Positions-Nachprüfung fehlgeschlagen — "
                "Status UNBEKANNT. Position JETZT manuell auf der Börse prüfen "
                "(evtl. Teilausführung)."
            )
            if self.db is not None:
                await self.db.insert_order(
                    symbol=symbol,
                    side=side,
                    request_json={"action": "manual_close", "vol": close_vol},
                    response_json=resp if isinstance(resp, dict) else {"data": resp},
                    status="close_unverified",
                    error="post-close position reread failed",
                )
            return {
                "ok": False,
                "status": "close_unverified",
                "closed_vol": close_vol,
                "hold_vol": hold,
                "residual_vol": None,
                "verified": False,
                "response": resp,
                "warnings": [warn],
            }

        if residual - expected_residual > epsilon:
            # PARTIAL fill: a meaningful residual beyond what we meant to leave
            # is still open. Do NOT report fully closed.
            warn = (
                f"TEILAUSFÜHRUNG: Schließen von {close_vol} gesendet, aber "
                f"{residual} bleiben offen (erwartet ~{expected_residual}). "
                "Position ist NICHT vollständig geschlossen — Rest manuell "
                "schließen/prüfen."
            )
            if self.db is not None:
                await self.db.insert_order(
                    symbol=symbol,
                    side=side,
                    request_json={"action": "manual_close", "vol": close_vol},
                    response_json=resp if isinstance(resp, dict) else {"data": resp},
                    status="close_incomplete",
                    error=f"residual {residual} remains after close",
                )
            return {
                "ok": False,
                "status": "partial",
                "closed_vol": close_vol,
                "hold_vol": hold,
                "residual_vol": residual,
                "verified": False,
                "response": resp,
                "warnings": [warn],
            }

        if self.db is not None:
            await self.db.insert_order(
                symbol=symbol,
                side=side,
                request_json={"action": "manual_close", "vol": close_vol},
                response_json=resp if isinstance(resp, dict) else {"data": resp},
                status="closed",
                error=None,
            )
        return {
            "ok": True,
            "status": "closed",
            "closed_vol": close_vol,
            "hold_vol": hold,
            "residual_vol": residual,
            "verified": True,
            "response": resp,
        }

    async def cancel(
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

        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        if is_hl and not symbol:
            raise OrderError("symbol required for Hyperliquid cancel")

        oid: Any = int(order_id) if str(order_id).isdigit() else order_id

        try:
            open_rows = await self.client.open_orders(symbol)
        except ExchangeError as e:
            raise OrderError(
                f"open orders lookup failed — cancel blocked: {e}"
            ) from e

        matched = False
        for r in open_rows or []:
            if not isinstance(r, dict):
                continue
            rid = r.get("orderId")
            if rid is None:
                rid = r.get("order_id")
            if rid is None:
                rid = r.get("oid")
            if rid is None:
                continue
            if str(rid) != str(oid):
                continue
            if symbol:
                rsym = str(r.get("symbol") or "").upper()
                want = symbol.upper()
                if rsym and rsym != want:
                    if not (
                        is_hl
                        and rsym.split("_")[0] == want.split("_")[0]
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
            if self.db is not None:
                await self.db.insert_order(
                    symbol=symbol or "",
                    side=None,
                    request_json={"orderId": order_id, "symbol": symbol},
                    response_json=getattr(e, "raw", None),
                    status="cancel_error",
                    error=str(e),
                )
            raise OrderError(f"cancel failed: {e}") from e

        # Batch cancel may return per-id errors
        cancel_ok = True
        detail = resp
        if isinstance(resp, list):
            for item in resp:
                if isinstance(item, dict) and item.get("errorCode") not in (None, 0, "0"):
                    cancel_ok = False
        elif isinstance(resp, dict) and resp.get("success") is False:
            cancel_ok = False

        if self.db is not None:
            await self.db.insert_order(
                symbol=symbol or "",
                side=None,
                request_json={"orderId": order_id, "symbol": symbol},
                response_json=resp if isinstance(resp, (dict, list)) else {"data": resp},
                status="cancelled" if cancel_ok else "cancel_partial_error",
                error=None if cancel_ok else "per-id cancel error in response",
            )
        return {"ok": cancel_ok, "response": detail}

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
        most_protective, _tp = classify_protection(stops or [], side=side)
        is_hl = getattr(self.client, "exchange_id", "") == "hyperliquid"
        out: list[Any] = []
        for s in stops or []:
            if not isinstance(s, dict):
                continue
            psym = str(s.get("symbol") or "").upper()
            if psym and psym != symbol.upper():
                if not (is_hl and psym.split("_")[0] == symbol.split("_")[0]):
                    continue
            kind = str(
                s.get("orderType") or s.get("tpsl") or s.get("type") or ""
            ).lower()
            # Q-05: gleiche Label-Regel wie Verify/Reevaluate (protection.py) —
            # drittes Inline-Duplikat entfernt, damit die Stellen nie driften.
            if classify_order_label(kind) == "tp":
                continue  # only SL is being replaced; keep any TP
            oid = s.get("orderId") or s.get("oid")
            if oid is None and isinstance(s.get("raw"), dict):
                oid = s["raw"].get("oid")
            if oid is not None:
                out.append(oid)
        return out, most_protective

    async def _verify_sl_oid(
        self, symbol: str, new_oid: Any, expected_sl: float
    ) -> tuple[bool, str, bool]:
        """Confirm the concrete new_oid is resting as a stop order on the exchange.

        OID match is the PRIMARY verify condition. A price-only match is unsafe
        when the SL step is smaller than the price tolerance (~0.15%): the OLD
        stop alone could satisfy a price check, so we would cancel it while the
        NEW stop might not rest → unprotected. Gating on the concrete new_oid
        avoids this. A price match is recorded as optional additional evidence.

        Retries because a freshly placed trigger reflects with a short delay.
        Returns (verified, detail, checked). `checked` is True iff the stop-order
        lookup succeeded at least once, so a broken endpoint never yields a false
        "gone" that would trip a cancel.
        """
        attempts = max(1, int(getattr(self.settings, "sl_verify_attempts", 3)))
        delay_s = max(0.0, float(getattr(self.settings, "sl_verify_delay_s", 0.7)))
        checked = False
        last_detail = f"new SL oid {new_oid} not found among open stop orders"
        for attempt in range(attempts):
            try:
                stops = await self.client.open_stop_orders(symbol)
                checked = True
            except ExchangeError as e:
                last_detail = str(e)
                stops = None
            for s in stops or []:
                if not isinstance(s, dict):
                    continue
                oid = s.get("orderId") or s.get("oid")
                if oid is None and isinstance(s.get("raw"), dict):
                    oid = s["raw"].get("oid")
                if oid is None or str(oid) != str(new_oid):
                    continue
                price_ok = _sl_matches(
                    expected_sl,
                    s.get("triggerPrice") or s.get("trigger_price"),
                )
                detail = f"new SL oid {new_oid} resting" + (
                    " (price matched)" if price_ok else ""
                )
                return True, detail, True
            if attempt < attempts - 1 and delay_s > 0:
                await asyncio.sleep(delay_s)
        return False, last_detail, checked

    async def _audit_modify(
        self, symbol, side, new_sl, response_json, status, error
    ) -> None:
        if self.db is None:
            return
        try:
            await self.db.insert_order(
                symbol=symbol,
                side=side,
                request_json={"action": "modify_sl", "new_sl": new_sl},
                response_json=response_json
                if isinstance(response_json, (dict, list))
                else {"data": str(response_json)},
                status=status,
                error=error,
            )
        except Exception:  # noqa: BLE001 — audit must never break the flow
            pass

    async def modify_stop_loss(
        self, *, symbol: str, side: str, new_sl: float
    ) -> dict[str, Any]:
        async with self._trade_lock:
            return await self._modify_stop_loss_locked(
                symbol=symbol, side=side, new_sl=new_sl
            )

    async def _modify_stop_loss_locked(
        self, *, symbol: str, side: str, new_sl: float
    ) -> dict[str, Any]:
        if not self.settings.trading_enabled:
            raise OrderError(
                "DISARMED: TRADING_ENABLED=false — modify-SL blocked. "
                "Set TRADING_ENABLED=true in .env to arm live trading."
            )
        if not hasattr(self.client, "place_stop_order"):
            raise OrderError("SL nachziehen ist nur auf Hyperliquid verfügbar")
        symbol = symbol.upper().strip()
        side = (side or "").lower()
        if side not in ("long", "short"):
            raise OrderError("side must be 'long' or 'short'")
        new_sl = float(new_sl)
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
            mark = float(ticker.last_price) if ticker.last_price else None
        except ExchangeError as e:
            raise OrderError(f"ticker failed — modify-SL blocked: {e}") from e
        if mark is None or mark <= 0:
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
            price_unit = float(contract.price_unit or 0)
        except ExchangeError:
            price_unit = 0.0
        rounded_sl = (
            round_trigger_to_unit(new_sl, price_unit, side=side, kind="sl")
            if price_unit > 0
            else new_sl
        )
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

        new_oid = placed.get("orderId") if isinstance(placed, dict) else None
        place_err = placed.get("error") if isinstance(placed, dict) else "unknown"
        if new_oid is None:
            await self._audit_modify(
                symbol, side, rounded_sl, placed,
                "modify_sl_place_rejected", str(place_err),
            )
            raise OrderError(
                "new SL rejected by exchange — old SL left in place "
                f"(still protected): {place_err}"
            )

        # ── STEP 2: VERIFY the concrete new_oid is really resting (by OID, not
        # by price — the old, price-close stop must never count as the new). ──
        verified, detail, checked = await self._verify_sl_oid(
            symbol, new_oid, rounded_sl
        )

        warnings: list[str] = []
        cancelled: list[Any] = []
        failed: list[Any] = []

        if not verified:
            # New stop unconfirmed. NEVER cancel the old one on doubt — keeping
            # both (or old only) is over-protected, never unprotected.
            warnings.append(
                f"neuer SL platziert (oid={new_oid}), aber NICHT verifiziert "
                f"({detail}) — alter SL NICHT entfernt. Beide Stops auf der "
                "Börse prüfen."
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
                        await self.client.cancel_order(
                            [{"orderId": oid, "symbol": symbol}]
                        )
                    else:
                        await self.client.cancel_order([oid])
                    cancelled.append(oid)
                except Exception as e:  # noqa: BLE001 — new stop is live; must not bubble
                    failed.append(oid)
                    warnings.append(
                        f"alter SL {oid} konnte nicht gecancelt werden ({e}) — er "
                        "bleibt aktiv. Position ist ÜBER-geschützt (zwei Stops), "
                        "NICHT ungeschützt; alten Stop manuell auf der Börse entfernen."
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


