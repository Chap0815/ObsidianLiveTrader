"""Fee-aware break-even price — pure port of trade-math.js:breakEvenPrice.

Server-side (auto-BE) had no fee-aware break-even calculation; only the
frontend's ``trade-math.js:breakEvenPrice`` computed it. This module mirrors
that formula and sign handling EXACTLY (money-adjacent — must match the JS):

    function breakEvenPrice(entry, isShort, feeRt) {
      const e = Number(entry);
      if (!Number.isFinite(e) || e <= 0) return null;
      const rt = feeRt == null ? 0.0006 : Number(feeRt);
      return isShort ? e * (1 - rt) : e * (1 + rt);
    }

      long:  entry * (1 + feeRt)
      short: entry * (1 - feeRt)

``feeRt`` defaults to 0.0006 (0.06% round-trip taker) — the constant every
prior JS copy hard-coded (see also ``tm_be_fee_rt`` in the trade-management
spec). Returns ``None`` for a non-finite or non-positive entry, mirroring the
JS ``null`` return.

Note on ``fee_rt``: the JS coerces a missing/``null`` feeRt to the 0.0006
default via ``feeRt == null ? 0.0006 : Number(feeRt)``, but does NOT
separately validate the result is finite — a non-finite feeRt just propagates
through the arithmetic (e.g. NaN in -> NaN out). Python has no direct
equivalent of "argument omitted vs. explicitly null", so the default is
applied via the normal parameter default and a caller-supplied non-finite
fee_rt is likewise used as-is (propagates to a non-finite/NaN result) rather
than additionally guarded — that is the closest byte-equivalent behavior.

Pure function: no state, no I/O.
"""
from __future__ import annotations

import math


def break_even_price(
    entry: float, is_short: bool, fee_rt: float = 0.0006
) -> float | None:
    if not math.isfinite(entry) or entry <= 0:
        return None
    # Trust boundary: a non-finite fee_rt must not silently yield a bogus but
    # finite BE that could then move a real stop-loss. The intended caller passes
    # the config-validated `tm_be_fee_rt`; refuse anything non-finite.
    if not math.isfinite(fee_rt):
        return None
    return entry * (1 - fee_rt) if is_short else entry * (1 + fee_rt)
