"""Server-side order risk gates (G2–G6 + notional + unprotected SL).

Grok proposals never bypass these — every ticket is re-validated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.models import ContractMeta, OrderTicket
from app.risk.sizing import calc_rrr, risk_usdt, round_down_to_unit, round_to_unit


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

    # ── Arming switch (preview + confirm) ──────────────────────────────
    if not settings.trading_enabled:
        msg = (
            "DISARMED: TRADING_ENABLED=false — live place blocked. "
            "Set TRADING_ENABLED=true in .env to arm."
        )
        errors.append(msg)

    # ── Contract / apiAllowed ──────────────────────────────────────────
    if not contract.api_allowed:
        errors.append(
            f"Symbol {contract.symbol or ticket.symbol} has apiAllowed=false — API orders rejected"
        )

    if contract.contract_size <= 0:
        errors.append("Invalid contract_size from exchange meta")

    # ── Side / type / open_type ────────────────────────────────────────
    side = (ticket.side or "").lower()
    if side not in ("long", "short"):
        errors.append("side must be 'long' or 'short'")

    order_type = (ticket.order_type or "").lower()
    if order_type not in ("market", "limit"):
        errors.append("order_type must be 'market' or 'limit'")

    # Manual SL/TP mode places NO exchange triggers (the trader manages exits),
    # so a missing take_profit is not a gate failure — the RRR check below warns
    # instead of hard-blocking (STRICT_RRR still applies to auto mode).
    manual_sltp = (getattr(ticket, "trigger_mode", "auto") or "auto").lower() == "manual"

    open_type = int(ticket.open_type or 1)
    if open_type not in (1, 2):
        errors.append("open_type must be 1 (isolated) or 2 (cross)")
    elif open_type == 2 and not settings.allow_cross_margin:
        errors.append(
            "cross margin (open_type=2) blocked — set ALLOW_CROSS_MARGIN=true to enable"
        )

    # ── G2 leverage ────────────────────────────────────────────────────
    lev = int(ticket.leverage)
    if lev < 1:
        errors.append("leverage must be >= 1")
    if lev > settings.max_leverage:
        errors.append(
            f"leverage {lev} exceeds MAX_LEVERAGE={settings.max_leverage}"
        )
    if contract.max_leverage and lev > contract.max_leverage:
        errors.append(
            f"leverage {lev} exceeds contract maxLeverage={contract.max_leverage}"
        )
    if contract.min_leverage and lev < contract.min_leverage:
        errors.append(
            f"leverage {lev} below contract minLeverage={contract.min_leverage}"
        )

    # ── G6 precision: vol / price ──────────────────────────────────────
    raw_vol = float(ticket.vol)
    if raw_vol <= 0:
        errors.append("vol must be > 0")

    vol_unit = float(contract.vol_unit or 0)
    min_vol = float(contract.min_vol or 0)
    max_vol = float(contract.max_vol or 0)

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
    price_unit = float(contract.price_unit or 0)
    if order_type == "limit":
        if ticket.price is None or float(ticket.price) <= 0:
            errors.append("limit order requires price > 0")
        else:
            raw_px = float(ticket.price)
            rounded_price = (
                round_to_unit(raw_px, price_unit) if price_unit > 0 else raw_px
            )
            if rounded_price <= 0:
                errors.append("rounded limit price is invalid")

    # ── Entry reference for risk ───────────────────────────────────────
    entry_for_risk: float | None = None
    if order_type == "market":
        if last_price is not None and last_price > 0:
            entry_for_risk = float(last_price)
            # (M-B) The market_entry_slippage adverse-entry shift used to be
            # applied here for risk/RRR. For a tight stop it inflated the
            # entry→SL distance disproportionately (worst case ~2.5×), wrongly
            # pushing legitimate scalps over MAX_RISK_PCT. Risk now uses the raw
            # last price. Trade-off (deliberate): the remaining distance buffer is
            # RISK_SLIPPAGE_PCT inside risk_usdt(), which is smaller than the old
            # entry shift — so an adverse fill (up to market_entry_slippage_pct)
            # can push the REALISED risk slightly over the computed MAX_RISK_PCT.
            # This is bounded by the exchange-side slippage cap on the ACTUAL
            # order (see exchange_factory / hyperliquid client), which limits how
            # far the fill can move from last.
        elif ticket.entry is not None and float(ticket.entry) > 0:
            entry_for_risk = float(ticket.entry)
            warnings.append("market order risk uses ticket.entry (no last_price)")
        else:
            errors.append("market order needs last_price (or entry ref) for risk calc")
    else:
        # Limit risk MUST use the limit price — ticket.entry is not trusted
        # (spoofed entry closer to SL would understate MAX_RISK_PCT / pass bad geometry).
        if rounded_price is not None and float(rounded_price) > 0:
            entry_for_risk = float(rounded_price)
            if ticket.entry is not None and float(ticket.entry) > 0:
                te = float(ticket.entry)
                if abs(te - entry_for_risk) / max(entry_for_risk, 1e-12) * 100.0 > 0.05:
                    warnings.append(
                        f"ticket.entry {te} ignored for risk; using limit price "
                        f"{entry_for_risk}"
                    )
        elif ticket.price is not None and float(ticket.price) > 0:
            entry_for_risk = float(ticket.price)
        else:
            errors.append("limit order needs price for risk calc")

    # ── Price drift (confirm vs preview) ───────────────────────────────
    if (
        for_confirm
        and preview_last_price is not None
        and float(preview_last_price) > 0
        and last_price is not None
        and float(last_price) > 0
    ):
        prev = float(preview_last_price)
        now = float(last_price)
        drift_pct = abs(now - prev) / prev * 100.0
        max_drift = float(getattr(settings, "max_price_drift_pct", 0.5) or 0.5)
        if drift_pct > max_drift + 1e-12:
            errors.append(
                f"price drift {drift_pct:.3f}% exceeds MAX_PRICE_DRIFT_PCT="
                f"{max_drift} (preview {prev} → now {now}) — re-preview"
            )

    # ── SL required unless unprotected allowed ─────────────────────────
    sl = ticket.stop_loss
    rounded_stop: float | None = None
    if sl is None or float(sl) <= 0:
        if not settings.allow_unprotected_entry:
            errors.append(
                "stop_loss required (ALLOW_UNPROTECTED_ENTRY=false). "
                "Naked live entries are blocked."
            )
        else:
            warnings.append(
                "No stop_loss — unprotected entry allowed by config (dangerous)"
            )
        sl_f: float | None = None
    else:
        sl_f = float(sl)
        if entry_for_risk is not None and side in ("long", "short"):
            if side == "long" and sl_f >= entry_for_risk:
                errors.append("long stop_loss must be below entry")
            if side == "short" and sl_f <= entry_for_risk:
                errors.append("short stop_loss must be above entry")
        if price_unit > 0:
            sl_f = round_to_unit(sl_f, price_unit)
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
    if tp is not None and float(tp) > 0:
        rounded_tp = float(tp)
        if price_unit > 0:
            rounded_tp = round_to_unit(rounded_tp, price_unit)

    # ── Equity fail-closed (G3 requires known equity) ──────────────────
    if equity is None or float(equity) <= 0:
        errors.append(
            "equity unknown/zero — fail-closed (MAX_RISK_PCT cannot be enforced)"
        )

    # ── G3 risk % (with slippage buffer on distance) ───────────────────
    risk_u = 0.0
    risk_p = 0.0
    if (
        sl_f is not None
        and entry_for_risk is not None
        and rounded_vol > 0
        and contract.contract_size > 0
    ):
        risk_u = risk_usdt(
            rounded_vol,
            contract.contract_size,
            entry_for_risk,
            sl_f,
            slippage_pct=settings.risk_slippage_pct,
        )
        # Aggregate with existing same-side exposure on symbol
        total_risk = risk_u + max(0.0, float(existing_same_side_risk_usdt or 0.0))
        if float(equity) > 0:
            risk_p = (total_risk / float(equity)) * 100.0
            if existing_same_side_risk_usdt and existing_same_side_risk_usdt > 0:
                warnings.append(
                    f"includes existing same-side risk ~"
                    f"{existing_same_side_risk_usdt:.4f} USDT "
                    "(loss to each position's stop where known, else "
                    "capped liquidation distance)"
                )
            # Surface any per-position fallback notes (e.g. missing liq price
            # in non-strict mode) authored by estimate_same_side_risk_usdt.
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
    rrr: float | None = None
    if (
        sl_f is not None
        and rounded_tp is not None
        and entry_for_risk is not None
        and side in ("long", "short")
    ):
        try:
            rrr = calc_rrr(side, entry_for_risk, sl_f, float(rounded_tp))
            if rrr + 1e-12 < settings.min_rrr:
                msg = f"RRR {rrr:.3f} < MIN_RRR={settings.min_rrr}"
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
            "MANUELL ohne TP: STRICT_RRR kann RRR nicht prüfen — kein Börsen-TP, "
            "du verwaltest den Ausstieg selbst."
        )

    # ── Max notional ───────────────────────────────────────────────────
    notional = 0.0
    if entry_for_risk is not None and rounded_vol > 0 and contract.contract_size > 0:
        notional = rounded_vol * contract.contract_size * entry_for_risk
        # Fixed USDT cap is a soft WARNING (does not scale with the account, so
        # it must not block "free size"). 0 = off. The real size guard is the
        # equity-relative cap below + risk-% + available-margin gates.
        if settings.max_notional_usdt > 0 and notional > settings.max_notional_usdt + 1e-9:
            warnings.append(
                f"Positionswert {notional:.2f} USDT über MAX_NOTIONAL_USDT="
                f"{settings.max_notional_usdt} (Hinweis, kein Block)"
            )
        # HARD equity-relative fat-finger cap: notional <= equity × pct/100.
        # Scales with the account and stays fail-closed to known equity. 0 = off.
        pct_cap = float(getattr(settings, "max_notional_pct_of_equity", 0) or 0)
        # Defense-in-depth: guard against a non-finite pct_cap, which would
        # otherwise silently disable this equity-relative cap (any comparison
        # against NaN is False; cap computed from Infinity would never bind).
        if not math.isfinite(pct_cap):
            errors.append(
                "MAX_NOTIONAL_PCT_OF_EQUITY is not a finite number — "
                "refusing to evaluate the equity-relative notional gate "
                "(fail-closed)"
            )
        elif pct_cap > 0 and equity is not None and float(equity) > 0:
            cap = float(equity) * pct_cap / 100.0
            if notional > cap + 1e-9:
                errors.append(
                    f"notional {notional:.2f} USDT exceeds {pct_cap:.0f}% of equity "
                    f"(cap {cap:.2f} USDT) — MAX_NOTIONAL_PCT_OF_EQUITY"
                )
        min_notional = float(contract.min_notional or 0)
        if min_notional > 0 and notional + 1e-9 < min_notional:
            errors.append(
                f"notional {notional:.4f} below exchange minimum "
                f"{min_notional} — order would be rejected"
            )

    # ── Available margin ───────────────────────────────────────────────
    if available_usdt is not None and float(available_usdt) <= 0 and float(equity or 0) > 0:
        errors.append("available USDT is 0 — cannot open (margin fully used)")
    elif available_usdt is not None and notional > 0 and lev > 0:
        est_im = notional / float(lev)
        if float(available_usdt) + 1e-9 < est_im:
            msg = (
                f"estimated IM ~{est_im:.2f} exceeds available "
                f"{float(available_usdt):.2f} USDT"
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
