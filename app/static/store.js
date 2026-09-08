/**
 * Obsidian Live Trader — shared state store (A3-02 / A3-11, Task 43).
 *
 * MODULE-LOADING CHOICE (same rationale as utils.js / trade-math.js / api.js):
 * this is a CLASSIC script, NOT `<script type="module">`. It is loaded in
 * base.html FIRST — BEFORE utils.js → trade-math.js → api.js → app.js — so its
 * top-level `var state` / `var PERSIST` become GLOBALS on the shared script
 * scope. Every later script (api.js, app.js) references `state` / `PERSIST` by
 * bare name — no import/export.
 *
 * WHY FIRST: `state` was formerly a `const` INSIDE app.js's IIFE (T41a). Moving
 * it here and loading it first makes it the ONE object every module sees. app.js
 * no longer declares its own `state` (that would shadow this one and re-split the
 * single source of truth); it references THIS global by bare name.
 *
 * A3-11 — SYMBOL/TF SINGLE SOURCE OF TRUTH: `state.symbol` and `state.tf` are the
 * ONLY truth. The DOM (`#symbol-input`, `.tf-btn.active`, …) is pure input/output,
 * mediated by app.js's `goToSymbol(sym)` / `setTf(tf)` (which update `state` FIRST,
 * then reflect to the DOM). readTicket/preview/order read `state.symbol` — never
 * the raw input text — so a typed-but-not-entered symbol can never drive a money
 * action against the wrong coin.
 */

// PERSIST — the ONE table of localStorage keys used across the app. Routing all
// persistence through this table keeps the key strings in a single place (and
// makes the token-key consolidation below auditable at a glance).
var PERSIST = {
  // Auth token: ONE canonical key. TOKEN_LEGACY is the pre-consolidation alias,
  // read once at load (migrateToken below) so an existing user's token is never
  // dropped, then never read again.
  TOKEN: "mexc_local_token",
  TOKEN_LEGACY: "local_api_token",
  DAY_EQUITY: "obsidian_day_equity",
  SIZE_MODE: "obsidian_size_mode",
  TRADE_MARKERS: "obsidian_trade_markers",
  OPEN_TABS: "obsidian_open_tabs",
  WATCHLIST: "obsidian_watchlist",
};

// One-time token migration: if the canonical key is empty but the legacy alias
// holds a token, adopt it into the canonical key. Idempotent — once the
// canonical key is set this never overwrites it, and the legacy key is left in
// place (removing it is unnecessary and only risks data loss). After this,
// authHeaders (api.js) reads the ONE canonical key exclusively.
(function migrateToken() {
  try {
    if (typeof localStorage === "undefined") return;
    var canon = localStorage.getItem(PERSIST.TOKEN);
    var legacy = localStorage.getItem(PERSIST.TOKEN_LEGACY);
    if (!canon && legacy) {
      localStorage.setItem(PERSIST.TOKEN, legacy);
    }
  } catch (_) {
    /* localStorage blocked (private mode / disabled) — nothing to migrate */
  }
})();

