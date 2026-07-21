/**
 * Obsidian Live Trader — pure presentation/format helpers (A3-01).
 *
 * MODULE-LOADING CHOICE (deliberate, documented): this is a CLASSIC script,
 * NOT `<script type="module">`. It is loaded in base.html IMMEDIATELY BEFORE
 * app.js so its top-level `function …` declarations become GLOBAL functions on
 * the shared script scope. app.js (a classic IIFE) references these by bare
 * name (`fmt(x)`, `escapeHtml(s)`, …); with classic scripts that bare name
 * resolves to the global defined here — so NO call sites in app.js change.
 *
 * Rationale for NOT using ES modules: `type="module"` would change execution
 * timing (deferred), give each file its own scope (forcing import/export at
 * every call site) and risk CSP / inline-handler breakage. A classic
 * load-before-app.js keeps behavior byte-identical.
 *
 * INVARIANT: only genuinely PURE helpers belong here — output depends solely on
 * arguments, with NO `state.` access, NO module-scoped app vars, and NO
 * `$()`/getElementById DOM lookups. Stateful/DOM-bound helpers (markerKey, ccy,
 * …) MUST stay in app.js.
 */

function fmt(n, digits) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const d = digits != null ? digits : 4;
  return Number(n).toLocaleString("de-DE", {
    maximumFractionDigits: d,
    minimumFractionDigits: 0,
  });
}

/**
 * Derive a Lightweight-Charts priceFormat {precision, minMove} for the
 * candle series. LWC's own default (precision:2, minMove:0.01) collapses
 * sub-dollar coins onto "0.00" on the price axis, crosshair, and every
 * price-line axis label (Entry/SL/TP/Liq via addChartLine) — Finding 1.
 * Prefers the exchange's own tick step (tickStep, e.g. MEXC's
 * contract.priceUnit — the same source updatePriceFieldSteps()/tickSize()
 * read in app.js) since that's authoritative; falls back to deriving from
 * the order of magnitude of lastPrice when no tick step is known yet (e.g.
 * before the contract meta has loaded). Pure — no state/DOM access, so it's
 * directly unit-testable.
 */
function derivePriceFormat(tickStep, lastPrice) {
  const tick = Number(tickStep);
  if (Number.isFinite(tick) && tick > 0) {
    const s = tick.toString();
    let precision;
    if (s.indexOf("e-") !== -1) {
      precision = Number(s.split("e-")[1]);
    } else {
      const i = s.indexOf(".");
      precision = i === -1 ? 0 : s.length - i - 1;
    }
    precision = Math.min(Math.max(precision, 0), 10);
    return { precision: precision, minMove: Math.pow(10, -precision) };
  }
  const px = Math.abs(Number(lastPrice));
  if (!Number.isFinite(px) || px <= 0) {
    return { precision: 2, minMove: 0.01 }; // LWC default — unchanged behavior
  }
  let precision;
  if (px >= 1) {
    precision = px >= 100 ? 2 : 4;
  } else {
    precision = Math.min(10, Math.max(2, -Math.floor(Math.log10(px)) + 3));
  }
  return { precision: precision, minMove: Math.pow(10, -precision) };
}

/**
 * Adaptive-precision price formatter for display strings (ctx-price,
 * mini-tile prices, fingerprints). fmt(px, 6) rounds anything below 1e-6 to
 * "0" — sub-µ-priced coins (Finding 2). Prices only: other numeric fields
 * keep plain fmt(), whose 4-digit default other call sites rely on.
 */
function fmtPx(px) {
  if (px == null || Number.isNaN(Number(px))) return fmt(px, 6);
  const abs = Math.abs(Number(px));
  const d = abs > 0 && abs < 1 ? Math.min(10, Math.max(6, -Math.floor(Math.log10(abs)) + 3)) : 6;
  return fmt(px, d);
}

function fmtPct(rate) {
  if (rate == null || Number.isNaN(Number(rate))) return "—";
  // funding often as fraction (e.g. 0.0001) → show bps-ish percent
  return (Number(rate) * 100).toFixed(4) + "%";
}

/** MEXC candle time is ms; Lightweight Charts wants seconds. */
function toChartTime(ms) {
  const t = Number(ms);
  if (!Number.isFinite(t)) return null;
  return t > 1e12 ? Math.floor(t / 1000) : Math.floor(t);
}

function tfSeconds(tf) {
  const m = {
    "5m": 300,
    "15m": 900,
    "1H": 3600,
    "4H": 14400,
    "1D": 86400,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
  };
  return m[tf] || 900;
}

function barOpenTimeSec(tsMsOrSec, tf) {
  let sec = Number(tsMsOrSec);
  if (!Number.isFinite(sec)) sec = Date.now() / 1000;
  if (sec > 1e12) sec = Math.floor(sec / 1000);
  else sec = Math.floor(sec);
  const bucket = tfSeconds(tf);
  return Math.floor(sec / bucket) * bucket;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function relTime(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "gerade eben";
  const m = Math.floor(s / 60);
  if (m < 60) return "vor " + m + " Min";
  const h = Math.floor(m / 60);
  if (h < 24) return "vor " + h + " Std";
  const d = Math.floor(h / 24);
  return "vor " + d + " Tag" + (d === 1 ? "" : "en");
}

function numOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = Number(el.value);
  return Number.isFinite(n) ? n : null;
}
