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
  if (orderType != null && typeof orderType !== "string") return null;
  const s = String(orderType == null ? "" : orderType).trim().toLowerCase();
  if (!s) return null;
  if (s.indexOf("take") >= 0 || s === "tp" || s === "take_profit" ||
      s === "take-profit") {
    return "tp";
  }
  if (s.indexOf("stop") >= 0 || s.indexOf("sl") >= 0) return "sl";
  return null;
}

/** Match an exact pair or, for Hyperliquid only, a reported bare base coin. */
function symbolsMatch(reported, wanted, allowBareBaseAlias) {
  if (typeof reported !== "string" || typeof wanted !== "string") return false;
  const candidate = reported.trim().toUpperCase();
  const target = wanted.trim().toUpperCase();
  if (!candidate || !target) return false;
  return candidate === target || (
    allowBareBaseAlias === true &&
    candidate.indexOf("_") < 0 &&
    candidate === target.split("_", 1)[0]
  );
}

function normalizeProtectionSide(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const normalized = String(value).trim().toLowerCase();
  if (normalized === "1" || normalized === "long") return "long";
  if (normalized === "2" || normalized === "short") return "short";
  return null;
}

function positiveFiniteNumber(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

/**
 * Treat persisted browser trade markers as untrusted input. Price fields may
 * be numeric strings for backwards compatibility, but booleans, objects and
 * non-positive/non-finite values must never become chart protection. Likewise,
 * only the literal boolean `true` enables manual-mode behavior; a stored
 * string such as "false" must not activate the manual-SL alarm.
 */
function normalizeTradeMarker(marker) {
  const m = marker && typeof marker === "object" && !Array.isArray(marker)
    ? marker
    : {};
  return {
    sl: positiveFiniteNumber(m.sl),
    tp: positiveFiniteNumber(m.tp),
    manual: m.manual === true,
  };
}

/**
 * Read one persisted marker timestamp without trusting localStorage coercion.
 * Marker times are generated as integer epoch milliseconds. Boolean, unsafe,
 * non-positive and future values are invalid; `nowMs` is passed explicitly so
 * this helper remains pure and deterministic in tests.
 */
function tradeMarkerTime(marker, field, nowMs) {
  if (field !== "ts" && field !== "entryMs") return null;
  if (!marker || typeof marker !== "object" || Array.isArray(marker)) return null;
  const value = positiveFiniteNumber(marker[field]);
  const now = positiveFiniteNumber(nowMs);
  if (value == null || now == null) return null;
  if (!Number.isSafeInteger(value) || !Number.isSafeInteger(now)) return null;
  return value <= now ? value : null;
}

/** Exact browser-side interpretation of the documented normalized fill labels. */
function classifyFillDir(direction) {
  if (typeof direction !== "string") return null;
  const value = direction.trim().toLowerCase();
  if (value === "open long" || value === "open short") return "open";
  if (value === "close long" || value === "close short") return "close";
  if (
    value === "liquidated long" || value === "liquidated short" ||
    value === "long > short" || value === "short > long"
  ) return "liq";
  return null;
}

/** Normalized adapters emit exactly one of these execution-side literals. */
function normalizeFillSide(side) {
  return side === "buy" || side === "sell" ? side : null;
}

/** Validate the normalized millisecond fill timestamp at the browser boundary. */
function fillTimeMs(value, nowMs) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const parsed = Number(value);
  const now = positiveFiniteNumber(nowMs);
  if (!Number.isSafeInteger(parsed) || !Number.isSafeInteger(now)) return null;
  if (parsed < 946684800000 || parsed > now + 5 * 60 * 1000) return null;
  return parsed;
}

/**
 * Validate one normalized exchange fill before any browser consumer sees it.
 * Side, size, price and time define trade geometry and therefore must be known;
 * optional fee/PnL values may be absent (zero), but never coercible objects,
 * booleans or non-finite numbers. Returns a fresh normalized object or null.
 */