var state = {
  symbol: "BTC_USDT",
  tf: "15m",
  htf: "1H",
  chart: null,
  candleSeries: null,
  ema20Series: null,
  ema50Series: null,
  volumeSeries: null,
  priceLines: [],
  health: null,
  account: null,
  _accountLoadPromise: null,
  _accountLoadQueued: false,
  _accountStaleWarned: false,
  proposal: null,
  analyzeBusy: false,
  previewToken: null,
  previewSummary: null,
  orderBusy: false,
  closeBusy: false,
  slBusy: false, // SL→BE move in flight (double-submit guard)
  _slDrag: null, // C3-04b: active SL-line drag {symbol,side,entry,sl,newSl,vol,cs,tp}
  _slHoverGeom: null, // C3-04b: SL geometry under the cursor (hit-zone hover)
  _slOverlayOn: false, // C3-04b: overlay currently interactive (±4px hit zone)
  _slDragWired: false, // C3-04b: pointer/keys wired once (initChart runs once, belt+braces)
  historyClearBusy: false, // history reset in flight
  _historySeq: 0, // latest-wins guard for overlapping history reads
  _cancelBusy: {}, // per-order cancel guards (F6/F7)
  llmLabel: "KI", // active provider label for the analyze spinner
  apiAllowed: null,
  market: null,
  ws: null,
  wsStatus: "off",
  liveBar: null, // { time, open, high, low, close } chart seconds
  lastPx: null,
  _lastTickTs: null, // ms timestamp of the last live price update from ANY source — WS tick OR REST poll (U-02 stale-feed banner, clock 1)
  _lastWsTickTs: null, // ms timestamp of the last REAL WS price frame ONLY — never stamped by the REST poll (E3-01 zombie clock 2)
  _lastAppPingTs: 0, // ms timestamp of the last client app-ping sent (Task 4/E3-01)
  _lastPongTs: null, // ms timestamp of the last app-pong received — socket-alive proof, NOT price data
  // Chart line groups (KI-Analyse, echte Positionen, offene Orders/Trigger)
  proposalLines: [],
  positionLines: [],
  orderLines: [],
  // C3-12: last-applied (price,title,color,style,width,axisLabel) spec set per
  // line group — lets each draw diff and only touch lines that actually
  // changed (applyOptions) instead of remove+recreate every poll (flicker).
  _lineSpecs: { proposal: [], position: [], order: [], ticket: [] },
  // T34 re-render hygiene: fingerprint guards so a poll only rebuilds DOM
  // when something the user sees actually changed.
  _positionsWired: false, // #positions-body delegated listener attached once
  _positionsFp: null, // last positions render fingerprint
  _symbolTabsFp: null, // last symbol-tab bar fingerprint
  _gridStructFp: null, // last overview-grid structure fingerprint
  proposalSymbol: null,
  proposalId: null,
  proposalApplied: false,
  ticketProposalId: null,
  ticketProposalSymbol: null,
  proposalAt: null, // ms timestamp of the underlying analysis (U2-04 drift header)
  _proposalDriftTimer: null,
  showAiLines: true,
  openOrders: null,
  _ordersLoadPromise: null,
  _ordersLoadQueued: false,
  scanBusy: false,
  scanResults: null,
  showZones: true,
  tradeEntryTimes: {}, // symbol -> entry candle time (seconds), for zone start
  tradeMarkers: {}, // symbol -> {sl, tp, side, manual} for manual-mode zones
  slAlarm: {}, // symbol -> already-alarmed flag for manual-SL touch (Task 4)
  _markOffset: {}, // E3-06: symbol -> {offset, ts} exchange Mark−Last basis (added to live ticks)
  sltpMode: "price",
  sizeMode: "position", // "position" = field is notional; "margin" = field is margin
  triggerMode: "auto", // auto = exchange SL/TP; manual = trader manages exit
  allSymbols: [], // full coin pool for the search dropdown
  openTabs: [], // watched coins in the chart tab bar (localStorage-backed)
  _pendingNewTab: false, // set by "+" tab / scanner so the next switch opens a new tab
  fills: [], // account executions of the active symbol (chart markers)
  activeView: "chart", // "chart" | "overview" (E5)
  watchlist: [], // overview watch coins (localStorage-backed)
  overviewData: {}, // symbol -> {last_price, change_pct, candles}
  overviewErrors: {}, // symbol -> error string (V3-02: surfaced per-tile, never swallowed)
  _overviewTimer: null,
  _miniBusy: false, // in-flight /api/mini fetch guard
  _miniLast: 0, // ms timestamp of the last successful /api/mini fetch
  newsItems: [], // /api/news headlines for the overview
  newsErrors: [], // /api/news per-feed errors (V3-04)
  newsStale: false, // /api/news served a stale cached payload (V3-04)
  _newsBusy: false, // in-flight /api/news fetch guard
  _newsLast: 0, // ms timestamp of the last successful /api/news fetch
  reevalBusy: {}, // symbol -> true while /api/reevaluate is in flight (double-click guard)
  reevalResults: {}, // symbol -> last /api/reevaluate response (or {error}), survives re-renders
  // Task 40 (N3-14 Stufe 1): Trades-tab sub-view — "roundtrips" (folded HL
  // fills) or "fills" (raw per-fill ledger, the pre-existing view).
  tradesSubTab: "roundtrips",
  _tradesWired: false, // #trades-body delegated listener attached once
  journalStats: null,
  journalEntries: [],
  _journalSeq: 0,
  // Task 40 (N3-17): active breakdown-row filter chip applied to the
  // journal Entries table, or null when no filter is active. Cleared
  // explicitly (clear button / re-clicking the active row) — never
  // silently reset by a data refresh (loadJournal re-renders with the
  // SAME state.journalFilter still applied).
  journalFilter: null, // {field: "setup_confidence"|"action"|"provider", value: string}
  _journalWired: false, // #journal-body delegated listener attached once
  calibrationData: null, // last /api/journal/stats payload for the Kalibrierung tab
  _calibrationSeq: 0,
  // A3-04: formerly dynamically-created top-level fields — enumerated
  // exhaustively (grep `\bstate\.<name>\s*=`) and pre-declared here so a
  // typo can no longer mint a silent new field once the state is sealed.
  fundingNextSettle: null, // next funding settlement ts (ms/sec) or null
  journalClearBusy: false, // journal reset in flight (double-submit guard)
  wsRetry: 0, // WS reconnect backoff counter
  _chartKey: null, // last-drawn chart identity (symbol|tf) — change guard
  _chartPricePrecision: null, // last applied candle-series price precision — change guard (updateChartPriceFormat); MUST be declared or the sealed store throws on first paint
  _chartResizeObserver: null, // ResizeObserver on the chart container
  _fillAggById: null, // Map: fill id -> aggregated fill (marker tooltips)
  _fillsSeq: 0, // fills fetch sequence guard (drop superseded responses)
  _fillsStaleWarned: false, // one visible warning per fill-read outage
  _symbolsStaleWarned: false, // one visible warning per degraded symbol-list phase
  _fundingCdTimer: null, // funding-countdown setInterval handle
  _lastSlRecon: 0, // ms ts of the last SL reconciliation (5s throttle)
  _markerTooltipEl: null, // hover tooltip DOM node (created once)
  _marketSeq: 0, // /api/market fetch sequence guard
  _ordersSeq: 0, // open-orders fetch sequence guard
  _rtKey: null, // last realtime-wiring chart identity
  _scanAgeTimer: null, // scanner staleness setInterval handle
  _ticketLinesLastDraw: 0, // ms ts of the last ticket-line redraw (throttle)
  _ttlTimer: null, // preview TTL countdown setInterval handle
  _wsReconnect: null, // WS reconnect setTimeout handle
  _zonesRafPending: false, // zones redraw rAF coalescing flag
  // Task 7 (Trade-Management-Layer, frontend): server-truth arming/alert
  // state, mirrored from GET /api/positions/alerts on the existing account
  // poll cadence — the ⚡ Auto-BE toggle NEVER guesses optimistically, it only
  // ever reflects what this poll last reported.
  positionMgmt: {}, // "SYMBOL|side" -> {armed_rules, be_done, alerts} (last poll row)
  armBusy: {}, // "SYMBOL|side" -> true while POST /api/positions/arm is in flight (double-click guard)
  killswitchBusy: false, // POST /api/positions/killswitch in flight guard
  _seenAlertTs: {}, // "SYMBOL|side|kind" -> ts already surfaced (client-side de-dup so a poll never re-toasts the SAME alert)
  _mgmtFeed: [], // recent auto-action/alert feed entries (newest first), capped — the visible "App hat SL auf BE gezogen" trail
};
// A3-04: freeze the SET of top-level keys. Nested objects (slAlarm[sym],
// reevalBusy[sym], _markOffset[sym], _lineSpecs.*, tradeMarkers[sym], …)
// stay mutable — seal only prevents adding/removing TOP-LEVEL state keys, so
// every existing `state.foo.bar = …` and `state.foo[key] = …` keeps working.
// Verified: no `state[dynamicVar] = …`, no `Object.assign(state, …)` exists.
Object.seal(state);
