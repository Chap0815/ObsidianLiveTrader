"""Single source of truth for SL/TP classification on the backend (Q-05).

Two money-path code paths must answer the SAME question — "is this trigger a
stop-loss or a take-profit?" — the exact same way, or a position can look
"protected" to the advisory reevaluate while the auto-flatten verifier decides
the SL is "missing" (or vice versa). Before this module those two sites carried
independently-drifting heuristics. They now share the helpers below.

Priority order for a single order (highest wins):
1. an explicit ``stopLossPrice`` / ``takeProfitPrice`` field (MEXC echoes these
   in the create body) — an explicit field ALWAYS beats any inference;
2. a ``triggerPrice`` (or ``price``) plus an ``orderType`` label ("Stop" /
   "Take Profit" on Hyperliquid, "sl"/"tp"/"tpsl" on MEXC);
3. only as a last fallback, a side+entry geometry heuristic — a stop sits on
   the LOSS side of entry, a take-profit on the PROFIT side.

Never fabricate an SL: an unlabeled trigger whose side/entry can't be resolved
counts as neither (unknown), because a fake SL can mask an actually-unprotected
position (F-12). The frontend keeps its own UI heuristic (app.js
``findPositionProtection``); this module is the backend's only classifier.
"""

from __future__ import annotations

import math


def _positive_price(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def classify_order_label(label: object) -> str | None:
    """Classify an order purely by its type/kind label → 'sl' | 'tp' | None.

    None means "no usable label" (empty/plan/unknown) — the caller decides the
    fallback (side+entry for the extractor, price-match for the verifier).

    A take-profit is only recognised on an UNAMBIGUOUS marker ("take"/exact
    "tp"), never on a bare "tp" prefix: MEXC's combined "tpsl" order carries a
    stop and must stay SL-relevant, so treating it as TP would let the
    auto-flatten verifier skip a real stop (fail-open). This keeps the verifier
    bit-identical to its prior inline rule.
    """
    if label is not None and not isinstance(label, str):
        return None
    s = (label or "").strip().lower()
    if not s:
        return None
    if "take" in s or s in ("tp", "take_profit", "take-profit"):
        return "tp"
    if "stop" in s or "sl" in s:
        return "sl"
    return None


def _position_side(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    normalized = str(value).strip().lower()
    if normalized in ("1", "long"):
        return "long"
    if normalized in ("2", "short"):
        return "short"
    return None


def classify_unlabeled_trigger(
    trigger_price: float, side: str | None, entry_price: float | None
) -> str | None:
    """Classify an unlabeled trigger as 'sl' or 'tp' by position side + entry.

    A stop sits on the loss side of entry, a take-profit on the profit side.
    Returns None ("unknown") when side/entry aren't known or the trigger sits
    exactly on entry — callers must NEVER default to SL in that case (F-12: a
    fabricated SL can mask an actually-unprotected position).
    """
    side_n = (side or "").strip().lower()
    if side_n not in ("long", "short"):
        return None
    trigger = _positive_price(trigger_price)
    entry = _positive_price(entry_price)
    if trigger is None or entry is None:
        return None
    if trigger == entry:
        return None
    if side_n == "long":
        return "sl" if trigger < entry else "tp"
    return "sl" if trigger > entry else "tp"


def classify_protection(
    stops: list[object], *, side: str | None = None, entry: float | None = None
) -> tuple[float | None, float | None]:
    """Best-effort current (sl, tp) from open trigger orders for one symbol.

    Explicit stopLossPrice/takeProfitPrice field wins; otherwise fall back to
    triggerPrice/price + an orderType label; an unlabeled trigger is classified
    by side + entry, or counts as neither (unknown) — never a fabricated SL.
    Read-only — never places/cancels anything.

    C1: when MULTIPLE resting orders classify as SL — a documented real state
    (``modify_stop_loss`` can leave two stops on
    ``modify_sl_ok_old_cancel_failed`` / ``modify_sl_unverified_old_kept``) — the
    MOST-protective one is reported, never last-wins. Under-reporting the current
    SL would let the auto-trail monitor compute a trail between a loose old stop
    and the good one and then cancel BOTH → live protection drops. Most
    protective: long → the HIGHEST sl, short → the LOWEST sl. When the side is
    unknown the last-seen candidate is kept (prior behaviour). TP keeps last-wins
    (not safety-critical).
    """
    sl_candidates: list[float] = []
    tp: float | None = None
    side_n = (side or "").strip().lower()
    for row in stops or []:
        if not isinstance(row, dict):
            continue
        position_types = [
            row.get(key)
            for key in ("positionType", "position_type")
            if key in row and row.get(key) is not None
        ]
        if position_types:
            normalized_types = [_position_side(value) for value in position_types]
            if any(value is None for value in normalized_types) or len(
                set(normalized_types)
            ) != 1:
                continue
            if (
                side_n in ("long", "short")
                and normalized_types[0] != side_n
            ):
                continue
        sl_field = _positive_price(row.get("stopLossPrice"))
        tp_field = _positive_price(row.get("takeProfitPrice"))
        invalid_explicit = (
            row.get("stopLossPrice") is not None and sl_field is None
        ) or (row.get("takeProfitPrice") is not None and tp_field is None)
        if invalid_explicit:
            continue
        if sl_field is not None:
            sl_candidates.append(sl_field)
        if tp_field is not None:
            tp = tp_field
        if sl_field is not None or tp_field is not None:
            continue
        raw_trg = row.get("triggerPrice")
        if raw_trg is None:
            raw_trg = row.get("price")
        trg = _positive_price(raw_trg)
        if trg is None:
            continue
        label_raw = row.get("orderType")
        if label_raw is not None and not isinstance(label_raw, str):
            continue
        kind = classify_order_label(label_raw)
        if kind == "tp":
            tp = trg
        elif kind == "sl":
            sl_candidates.append(trg)
        else:
            # Unlabeled trigger: never assume SL. Classify by side + entry;
            # if that's not resolvable, it's unknown protection (neither).
            geo = classify_unlabeled_trigger(trg, side, entry)
            if geo == "sl":
                sl_candidates.append(trg)
            elif geo == "tp":
                tp = trg
    return most_protective_sl(sl_candidates, side), tp


def most_protective_sl(
    candidates: list[float], side: str | None
) -> float | None:
    """Pick the MOST-protective stop among candidates (C1).

    long → HIGHEST sl (closest to entry above the loss side), short → LOWEST sl.
    When the side is unknown the direction can't be decided, so the last-seen
    candidate is returned (preserves the pre-C1 last-wins behaviour for that
    ambiguous case). Empty → None.
    """
    if not candidates:
        return None
    side_n = (side or "").strip().lower()
    if side_n == "long":
        return max(candidates)
    if side_n == "short":
        return min(candidates)
    return candidates[-1]