function normalizeFillRecord(fill, nowMs) {
  if (!fill || typeof fill !== "object" || Array.isArray(fill)) return null;
  const side = normalizeFillSide(fill.side);
  const sz = positiveFiniteNumber(fill.sz);
  const px = positiveFiniteNumber(fill.px);
  const time = fillTimeMs(fill.time, nowMs);

  function optionalFinite(value) {
    if (value == null || value === "") return 0;
    if (typeof value !== "number" && typeof value !== "string") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  const fee = optionalFinite(fill.fee);
  const closedPnl = optionalFinite(fill.closed_pnl);
  if (side == null || sz == null || px == null || time == null || fee == null || closedPnl == null) {
    return null;
  }
  const direction = typeof fill.dir === "string" ? fill.dir.trim().toLowerCase() : "";
  const directionSide = {
    "open long": "buy",
    "close short": "buy",
    "short > long": "buy",
    "liquidated short": "buy",
    "open short": "sell",
    "close long": "sell",
    "long > short": "sell",
    "liquidated long": "sell",
  }[direction];
  if (directionSide != null && directionSide !== side) return null;
  return Object.assign({}, fill, {
    side: side,
    sz: sz,
    px: px,
    time: time,
    fee: fee,
    closed_pnl: closedPnl,
  });
}

/**
 * Latest proven Flat->Open epoch for the current position side. Historical
 * completed trades and add-on fills must not move the current zone start.
 */
function currentPositionEntryFillTime(fills, side, nowMs) {
  if (!Array.isArray(fills)) return null;
  const sideN = typeof side === "string" ? side.trim().toLowerCase() : "";
  const expectedDir = sideN === "long" ? "open long"
    : sideN === "short" ? "open short"
    : null;
  if (expectedDir == null) return null;
  let latest = null;
  fills.forEach(function (fill) {
    if (!fill || typeof fill !== "object" || Array.isArray(fill)) return;
    if (typeof fill.dir !== "string" || fill.dir.trim().toLowerCase() !== expectedDir) {
      return;
    }
    const start = fill.start_position;
    if (typeof start !== "number" || !Number.isFinite(start) || start !== 0) return;
    const time = fillTimeMs(fill.time, nowMs);
    if (time != null && (latest == null || time > latest)) latest = time;
  });
  return latest;
}

/**
 * Classify ONE exchange trigger order as protection → { sl, tp } prices.
 * Single FRONTEND source, mirroring the backend
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
  const sideN = typeof side === "string" ? side.trim().toLowerCase() : "";
  const positionSides = ["positionType", "position_type"]
    .filter(function (key) { return o[key] != null; })
    .map(function (key) { return normalizeProtectionSide(o[key]); });
  if (positionSides.length > 0) {
    if (positionSides.some(function (value) { return value == null; }) ||
        new Set(positionSides).size !== 1) return out;
    if ((sideN === "long" || sideN === "short") && positionSides[0] !== sideN) {
      return out;
    }
  }
  // 1) explicit field wins
  const slField = positiveFiniteNumber(o.stopLossPrice);
  const tpField = positiveFiniteNumber(o.takeProfitPrice);
  const invalidExplicit =
    (o.stopLossPrice != null && slField == null) ||
    (o.takeProfitPrice != null && tpField == null);
  if (invalidExplicit) return out;
  if (slField != null) out.sl = slField;
  if (tpField != null) out.tp = tpField;
  if (slField != null || tpField != null) return out;
  // trigger price (triggerPrice, else price)
  const trg = positiveFiniteNumber(
    o.triggerPrice != null ? o.triggerPrice : o.price
  );
  if (trg == null) return out;
  // 2) orderType label
  const label = o.orderType;
  if (label != null && typeof label !== "string") return out;
  const kind = classifyTriggerLabel(label);
  if (kind === "tp") { out.tp = trg; return out; }
  if (kind === "sl") { out.sl = trg; return out; }
  // 3) geometry (side + entry) — last resort
  const e = Number(entry);
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

/** Validated SL/TP, or a neutral unlabeled trigger for generic chart display. */
function classifyChartTrigger(order) {
  const o = order || {};
  const classified = classifyTriggers(o, null, null, false);
  const out = { sl: classified.sl, tp: classified.tp, trigger: null };
  if (out.sl != null || out.tp != null) return out;
  if (o.stopLossPrice != null || o.takeProfitPrice != null) return out;
  if (o.orderType != null && typeof o.orderType !== "string") return out;
  out.trigger = positiveFiniteNumber(
    o.triggerPrice != null ? o.triggerPrice : o.price
  );
  return out;
}

/** Return the tightest valid SL, matching the backend selector exactly. */
function mostProtectiveSl(candidates, side) {
  if (!Array.isArray(candidates) || candidates.length === 0) return null;
  const sideN = typeof side === "string" ? side.trim().toLowerCase() : "";
  if (sideN === "long") return Math.max(...candidates);
  if (sideN === "short") return Math.min(...candidates);
  return candidates[candidates.length - 1];
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
