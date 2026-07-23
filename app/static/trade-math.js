/**
 * Obsidian Live Trader — pure money-math helpers (A3-06, Task 41b).
 *
 * MODULE-LOADING CHOICE (same rationale as utils.js): this is a CLASSIC script,
 * NOT `<script type="module">`. It is loaded in base.html AFTER utils.js and
 * IMMEDIATELY BEFORE app.js, so its top-level `function …` declarations become
 * GLOBAL functions on the shared script scope. app.js (a classic IIFE) calls
 * them by bare name — no import/export, behavior byte-identical to the inline
 * copies these replace.
 *
 * WHY THIS FILE EXISTS: PnL, ROE, break-even, contract-size fallback,
 * R-multiple and SL/TP trigger-classification were each implemented multiple
 * times across app.js, divergently — the most likely birthplace of a
 * "the display lies" bug (one place says protected, another says naked; one
 * PnL sign flips). This file is the ONE frontend source for each formula.
 *
 * INVARIANT: every function here is PURE — output depends solely on the
 * arguments. NO `state.` access, NO DOM, NO `fmt`/format calls. If a formula
 * needs contract size, health, side or entry, the CALLER passes it in.
 *
 * The trigger classifier (classifyTriggerLabel / classifyTriggers) mirrors the
 * BACKEND single source app/orders/protection.py — field-first, then label,
 * then side+entry geometry LAST — so the frontend can never answer "is this a
 * stop-loss?" differently from the backend. See classifyTriggers for the one
 * documented, deliberate frontend-only nuance (break-even tolerance, F-12b).
 */

/**
 * Unrealised PnL in quote currency (USDT) from a mark price.
 *   pnl = (mark − entry) · vol · contractSize · (short ? −1 : +1)
 * The single exchange-accurate form; every prior copy agreed on this, so
 * behavior is unchanged. Returns a Number (NaN if inputs are non-finite —
 * callers already gate on Number.isFinite before displaying).
 */
function computePnl(mark, entry, vol, contractSize, isShort) {
  return (Number(mark) - Number(entry)) * Number(vol) * Number(contractSize) *
    (isShort ? -1 : 1);
}

/**
 * Return on equity (%) = pnl / initial-margin · 100, or null when the margin
 * isn't a usable positive number (or pnl is non-finite). Matches every prior
 * `Number.isFinite(im) && im > 0 ? (pnl/im)*100 : null` copy — including the
 * ticket copy that also guarded `pnl != null` (a non-finite pnl → null here).
 * Named computeRoe (not `roe`) to avoid shadowing the ubiquitous `const roe`
 * local at the call sites.
 */
function computeRoe(pnl, im) {
  if (pnl == null) return null; // Number(null)===0 is finite — guard explicitly so a null pnl stays null (not a fabricated 0.0%)
  const p = Number(pnl);
  const m = Number(im);
  if (!Number.isFinite(p) || !Number.isFinite(m) || m <= 0) return null;
  return (p / m) * 100;
}

/**
 * Fee-adjusted break-even price: the price at which closing covers the
 * ~round-trip taker fees, so "SL → Break-Even" actually covers costs rather
 * than just the raw entry.
 *   long:  entry · (1 + feeRt)      short: entry · (1 − feeRt)
 * feeRt defaults to 0.0006 (0.06% round-trip taker) — the constant every prior
 * copy hard-coded. Returns null for a non-positive/non-finite entry.
 */
function breakEvenPrice(entry, isShort, feeRt) {
  const e = Number(entry);
  if (!Number.isFinite(e) || e <= 0) return null;
  const rt = feeRt == null ? 0.0006 : Number(feeRt);
  return isShort ? e * (1 - rt) : e * (1 + rt);
}

/**
 * R-multiple of a target vs entry, measured in units of the stop distance:
 *   |target − entry| / |entry − sl|
 * i.e. "how much reward per unit of risk this target pays". Returns null when
 * the stop distance is zero or any input is non-finite. This is the
 * abs-distance form used by every R-LABEL site (chart TP band, drag ghost,
 * proposal/analysis TP tiles); it equals the directional reward/risk for
 * correct-side geometry. NOTE: the ticket's RRR readout keeps its own
 * DIRECTIONAL geometry (long: tp−entry over entry−sl; null on wrong side,
 * F-23) — that is a validation guard, not this per-target label, so it is
 * intentionally NOT folded in here.
 */
function rMultiple(target, entry, sl) {
  const t = Number(target);
  const e = Number(entry);
  const s = Number(sl);
  if (!Number.isFinite(t) || !Number.isFinite(e) || !Number.isFinite(s)) {
    return null;
  }
  const risk = Math.abs(e - s);
  if (!(risk > 0)) return null;
  return Math.abs(t - e) / risk;
}

/**
 * The contract size to use for a position: the position's OWN contract_size
 * when it's a usable positive number, else a caller-supplied fallback (the
 * active chart symbol's contractSize, or 1). Unifies the five divergent
 * `Number(p.contract_size) … ? … : contractSize()/1` copies into one rule so a
 * per-coin contract size is NEVER silently swapped for a global default when
 * the specific value is present (F-10). `fallback` is likewise validated
 * (>0), so a non-positive fallback degrades to 1 rather than poisoning notional.
 */
function positionContractSize(rawContractSize, fallback) {
  const v = Number(rawContractSize);
  if (Number.isFinite(v) && v > 0) return v;
  const f = Number(fallback);
  return Number.isFinite(f) && f > 0 ? f : 1;
}

