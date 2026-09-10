"""Server-side order risk gates (G2–G6 + notional + unprotected SL).

Grok proposals never bypass these — every ticket is re-validated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.models import ContractMeta, OrderTicket
from app.risk.sizing import (
    adverse_market_entry,
    calc_rrr,
    risk_usdt,
    round_down_to_unit,
    round_to_unit,
    round_trigger_to_unit,
)


@dataclass
class GateResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rounded_vol: float = 0.0
    rounded_price: float | None = None
    rounded_stop: float | None = None
    rounded_tp: float | None = None
    risk_usdt: float = 0.0
    risk_pct: float = 0.0
    rrr: float | None = None
    entry_for_risk: float | None = None
    notional_usdt: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "rounded_vol": self.rounded_vol,
            "rounded_price": self.rounded_price,
            "rounded_stop": self.rounded_stop,
            "rounded_tp": self.rounded_tp,
            "risk_usdt": self.risk_usdt,
            "risk_pct": self.risk_pct,
            "rrr": self.rrr,
            "entry_for_risk": self.entry_for_risk,
            "notional_usdt": self.notional_usdt,
        }


def validate_order(
    ticket: OrderTicket,
    contract: ContractMeta,
    equity: float,
    settings: Settings,
    *,
    last_price: float | None = None,
    for_confirm: bool = False,
    existing_same_side_risk_usdt: float = 0.0,
    existing_same_side_warnings: list[str] | None = None,
    available_usdt: float | None = None,
    preview_last_price: float | None = None,
) -> GateResult:
    """Validate ticket against contract meta + risk settings.

    trading_enabled=false is always a hard error (preview and confirm) so
    tokens are never issued while disarmed.
    Equity <= 0 is always fail-closed (no soft skip of MAX_RISK_PCT).
    """
    errors: list[str] = []
    warnings: list[str] = []

    existing_risk_f = 0.0
    try:
        if isinstance(existing_same_side_risk_usdt, bool):
            raise ValueError
        candidate_existing_risk = float(existing_same_side_risk_usdt)
        if not math.isfinite(candidate_existing_risk) or candidate_existing_risk < 0:
            raise ValueError
        existing_risk_f = candidate_existing_risk
    except (TypeError, ValueError, OverflowError):
        errors.append("existing same-side risk is invalid (must be finite and non-negative)")

    # ── Arming switch (preview + confirm) ──────────────────────────────
    if not settings.trading_enabled:
        msg = (
            "DISARMED: TRADING_ENABLED=false — live place blocked. "
            "Set TRADING_ENABLED=true in .env to arm."
        )
        errors.append(msg)

    # ── Contract / apiAllowed ──────────────────────────────────────────
    if contract.api_allowed is not True:
        errors.append(
            f"Symbol {contract.symbol or ticket.symbol} does not have literal "
            "apiAllowed=true — API orders rejected"
        )
    if settings.exchange == "mexc" and (
        type(contract.state) is not int or contract.state != 0
    ):
        errors.append(
            f"Symbol {contract.symbol or ticket.symbol} has state={contract.state} "
            "— new entries require an enabled MEXC contract (state=0)"
        )

    def contract_float(field_name: str) -> float:
        raw_value = getattr(contract, field_name)
        if isinstance(raw_value, bool):
            errors.append(f"Invalid {field_name} from exchange meta")
            return float("nan")
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            errors.append(f"Invalid {field_name} from exchange meta")
            return float("nan")
        if not math.isfinite(value):
            errors.append(f"Invalid non-finite {field_name} from exchange meta")
            return float("nan")
        return value

    contract_size = contract_float("contract_size")
    price_unit = contract_float("price_unit")
    vol_unit = contract_float("vol_unit")
    min_vol = contract_float("min_vol")
    max_vol = contract_float("max_vol")
    min_notional = contract_float("min_notional")

    if math.isfinite(contract_size) and contract_size <= 0:
        errors.append("Invalid contract_size from exchange meta")

    for field_name, value in (
        ("vol_unit", vol_unit),
        ("min_vol", min_vol),
        ("max_vol", max_vol),
    ):
        if math.isfinite(value) and value <= 0:
            errors.append(f"Invalid {field_name} from exchange meta")
    for field_name, value in (
        ("price_unit", price_unit),
        ("min_notional", min_notional),
    ):
        if math.isfinite(value) and value < 0:
            errors.append(f"Invalid {field_name} from exchange meta")
    if max_vol > 0 and min_vol > max_vol:
        errors.append("Invalid min_vol/max_vol bounds from exchange meta")
    min_leverage = (
        contract.min_leverage if type(contract.min_leverage) is int else None
    )
    max_leverage = (
        contract.max_leverage if type(contract.max_leverage) is int else None
    )
    if (
        min_leverage is None
        or max_leverage is None
        or min_leverage < 1
        or max_leverage < 1
        or min_leverage > max_leverage
    ):
        errors.append("Invalid leverage bounds from exchange meta")

    # ── Side / type / open_type ────────────────────────────────────────
    side = ticket.side if type(ticket.side) is str else ""
    if side not in ("long", "short"):
        errors.append("side must be 'long' or 'short'")

    order_type = ticket.order_type if type(ticket.order_type) is str else ""
    if order_type not in ("market", "limit"):
        errors.append("order_type must be 'market' or 'limit'")

    # A Hyperliquid limit can fill (or continue filling) after this synchronous
    # request has returned. Without a durable fill watcher, that later exposure
    # cannot be guaranteed an attached stop. Fail closed until it can.
    if settings.exchange == "hyperliquid" and order_type == "limit":
        errors.append(
            "Hyperliquid limit entries are disabled: later/partial fills cannot "
            "be guaranteed an attached stop. Use a market entry."
        )

    # Manual SL/TP mode places NO exchange triggers (the trader manages exits),
    # so a missing take_profit is not a gate failure — the RRR check below warns
    # instead of hard-blocking (STRICT_RRR still applies to auto mode).
    trigger_mode = (
        ticket.trigger_mode.lower() if type(ticket.trigger_mode) is str else ""
    )
    if trigger_mode not in ("auto", "manual"):
        errors.append("trigger_mode must be 'auto' or 'manual'")
    manual_sltp = trigger_mode == "manual"

    if type(ticket.scale_out) is not bool:
        errors.append("scale_out must be a boolean")

    # R-04: block manual trigger_mode already in the GATE (preview + confirm),
    # not just at confirm time — a preview that will be refused on confirm
    # anyway must not issue a one-shot token. getattr default False is
    # fail-closed and mirrors the config.py default (see service.py's
    # confirm-time re-check, kept as defense-in-depth).
    if manual_sltp and not getattr(settings, "allow_manual_trigger", False):
        errors.append(
            "manual trigger_mode blocked (ALLOW_MANUAL_TRIGGER=false) — "
            "manual mode places no exchange SL/TP; use auto mode or enable "
            "ALLOW_MANUAL_TRIGGER to permit it."
        )

    open_type = ticket.open_type if type(ticket.open_type) is int else None
    if open_type not in (1, 2):
        errors.append("open_type must be 1 (isolated) or 2 (cross)")
    elif open_type == 2 and not settings.allow_cross_margin:
        errors.append(
            "cross margin (open_type=2) blocked — set ALLOW_CROSS_MARGIN=true to enable"
        )

    # ── G2 leverage ────────────────────────────────────────────────────
    leverage_valid = type(ticket.leverage) is int
    lev = ticket.leverage if leverage_valid else 0
    if not leverage_valid:
        errors.append("leverage must be an integer")
    elif lev < 1:
        errors.append("leverage must be >= 1")
    if leverage_valid and lev > settings.max_leverage:
        errors.append(
            f"leverage {lev} exceeds MAX_LEVERAGE={settings.max_leverage}"
        )
    if leverage_valid and max_leverage is not None and lev > max_leverage:
        errors.append(
            f"leverage {lev} exceeds contract maxLeverage={max_leverage}"
        )
    if leverage_valid and min_leverage is not None and lev < min_leverage:
        errors.append(
            f"leverage {lev} below contract minLeverage={min_leverage}"
        )

    # ── G6 precision: vol / price ──────────────────────────────────────
    volume_valid = True
    try:
        if isinstance(ticket.vol, bool):
            raise ValueError
        raw_vol = float(ticket.vol)
        if not math.isfinite(raw_vol):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        volume_valid = False
        raw_vol = 0.0
        errors.append("vol must be a finite number")
    if volume_valid and raw_vol <= 0:
        errors.append("vol must be > 0")

    if vol_unit > 0:
        rounded_vol = round_down_to_unit(raw_vol, vol_unit)
    else:
        rounded_vol = raw_vol

    if min_vol > 0 and rounded_vol < min_vol:
        errors.append(
            f"vol {rounded_vol} below minVol={min_vol} (after volUnit rounding)"
        )
    if max_vol > 0 and rounded_vol > max_vol:
        errors.append(f"vol {rounded_vol} above maxVol={max_vol}")
    if vol_unit > 0 and raw_vol > 0 and rounded_vol <= 0:
        errors.append(f"vol rounds to 0 with volUnit={vol_unit}")

    rounded_price: float | None = None
    limit_price_f: float | None = None
    if order_type == "limit":
        if ticket.price is not None and not isinstance(ticket.price, bool):
            try:
                candidate_limit_price = float(ticket.price)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if math.isfinite(candidate_limit_price) and candidate_limit_price > 0:
                    limit_price_f = candidate_limit_price
        if limit_price_f is None:
            errors.append("limit order requires price > 0")
        else:
            rounded_price = (
                round_to_unit(limit_price_f, price_unit)
                if price_unit > 0
                else limit_price_f
            )
            if rounded_price <= 0:
                errors.append("rounded limit price is invalid")

    # ── Entry reference for risk ───────────────────────────────────────
    last_price_f: float | None = None
    if last_price is not None and not isinstance(last_price, bool):
        try:
            candidate_last_price = float(last_price)
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            if math.isfinite(candidate_last_price) and candidate_last_price > 0:
                last_price_f = candidate_last_price

    ticket_entry_f: float | None = None
    if ticket.entry is not None:
        try:
            if isinstance(ticket.entry, bool):
                raise ValueError
            ticket_entry_f = float(ticket.entry)
            if not math.isfinite(ticket_entry_f):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            ticket_entry_f = None
            errors.append("ticket.entry must be a finite number")

    entry_for_risk: float | None = None
    if order_type == "market":
        if last_price_f is not None and side in ("long", "short"):
            try:
                entry_for_risk = adverse_market_entry(
                    last_price_f, side, settings.market_entry_slippage_pct
                )
            except ValueError as exc:
                errors.append(f"invalid market risk reference: {exc}")
        else:
            # Fail-closed: a MARKET order with no usable server-side reference
            # price (last_price None/<=0, e.g. a degraded ticker — service.py
            # warns this state at preview @~499 and confirm @~993) must NOT fall
            # back to the caller-controlled ticket.entry. Same distrust the limit
            # branch below documents: a spoofed entry nudged toward the SL would
            # understate MAX_RISK_PCT / RRR / notional while the REAL market order
            # fills at the true market. No server-side reference is plumbed into
            # the gate here (only last_price), so with it absent there is nothing
            # trustworthy to price risk against — block instead of warn. Preview
            # and confirm take this identical fail-closed path.
            errors.append(
                "market order needs a server-side reference price (last_price) "
                "for risk — degraded ticker, fail-closed (ticket.entry not trusted)"
            )
    else:
        # Limit risk MUST use the limit price — ticket.entry is not trusted
        # (spoofed entry closer to SL would understate MAX_RISK_PCT / pass bad geometry).
        if rounded_price is not None and float(rounded_price) > 0:
            entry_for_risk = float(rounded_price)
            if ticket_entry_f is not None and ticket_entry_f > 0:
                te = ticket_entry_f
                if abs(te - entry_for_risk) / max(entry_for_risk, 1e-12) * 100.0 > 0.05:
                    warnings.append(
                        f"ticket.entry {te} ignored for risk; using limit price "
                        f"{entry_for_risk}"
                    )
        elif limit_price_f is not None:
            entry_for_risk = limit_price_f
        else:
            errors.append("limit order needs price for risk calc")

    # ── Price drift (confirm vs preview) ───────────────────────────────
    preview_price_f: float | None = None
    if (
        for_confirm
        and preview_last_price is not None
        and not isinstance(preview_last_price, bool)
    ):
        try:
            candidate_preview_price = float(preview_last_price)
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            if math.isfinite(candidate_preview_price) and candidate_preview_price > 0:
                preview_price_f = candidate_preview_price

    if (
        for_confirm
        and preview_price_f is not None
        and last_price_f is not None
    ):
        prev = preview_price_f
        now = last_price_f
        drift_pct = abs(now - prev) / prev * 100.0
        try:
            raw_max_drift = getattr(settings, "max_price_drift_pct", 0.5)
            if isinstance(raw_max_drift, bool):
                raise ValueError
            max_drift = float(raw_max_drift)
            if not math.isfinite(max_drift) or not (0 <= max_drift <= 100):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            errors.append(
                "MAX_PRICE_DRIFT_PCT is invalid — expected a finite number "
                "in [0, 100]"
            )
        else:
            if drift_pct > max_drift + 1e-12:
                errors.append(
                    f"price drift {drift_pct:.3f}% exceeds MAX_PRICE_DRIFT_PCT="
                    f"{max_drift} (preview {prev} → now {now}) — re-preview"
                )
    elif for_confirm and preview_price_f is None:
        # The preview captured no usable baseline price (its ticker returned no
        # price), so the confirm-vs-preview drift check can't run. Surface it as a
        # warning instead of silently skipping — the one-time token's TTL bounds
        # the exposure window, but the human should re-preview on a large move.
        warnings.append(
            "Price drift cannot be checked: preview was created without a market "
            "price — create a new preview after a significant price move"
        )

    # ── SL required unless unprotected allowed ─────────────────────────
    sl = ticket.stop_loss
    rounded_stop: float | None = None
    sl_f: float | None = None
    invalid_sl = False
    if sl is not None:
        try:
            if isinstance(sl, bool):
                raise ValueError
            sl_f = float(sl)
            if not math.isfinite(sl_f):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            sl_f = None
            invalid_sl = True

    if invalid_sl:
        errors.append("stop_loss must be a finite number")
    elif sl_f is None or sl_f <= 0:
        if not settings.allow_unprotected_entry:
            errors.append(
                "stop_loss required (ALLOW_UNPROTECTED_ENTRY=false). "
                "Naked live entries are blocked."
            )
        else:
            warnings.append(
                "No stop_loss — unprotected entry allowed by config (dangerous)"
            )
        sl_f = None
    else:
        if entry_for_risk is not None and side in ("long", "short"):
            if side == "long" and sl_f >= entry_for_risk:
                errors.append("long stop_loss must be below entry")
            if side == "short" and sl_f <= entry_for_risk:
                errors.append("short stop_loss must be above entry")
        if price_unit > 0:
            # R-02: side-aware conservative rounding (long SL floors, short SL
            # ceils) so tick rounding never nudges the stop TOWARD entry —
            # nearest/half-even rounding could shift it up to 0.5 tick closer,
            # understating risk. The onto-/above-entry guard below still
            # stands as a second line of defense.
            sl_f = round_trigger_to_unit(sl_f, price_unit, side=side, kind="sl")
            if not math.isfinite(sl_f) or sl_f <= 0:
                errors.append("rounded stop_loss is invalid")
            # Tick rounding can flip a razor-thin stop onto the wrong side
            if entry_for_risk is not None and side in ("long", "short"):
                if side == "long" and sl_f >= entry_for_risk:
                    errors.append(
                        "stop_loss rounds onto/above entry (tick size) — widen the stop"
                    )
                if side == "short" and sl_f <= entry_for_risk:
                    errors.append(
                        "stop_loss rounds onto/below entry (tick size) — widen the stop"
                    )
        rounded_stop = sl_f

    rounded_tp: float | None = None
    tp = ticket.take_profit
    tp_f: float | None = None
    invalid_tp = False
    if tp is not None:
        try:
            if isinstance(tp, bool):
                raise ValueError
            tp_f = float(tp)
            if not math.isfinite(tp_f):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            tp_f = None
            invalid_tp = True
    if invalid_tp:
        errors.append("take_profit must be a finite number")
    elif tp_f is not None and tp_f > 0:
        rounded_tp = tp_f
        if price_unit > 0:
            # R-02: same conservative side-aware rounding for TP (long floors
            # toward entry, short ceils toward entry) so RRR is never
            # overstated by a nearest-rounding drift away from entry.
            rounded_tp = round_trigger_to_unit(rounded_tp, price_unit, side=side, kind="tp")

    # ── Equity fail-closed (G3 requires known equity) ──────────────────
    try:
        if equity is None or isinstance(equity, bool):
            raise ValueError
        equity_f = float(equity)
    except (TypeError, ValueError, OverflowError):
        equity_f = 0.0
    if not math.isfinite(equity_f) or equity_f <= 0:
        errors.append(
            "equity unknown/zero — fail-closed (MAX_RISK_PCT cannot be enforced)"
        )

    # ── G3 risk % (with slippage buffer on distance) ───────────────────
    risk_u = 0.0
    risk_p = 0.0
    if (
        sl_f is not None
        and math.isfinite(sl_f)
        and entry_for_risk is not None
        and math.isfinite(entry_for_risk)
        and rounded_vol > 0
        and contract_size > 0
    ):
        calculated_risk = risk_usdt(
            rounded_vol,
            contract_size,
            entry_for_risk,
            sl_f,
            slippage_pct=settings.risk_slippage_pct,
        )
        if not math.isfinite(calculated_risk):
            errors.append("calculated stop risk is not finite — fail-closed")
        else:
            risk_u = calculated_risk
            # Aggregate with existing same-side exposure on symbol.
            total_risk = risk_u + existing_risk_f
            if not math.isfinite(total_risk):
                errors.append("aggregate same-side risk is not finite — fail-closed")
            elif math.isfinite(equity_f) and equity_f > 0:
                calculated_risk_pct = (total_risk / equity_f) * 100.0
                if not math.isfinite(calculated_risk_pct):
                    errors.append("calculated risk percentage is not finite — fail-closed")
                else:
                    risk_p = calculated_risk_pct
                    if existing_risk_f > 0:
                        warnings.append(
                            f"includes existing same-side risk ~"
                            f"{existing_risk_f:.4f} USDT "
                            "(loss to each position's stop where known, else "
                            "full liquidation distance)"
                        )
                    # Preserve the estimator's stable warnings channel for callers.
                    if existing_same_side_warnings:
                        warnings.extend(existing_same_side_warnings)
                    # Defense-in-depth: config.py already rejects non-finite
                    # max_risk_pct, but guard here too so a NaN/Infinity that
                    # somehow slips through can't silently fail this gate open
                    # (risk_p > nan is always False).
                    if not math.isfinite(settings.max_risk_pct):
                        errors.append(
                            "MAX_RISK_PCT is not a finite number — refusing to "
                            "evaluate the risk gate (fail-closed)"
                        )
                    elif risk_p > settings.max_risk_pct + 1e-9:
                        errors.append(
                            f"risk {risk_p:.4f}% of equity exceeds MAX_RISK_PCT="
                            f"{settings.max_risk_pct} (incl. RISK_SLIPPAGE_PCT="
                            f"{settings.risk_slippage_pct}% buffer; fees not fully modeled)"
                        )

    # ── G4 RRR ─────────────────────────────────────────────────────────
    min_rrr_f: float | None = None
    try:
        if isinstance(settings.min_rrr, bool):
            raise ValueError
        min_rrr_f = float(settings.min_rrr)
        if not math.isfinite(min_rrr_f) or not (0 <= min_rrr_f <= 1000):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        min_rrr_f = None
        errors.append(
            "MIN_RRR is invalid — expected a finite number in [0, 1000]"
        )

    rrr: float | None = None
    if (
        sl_f is not None
        and math.isfinite(sl_f)
        and rounded_tp is not None
        and entry_for_risk is not None
        and math.isfinite(entry_for_risk)
        and side in ("long", "short")
    ):
        try:
            calculated_rrr = calc_rrr(
                side, entry_for_risk, sl_f, float(rounded_tp)
            )
            if not math.isfinite(calculated_rrr):
                raise ValueError("RRR is not finite")
            rrr = calculated_rrr
            if min_rrr_f is not None and rrr + 1e-12 < min_rrr_f:
                msg = f"RRR {rrr:.3f} < MIN_RRR={min_rrr_f}"
                if settings.strict_rrr:
                    errors.append(msg + " (STRICT_RRR=true)")
                else:
                    warnings.append(msg)
        except ValueError as e:
            errors.append(f"invalid RRR geometry: {e}")
    elif settings.strict_rrr and not manual_sltp and sl_f is not None and (
        rounded_tp is None or float(rounded_tp) <= 0
    ):
        errors.append(
            "take_profit required when STRICT_RRR=true (cannot enforce min RRR without TP)"
        )
    elif settings.strict_rrr and manual_sltp and sl_f is not None and (
        rounded_tp is None or float(rounded_tp) <= 0
    ):
        warnings.append(
            "MANUAL without TP: STRICT_RRR cannot check RRR — there is no "
            "exchange-side TP, so you manage the exit yourself."
        )

    # ── Max notional ───────────────────────────────────────────────────
    notional = 0.0
    if entry_for_risk is not None and rounded_vol > 0 and contract_size > 0:
        calculated_notional = rounded_vol * contract_size * entry_for_risk
        if not math.isfinite(calculated_notional):
            errors.append("calculated notional is not finite — fail-closed")
        else:
            notional = calculated_notional
            # Fixed USDT cap is a soft WARNING (does not scale with the account, so
            # it must not block "free size"). 0 = off. The real size guard is the
            # equity-relative cap below + risk-% + available-margin gates.
            if (
                settings.max_notional_usdt > 0
                and notional > settings.max_notional_usdt + 1e-9
            ):
                warnings.append(
                    f"Position value {notional:.2f} USDT exceeds MAX_NOTIONAL_USDT="
                    f"{settings.max_notional_usdt} (warning only, not a block)"
                )
            # HARD equity-relative fat-finger cap: notional <= equity × pct/100.
            # Scales with the account and stays fail-closed to known equity. 0 = off.
            pct_cap = float(
                getattr(settings, "max_notional_pct_of_equity", 0) or 0
            )
            # Defense-in-depth: guard against a non-finite pct_cap, which would
            # otherwise silently disable this equity-relative cap (any comparison
            # against NaN is False; cap computed from Infinity would never bind).
            if not math.isfinite(pct_cap):
                errors.append(
                    "MAX_NOTIONAL_PCT_OF_EQUITY is not a finite number — "
                    "refusing to evaluate the equity-relative notional gate "
                    "(fail-closed)"
                )
            elif pct_cap > 0 and math.isfinite(equity_f) and equity_f > 0:
                cap = equity_f * pct_cap / 100.0
                if notional > cap + 1e-9:
                    errors.append(
                        f"notional {notional:.2f} USDT exceeds {pct_cap:.0f}% of equity "
                        f"(cap {cap:.2f} USDT) — MAX_NOTIONAL_PCT_OF_EQUITY"
                    )
            if min_notional > 0 and notional + 1e-9 < min_notional:
                errors.append(
                    f"notional {notional:.4f} below exchange minimum "
                    f"{min_notional} — order would be rejected"
                )

    # ── Available margin ───────────────────────────────────────────────
    if available_usdt is None:
        available_f = None
    else:
        try:
            if isinstance(available_usdt, bool):
                raise ValueError
            available_f = float(available_usdt)
        except (TypeError, ValueError, OverflowError):
            available_f = float("nan")
    if available_f is not None and not math.isfinite(available_f):
        errors.append("available USDT is not finite — fail-closed")
    elif available_f is not None and available_f <= 0 and equity_f > 0:
        errors.append("available USDT is 0 — cannot open (margin fully used)")
    elif available_f is not None and notional > 0 and lev > 0:
        est_im = notional / float(lev)
        if available_f + 1e-9 < est_im:
            msg = (
                f"estimated IM ~{est_im:.2f} exceeds available "
                f"{available_f:.2f} USDT"
            )
            if getattr(settings, "strict_available_margin", True):
                errors.append(msg + " (STRICT_AVAILABLE_MARGIN=true)")
            else:
                warnings.append(msg)

    if risk_u > 0:
        warnings.append(
            "Risk excludes trading fees/funding; actual loss at SL can be higher"
        )

    ok = len(errors) == 0
    return GateResult(
        ok=ok,
        errors=errors,
        warnings=warnings,
        rounded_vol=rounded_vol if rounded_vol > 0 else 0.0,
        rounded_price=rounded_price,
        rounded_stop=rounded_stop,
        rounded_tp=rounded_tp,
        risk_usdt=risk_u,
        risk_pct=risk_p,
        rrr=rrr,
        entry_for_risk=entry_for_risk,
        notional_usdt=notional,
    )
