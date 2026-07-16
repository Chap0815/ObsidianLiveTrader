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


def classify_order_label(label: str | None) -> str | None:
    """Classify an order purely by its type/kind label → 'sl' | 'tp' | None.

    None means "no usable label" (empty/plan/unknown) — the caller decides the
    fallback (side+entry for the extractor, price-match for the verifier).

    A take-profit is only recognised on an UNAMBIGUOUS marker ("take"/exact
    "tp"), never on a bare "tp" prefix: MEXC's combined "tpsl" order carries a
    stop and must stay SL-relevant, so treating it as TP would let the
    auto-flatten verifier skip a real stop (fail-open). This keeps the verifier
    bit-identical to its prior inline rule.
    """
    s = (label or "").strip().lower()
    if not s:
        return None
    if "take" in s or s in ("tp", "take_profit", "take-profit"):
        return "tp"
    if "stop" in s or "sl" in s:
        return "sl"
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
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    if trigger_price == entry:
        return None
    if side_n == "long":
        return "sl" if trigger_price < entry else "tp"
    return "sl" if trigger_price > entry else "tp"


def classify_protection(
    stops: list[dict], *, side: str | None = None, entry: float | None = None
) -> tuple[float | None, float | None]:
    """Best-effort current (sl, tp) from open trigger orders for one symbol.

    Explicit stopLossPrice/takeProfitPrice field wins; otherwise fall back to
    triggerPrice/price + an orderType label; an unlabeled trigger is classified
    by side + entry, or counts as neither (unknown) — never a fabricated SL.
    Read-only — never places/cancels anything.
    """
    sl: float | None = None
    tp: float | None = None
    for row in stops or []:
        try:
            sl_field = float(row.get("stopLossPrice"))
        except (TypeError, ValueError):
            sl_field = None
        if sl_field and sl_field > 0:
            sl = sl_field
            continue
        try:
            tp_field = float(row.get("takeProfitPrice"))
        except (TypeError, ValueError):
            tp_field = None
        if tp_field and tp_field > 0:
            tp = tp_field
            continue
        raw_trg = row.get("triggerPrice")
        if raw_trg is None:
            raw_trg = row.get("price")
        try:
            trg = float(raw_trg)
        except (TypeError, ValueError):
            trg = None
        if not trg or trg <= 0:
            continue
        kind = classify_order_label(row.get("orderType"))
        if kind == "tp":
            tp = trg
        elif kind == "sl":
            sl = trg
        else:
            # Unlabeled trigger: never assume SL. Classify by side + entry;
            # if that's not resolvable, it's unknown protection (neither).
            geo = classify_unlabeled_trigger(trg, side, entry)
            if geo == "sl":
                sl = trg
            elif geo == "tp":
                tp = trg
    return sl, tp