/**
 * Classify an order purely by its type/kind LABEL → 'sl' | 'tp' | null.
 * MIRRORS app/orders/protection.py::classify_order_label EXACTLY:
 *  - a take-profit is recognised only on an UNAMBIGUOUS marker — "take"
 *    anywhere, or an EXACT "tp"/"take_profit"/"take-profit" — NEVER a bare
 *    "tp" PREFIX. MEXC's combined "tpsl" order carries a STOP and must stay
 *    SL-relevant; classifying it TP would let a real stop read as "no SL"
 *    (fail-open). This is the bug the prior frontend `indexOf("tp") === 0`
 *    copies shipped — "tpsl" matched the prefix and was mislabeled TP.
 *  - otherwise "stop" anywhere or "sl" anywhere → 'sl'.
 *  - null means "no usable label" — the caller falls back to geometry.
 */
function classifyTriggerLabel(orderType) {
  const s = String(orderType == null ? "" : orderType).trim().toLowerCase();
  if (!s) return null;
  if (s.indexOf("take") >= 0 || s === "tp" || s === "take_profit" ||
      s === "take-profit") {
    return "tp";
  }
  if (s.indexOf("stop") >= 0 || s.indexOf("sl") >= 0) return "sl";
  return null;
}

/**
 * Classify ONE exchange trigger order as protection → { sl, tp } (prices;
 * at most one set). Single FRONTEND source, mirroring the backend
 * app/orders/protection.py::classify_protection priority order (highest wins):
 *   1. an explicit stopLossPrice / takeProfitPrice FIELD (MEXC echoes these) —
 *      a field ALWAYS beats any inference;
 *   2. a triggerPrice|price plus an orderType LABEL (classifyTriggerLabel);
 *   3. side + entry GEOMETRY, last: a stop sits on the LOSS side of entry, a
 *      take-profit on the PROFIT side.
 * Like the backend, an unlabeled trigger whose side/entry can't be resolved is
 * classified as NEITHER (never a fabricated SL that could mask an actually
 * unprotected position — F-12).
 *
 * ONE documented FRONTEND-ONLY nuance (the backend keeps its own classifier and
 * explicitly lets the frontend keep this UI heuristic — see protection.py
 * docstring): a trigger within ~0.1% of entry is treated as a break-even STOP
 * (→ sl), not "unknown" (F-12b) — a real break-even stop must never read as
 * unprotected. Pass includeBeTolerance=false to get the pure backend geometry
 * (trigger exactly on/near entry → neither).
 *
 * Pure: side and entry are passed in; no state access.
 */
function classifyTriggers(order, side, entry, includeBeTolerance) {
  const o = order || {};
  const out = { sl: null, tp: null };
  // 1) explicit field wins
  const slField = Number(o.stopLossPrice);
  if (Number.isFinite(slField) && slField > 0) { out.sl = slField; return out; }
  const tpField = Number(o.takeProfitPrice);
  if (Number.isFinite(tpField) && tpField > 0) { out.tp = tpField; return out; }
  // trigger price (triggerPrice, else price)
  const trg = Number(o.triggerPrice != null ? o.triggerPrice : o.price);
  if (!Number.isFinite(trg) || trg <= 0) return out;
  // 2) orderType label
  const kind = classifyTriggerLabel(o.orderType);
  if (kind === "tp") { out.tp = trg; return out; }
  if (kind === "sl") { out.sl = trg; return out; }
  // 3) geometry (side + entry) — last resort
  const e = Number(entry);
  const sideN = String(side == null ? "" : side).trim().toLowerCase();
  if (!(Number.isFinite(e) && e > 0) || (sideN !== "long" && sideN !== "short")) {
    return out; // unresolved → neither (never a fabricated SL)
  }
  if (includeBeTolerance !== false && Math.abs(trg - e) <= e * 0.001) {
    out.sl = trg; // within ~0.1% of entry = break-even stop = protection
    return out;
  }
  const below = trg < e;
  if (sideN === "short" ? !below : below) out.sl = trg; // adverse side = SL
  else out.tp = trg;
  return out;
}

/**
 * SL coverage verdict (HIGH-fix): do the protective stop orders cover the FULL
 * position? `slVol` is the summed size of the same-side stop-loss orders and
 * `holdVol` the position's hold volume — BOTH must be in the SAME unit (MEXC:
 * contracts; Hyperliquid: coins; a contracts-vs-coins mix would be a bug). The
 * epsilon (default 0.5%) absorbs lot/float rounding, so a stop that covers the
 * position minus a rounding crumb still reads as fully covered.
 *
 * CONSERVATIVE by construction: a non-finite / non-positive `holdVol` or
 * `slVol` (e.g. a stop order without a readable size — HL triggers carry no
 * top-level vol) yields `true`, so the caller keeps its existing "protected"
 * display and never raises a false partial-coverage alarm. Under-coverage is
 * asserted ONLY on a positive, readable `slVol` that genuinely falls short.
 *
 * Pure: no state/DOM access.
 */
function slCoverageCovered(slVol, holdVol, eps) {
  const h = Number(holdVol);
  const v = Number(slVol);
  if (!Number.isFinite(h) || h <= 0) return true;
  if (!Number.isFinite(v) || v <= 0) return true;
  const tol = Number.isFinite(eps) ? eps : 0.005;
  return v >= h * (1 - tol);
}
