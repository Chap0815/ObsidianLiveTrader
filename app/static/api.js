/**
 * Obsidian Live Trader — network core (A3-07 / A3-08, Task 42).
 *
 * MODULE-LOADING CHOICE (same rationale as utils.js / trade-math.js): this is a
 * CLASSIC script, NOT `<script type="module">`. It is loaded in base.html AFTER
 * trade-math.js and IMMEDIATELY BEFORE app.js, so its top-level `function …`
 * declarations become GLOBAL functions on the shared script scope. app.js (a
 * classic IIFE) calls `apiFetch(...)` / `authHeaders(...)` by bare name — no
 * import/export, behavior byte-identical to the inline copies these replace.
 *
 * INVARIANT — STATE-FREE: this file is loaded BEFORE app.js, so app.js's sealed
 * `state` object is NOT in scope here and MUST NOT be referenced. The former
 * `state.localToken ||` fallback in authHeaders is replaced by a module-level
 * `_localToken` (default "" — the former `state.localToken` was ALWAYS "" at
 * runtime; it was never assigned anywhere and has since been removed from the
 * state literal, so the effective token source is the localStorage lookup below). An
 * explicit override hook (`setLocalToken`) preserves the old escape hatch.
 *
 * AUTH/CSRF CONTRACT (preserved byte-identical): same-origin requests carry the
 * HttpOnly auth cookie automatically (credentials:"same-origin"); an explicit
 * X-Local-Token header is attached only when a non-browser token is present;
 * JSON content-type is forced for non-GET/HEAD when a body is sent.
 */

// Optional non-browser auth token. Replaces the former (always-empty, now
// removed) state.localToken; only setLocalToken()/localStorage set a token, so
// behavior is identical to before. Kept STATE-FREE on purpose (see header).
var _localToken = "";
function setLocalToken(tok) {
  _localToken = tok || "";
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  // Only force JSON content-type when body is present (POST/PUT)
  if (!h["Content-Type"] && extra && extra["Content-Type"]) {
    h["Content-Type"] = extra["Content-Type"];
  }
  // F-19: the auth token is normally delivered via an HttpOnly session
  // cookie (sent automatically on same-origin requests), so we no longer
  // read it from the DOM. An explicit override/localStorage token is still
  // honored as a fallback for non-browser use.
  // A3-11 (Task 43): read the ONE canonical token key. store.js (loaded FIRST)
  // migrates the legacy alias ("local_api_token") into this canonical key once
  // at load, so an existing user's token is preserved without a second lookup
  // here. PERSIST.TOKEN is a store.js global; the literal is a load-order-safe
  // fallback (identical value) in case store.js hasn't defined it yet.
  const canonKey =
    (typeof PERSIST !== "undefined" && PERSIST && PERSIST.TOKEN) ||
    "mexc_local_token";
  const tok =
    _localToken ||
    (typeof localStorage !== "undefined" && localStorage.getItem(canonKey)) ||
    "";
  if (tok) h["X-Local-Token"] = tok;
  return h;
}

function apiFetch(url, opts) {
  opts = opts || {};
  const method = (opts.method || "GET").toUpperCase();
  const headers = authHeaders(opts.headers || {});
  if (method !== "GET" && method !== "HEAD" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  // F-19: same-origin so the HttpOnly auth cookie is sent automatically.
  return fetch(
    url,
    Object.assign({}, opts, { headers: headers, credentials: "same-origin" })
  );
}

/* ── Per-resource abort registry (A3-08) ─────────────────────────────────
 * A fast symbol switch can leave a slow READ request (/api/market snapshot,
 * /api/analyze) in flight — a discarded /api/analyze wastes real LLM tokens.
 * apiFetchAbortable keys ONE AbortController per logical resource; issuing a
 * new request for the same resource aborts the previous in-flight one.
 *
 * MONEY-PATH GUARDRAIL: this registry is used ONLY for idempotent READ
 * resources (market/analyze/fills/history/journal/calibration). Order submit /
 * modify-sl / cancel go
 * through plain apiFetch() and are NEVER routed here, so the symbol-switch
 * abort logic can never abort a money request (aborting a modify-sl mid-flight
 * could leave a position transiently unprotected). Kept state-free — the
 * registry lives here in api.js, not in app.js's sealed `state`. */
var _apiAborters = {};

function abortResource(resource) {
  const ctrl = _apiAborters[resource];
  if (ctrl) {
    try {
      ctrl.abort();
    } catch (_) {
      /* ignore */
    }
    delete _apiAborters[resource];
  }
}

function apiFetchAbortable(resource, url, opts) {
  opts = opts || {};
  // Supersede any previous in-flight read for this SAME resource.
  abortResource(resource);
  const merged = Object.assign({}, opts);
  if (typeof AbortController !== "undefined") {
    const ctrl = new AbortController();
    _apiAborters[resource] = ctrl;
    merged.signal = ctrl.signal;
  }
  return apiFetch(url, merged);
}

/* ── Trade-Management-Layer endpoints (Task 7) ───────────────────────────
 * Thin apiFetch() wrappers — no state, no JSON parsing, no toasts. Callers
 * (app.js) await the Response themselves, exactly like every other apiFetch
 * call site. Kept here, not in app.js, for the same STATE-FREE reason as the
 * rest of this file (see header). */

/** Arm/disarm one autonomous rule for ONE open position. `rules` MUST carry
 * REAL booleans (e.g. {auto_be: true}) — the server 400s on any non-bool
 * rule value (a truthy string like "false" must never arm a money path). */
function armPosition(symbol, side, rules) {
  return apiFetch("/api/positions/arm", {
    method: "POST",
    body: JSON.stringify({ symbol: symbol, side: side, rules: rules || {} }),
  });
}

/** Polled alert + auto-action feed (spec §6) — armed_rules/be_done/alerts
 * per OPEN position. No server→client push exists; the caller polls this. */
function fetchPositionAlerts() {
  return apiFetch("/api/positions/alerts");
}

/** Global kill-switch: disarms ALL autonomous rules on EVERY open position
 * immediately (spec §3.5). Never touches a stop/order itself. */
function killswitchPositions() {
  return apiFetch("/api/positions/killswitch", { method: "POST" });
}
