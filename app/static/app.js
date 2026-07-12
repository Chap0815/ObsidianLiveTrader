/**
 * Obsidian Live Trader (Hyperliquid + MEXC) — trader cockpit chart + ticket.
 * Lightweight Charts expects Unix time in seconds.
 */
(function () {
  "use strict";

  const state = {
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
    proposal: null,
    analyzeBusy: false,
    previewToken: null,
    previewSummary: null,
    orderBusy: false,
    closeBusy: false,
    apiAllowed: null,
    localToken: "", // optional: set window.LOCAL_API_TOKEN or localStorage mexc_local_token
    market: null,
    ws: null,
    wsStatus: "off",
    liveBar: null, // { time, open, high, low, close } chart seconds
    lastPx: null,
    // Chart line groups (KI-Analyse, echte Positionen, offene Orders/Trigger)
    proposalLines: [],
    positionLines: [],
    orderLines: [],
    proposalSymbol: null,
    proposalApplied: false,
    showAiLines: true,
    openOrders: null,
    scanBusy: false,
    scanResults: null,
    showZones: true,
    tradeEntryTimes: {}, // symbol -> entry candle time (seconds), for zone start
    tradeMarkers: {}, // symbol -> {sl, tp, side, manual} for manual-mode zones
    slAlarm: {}, // symbol -> already-alarmed flag for manual-SL touch (Task 4)
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
    _overviewTimer: null,
    _miniBusy: false, // in-flight /api/mini fetch guard
    _miniLast: 0, // ms timestamp of the last successful /api/mini fetch
    newsItems: [], // /api/news headlines for the overview
    _newsBusy: false, // in-flight /api/news fetch guard
    _newsLast: 0, // ms timestamp of the last successful /api/news fetch
    reevalBusy: {}, // symbol -> true while /api/reevaluate is in flight (double-click guard)
    reevalResults: {}, // symbol -> last /api/reevaluate response (or {error}), survives re-renders
  };

  function $(id) {
    return document.getElementById(id);
  }

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    // Only force JSON content-type when body is present (POST/PUT)
    if (!h["Content-Type"] && extra && extra["Content-Type"]) {
      h["Content-Type"] = extra["Content-Type"];
    }
    // F-19: the auth token is normally delivered via an HttpOnly session
    // cookie (sent automatically on same-origin requests), so we no longer
    // read it from the DOM. An explicit state/localStorage token is still
    // honored as a fallback for non-browser use.
    const tok =
      state.localToken ||
      (typeof localStorage !== "undefined" &&
        (localStorage.getItem("mexc_local_token") ||
          localStorage.getItem("local_api_token"))) ||
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

  function updateOrderButtonsEnabled() {
    const btn = $("btn-send-order");
    if (!btn) return;
    const blocked = state.apiAllowed === false;
    btn.disabled = blocked || state.orderBusy;
    btn.title = blocked
      ? "apiAllowed=false — API-Orders für dieses Symbol gesperrt"
      : "Preview → Confirm";
  }

  function fmt(n, digits) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    const d = digits != null ? digits : 4;
    return Number(n).toLocaleString("de-DE", {
      maximumFractionDigits: d,
      minimumFractionDigits: 0,
    });
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

  function setDot(el, ok) {
    if (!el) return;
    el.classList.remove("ok", "bad", "unknown");
    el.classList.add(ok ? "ok" : "bad");
  }

  function initChart() {
    const el = $("chart");
    if (!el || typeof LightweightCharts === "undefined") {
      console.error("Lightweight Charts not available");
      return;
    }

    state.chart = LightweightCharts.createChart(el, {
      layout: {
        background: { color: "#0a0810" },
        textColor: "#b4b0c8",
        fontFamily: '"IBM Plex Mono", Consolas, monospace',
        fontSize: 14, // larger price-axis / time-axis labels (UX-2: were too small)
      },
      grid: {
        vertLines: { color: "#1a1626" },
        horzLines: { color: "#1a1626" },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: "#6b6788", width: 1, style: 3, labelBackgroundColor: "#2b2740" },
        horzLine: { color: "#6b6788", width: 1, style: 3, labelBackgroundColor: "#2b2740" },
      },
      rightPriceScale: {
        borderColor: "#2b2740",
        scaleMargins: { top: 0.08, bottom: 0.22 }, // room for volume below
        entireTextOnly: true, // never draw a half-clipped price label at the edge
      },
      timeScale: {
        borderColor: "#2b2740",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6, // breathing room next to the live candle
        barSpacing: 7,
      },
      watermark: {
        visible: true,
        text: state.symbol || "",
        color: "rgba(157, 154, 182, 0.07)",
        fontSize: 44,
        fontFamily: '"IBM Plex Mono", Consolas, monospace',
      },
      width: el.clientWidth,
      height: Math.max(el.clientHeight, 340),
    });

    // API differs slightly across LWC major versions — prefer v4 style, fall back.
    const chart = state.chart;
    if (typeof chart.addCandlestickSeries === "function") {
      state.candleSeries = chart.addCandlestickSeries({
        upColor: "#4fbe8e",
        downColor: "#e35349",
        borderUpColor: "#4fbe8e",
        borderDownColor: "#e35349",
        wickUpColor: "#4fbe8e",
        wickDownColor: "#e35349",
      });
      state.ema20Series = chart.addLineSeries({
        color: "#5aa6e6",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: "EMA20",
      });
      state.ema50Series = chart.addLineSeries({
        color: "#b07ae0",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: "EMA50",
      });
      state.volumeSeries = chart.addHistogramSeries({
        priceScaleId: "vol",
        priceFormat: { type: "volume" },
        priceLineVisible: false,
        lastValueVisible: false,
      });
    } else if (typeof chart.addSeries === "function") {
      // v5+
      state.candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: "#4fbe8e",
        downColor: "#e35349",
        borderUpColor: "#4fbe8e",
        borderDownColor: "#e35349",
        wickUpColor: "#4fbe8e",
        wickDownColor: "#e35349",
      });
      state.ema20Series = chart.addSeries(LightweightCharts.LineSeries, {
        color: "#5aa6e6",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: "EMA20",
      });
      state.ema50Series = chart.addSeries(LightweightCharts.LineSeries, {
        color: "#b07ae0",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: "EMA50",
      });
      state.volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
        priceScaleId: "vol",
        priceFormat: { type: "volume" },
        priceLineVisible: false,
        lastValueVisible: false,
      });
    } else {
      console.error("Unsupported Lightweight Charts API");
      return;
    }

    // Volume pane pinned to the bottom fifth of the chart
    if (state.volumeSeries) {
      try {
        state.chart
          .priceScale("vol")
          .applyOptions({ scaleMargins: { top: 0.84, bottom: 0 }, visible: false });
      } catch (_) {
        /* older LWC */
      }
    }

    window.addEventListener("resize", scheduleChartResize);

    // The window "resize" event alone misses container-size changes that
    // aren't a viewport resize: grid/layout shifts, a scrollbar appearing,
    // or the overview↔chart toggle (the container is display:none while the
    // overview tab is active, so clientWidth is 0 and any resize during that
    // window would otherwise collapse the canvas). Observe the actual wrap
    // element so the chart always tracks its real box.
    const wrapEl = $("chart-wrap") || el;
    if (typeof ResizeObserver === "function") {
      state._chartResizeObserver = new ResizeObserver(function () {
        scheduleChartResize();
      });
      state._chartResizeObserver.observe(wrapEl);
    }

    // Redraw trade zones whenever the chart is panned/zoomed
    try {
      state.chart.timeScale().subscribeVisibleLogicalRangeChange(function () {
        drawTradeZones();
      });
    } catch (_) {
      /* older LWC */
    }
    sizeTradeOverlay();
  }

  /** Actually apply the chart's box size from its live container. Guarded
   *  against a zero-size read (container hidden via display:none, e.g. while
   *  the overview tab is active) so a stray resize event can't collapse the
   *  chart to nothing. */
  function resizeChart() {
    const el = $("chart");
    if (!state.chart || !el) return;
    const w = el.clientWidth;
    const h = el.clientHeight;
    if (!w || !h) return; // hidden container — nothing to size yet
    state.chart.applyOptions({ width: w, height: Math.max(h, 280) });
    sizeTradeOverlay();
    drawTradeZones();
  }

  // rAF-throttled entry point for both the window resize listener and the
  // ResizeObserver — applyOptions() itself doesn't change the observed
  // element's box, so this can't recurse into another observer callback;
  // the rAF coalescing is just to avoid doing the work more than once per
  // frame when both fire close together.
  let _chartResizeRaf = null;
  function scheduleChartResize() {
    if (_chartResizeRaf != null) return;
    _chartResizeRaf = requestAnimationFrame(function () {
      _chartResizeRaf = null;
      resizeChart();
    });
  }

  /* ── Trade zones overlay: SL (red) / TP (green) fields from entry ─────
     A canvas above the chart draws, per open position, a translucent field
     from the entry price to the SL (down-risk) and to the TP (up-target),
     starting at the entry candle and running into the future, plus hard
     lines at entry/SL/TP. Purely visual; positions/orders drive it. */
  function tradeOverlayCanvas() {
    return $("trade-overlay");
  }

  function sizeTradeOverlay() {
    const cv = tradeOverlayCanvas();
    const el = $("chart");
    if (!cv || !el) return;
    const w = el.clientWidth;
    const h = el.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    cv.style.width = w + "px";
    cv.style.height = h + "px";
    const ctx = cv.getContext("2d");
    if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function drawTradeZones() {
    // Coalesce redraws to one per animation frame. On a lively bbo feed this
    // fires many times/second (every tick via updateLivePnl); without batching
    // that means a full canvas clear+redraw per tick → CPU spikes and flicker.
    if (state._zonesRafPending) return;
    state._zonesRafPending = true;
    const run = function () {
      state._zonesRafPending = false;
      // Cosmetic overlay: a failure here must never bubble up to callers such
      // as loadAccount / updateLivePnl (blank panel / frozen ticks).
      try {
        _drawTradeZones();
      } catch (e) {
        console.error("drawTradeZones", e);
      }
    };
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(run);
    } else {
      run();
    }
  }

  function _drawTradeZones() {
    const cv = tradeOverlayCanvas();
    if (!cv || !state.chart || !state.candleSeries) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const wCss = cv.width / (window.devicePixelRatio || 1);
    const hCss = cv.height / (window.devicePixelRatio || 1);
    ctx.clearRect(0, 0, wCss, hCss);
    if (state.showZones === false) return;
    // Plot width WITHOUT the right price-axis column, so fills/lines never
    // paint over the axis labels (the "buggy" look the trader reported).
    let plotW = wCss;
    try {
      const tw = state.chart.timeScale().width();
      if (Number.isFinite(tw) && tw > 0) plotW = tw;
    } catch (_) {}

    const positions = (state.account && state.account.positions) || [];
    const ts = state.chart.timeScale();
    const series = state.candleSeries;

    positions.forEach(function (p) {
      if (!symMatch(p.symbol, state.symbol)) return;
      const entry = Number(p.entry_price);
      if (!Number.isFinite(entry) || entry <= 0) return;
      const short = String(p.side || "").toLowerCase() === "short";

      // SL/TP levels from the open trigger orders on the exchange (auto mode)
      let sl = null;
      let tp = null;
      const stops = (state.openOrders && state.openOrders.stop_orders) || [];
      stops.forEach(function (s) {
        if (s.symbol && !symMatch(s.symbol, state.symbol)) return;
        const trg = Number(
          s.stopLossPrice != null ? s.stopLossPrice
          : s.takeProfitPrice != null ? s.takeProfitPrice
          : s.triggerPrice != null ? s.triggerPrice : s.price
        );
        if (!Number.isFinite(trg) || trg <= 0) return;
        // classify by side relative to entry
        const below = trg < entry;
        if (short ? !below : below) sl = trg; // SL is adverse side
        else tp = trg;
      });
      // Fallback for MANUAL mode (no exchange trigger): the SL/TP the trader
      // set at entry, remembered on confirm. This is the ONE place the manual
      // trader still gets a visual SL/TP zone.
      const mk = state.tradeMarkers && state.tradeMarkers[String(p.symbol || "").toUpperCase()];
      if (mk) {
        if (sl == null && mk.sl) sl = mk.sl;
        if (tp == null && mk.tp) tp = mk.tp;
      }

      const yEntry = series.priceToCoordinate(entry);
      if (yEntry == null) return;

      // x-start: entry candle time if we recorded it, else left edge
      let xStart = 0;
      const et = state.tradeEntryTimes && state.tradeEntryTimes[p.symbol];
      if (et != null) {
        const xc = ts.timeToCoordinate(et);
        if (xc != null) xStart = Math.max(0, xc);
      }
      const xEnd = plotW;
      // Entry candle scrolled off to the right → avoid negative-width rects.
      if (xStart > xEnd) xStart = 0;

      const vol = Number(p.hold_vol) || 0;
      const cs = Number(
        (state.market && state.market.contract && state.market.contract.contractSize) || 1
      );
      // $ risk = distance * vol * contractSize
      const riskAmt =
        sl != null ? Math.abs(entry - sl) * vol * cs : null;

      function band(price, colorFill, colorLine, label) {
        if (price == null) return;
        const y = series.priceToCoordinate(price);
        if (y == null) return;
        const top = Math.min(yEntry, y);
        const hgt = Math.abs(y - yEntry);
        ctx.fillStyle = colorFill;
        ctx.fillRect(xStart, top, xEnd - xStart, hgt);
        // hard line at the level
        ctx.strokeStyle = colorLine;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(xStart, y);
        ctx.lineTo(xEnd, y);
        ctx.stroke();
        // label with price + $ amount (+ R multiple for TP)
        const amt = Math.abs(entry - price) * vol * cs;
        let extra = " · " + (label === "SL" ? "−" : "+") + fmt(amt, 2);
        if (label === "TP" && riskAmt && riskAmt > 0) {
          extra += " · " + fmt(amt / riskAmt, 1) + "R";
        }
        ctx.fillStyle = colorLine;
        ctx.font = "10px 'IBM Plex Mono', monospace";
        ctx.fillText(label + " " + fmt(price, 4) + extra, xStart + 6, y - 4);
      }

      band(sl, "rgba(227, 83, 73, 0.17)", "#e35349", "SL");
      band(tp, "rgba(79, 190, 142, 0.17)", "#4fbe8e", "TP");

      // entry line (neutral)
      ctx.strokeStyle = short ? "#e6a0a0" : "#a0d8c0";
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xStart, yEntry);
      ctx.lineTo(xEnd, yEntry);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = short ? "#e6a0a0" : "#a0d8c0";
      ctx.font = "10px 'IBM Plex Mono', monospace";
      ctx.fillText((short ? "Short " : "Long ") + fmt(entry, 4), xStart + 6, yEntry - 4);
    });
  }

  function candlesToSeries(candles) {
    if (!Array.isArray(candles)) return [];
    const out = [];
    for (const c of candles) {
      const time = toChartTime(c.time);
      if (time == null) continue;
      out.push({
        time,
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
      });
    }
    // Lightweight Charts requires ascending unique times
    out.sort((a, b) => a.time - b.time);
    return out;
  }

  function volumeToSeries(candles) {
    if (!Array.isArray(candles)) return [];
    const out = [];
    for (const c of candles) {
      const time = toChartTime(c.time);
      if (time == null) continue;
      out.push({
        time,
        value: Number(c.vol) || 0,
        color:
          Number(c.close) >= Number(c.open)
            ? "rgba(79, 190, 142, 0.30)"
            : "rgba(227, 83, 73, 0.30)",
      });
    }
    out.sort((a, b) => a.time - b.time);
    return out;
  }

  function emaToSeries(candles, emaArr) {
    if (!Array.isArray(candles) || !Array.isArray(emaArr)) return [];
    const out = [];
    const n = Math.min(candles.length, emaArr.length);
    for (let i = 0; i < n; i++) {
      const v = emaArr[i];
      if (v == null || Number.isNaN(Number(v))) continue;
      const time = toChartTime(candles[i].time);
      if (time == null) continue;
      out.push({ time, value: Number(v) });
    }
    out.sort((a, b) => a.time - b.time);
    return out;
  }

  function clearPriceLines() {
    if (!state.candleSeries) return;
    for (const pl of state.priceLines) {
      try {
        state.candleSeries.removePriceLine(pl);
      } catch (_) {
        /* ignore */
      }
    }
    state.priceLines = [];
  }

  /* ── Chart line groups ──────────────────────────────────
     Three independent overlays on the candle series:
     - proposalLines: what the KI suggests (Entry/SL/TP + Levels)
     - positionLines: what is actually open (Entry/Liq)
     - orderLines:    resting orders + active SL/TP triggers   */

  function clearLineGroup(arr) {
    if (state.candleSeries && Array.isArray(arr)) {
      for (const pl of arr) {
        try {
          state.candleSeries.removePriceLine(pl);
        } catch (_) {
          /* ignore */
        }
      }
    }
    return [];
  }

  // axisLabel (the price box on the right) OFF by default — it stacks and
  // clutters. The title stays as a small label on the line. Only a few key
  // lines (Liq, current price) keep the numeric axis label.
  function addChartLine(group, price, color, style, title, width, axisLabel) {
    const px = Number(price);
    if (!state.candleSeries || !Number.isFinite(px) || px <= 0) return;
    try {
      group.push(
        state.candleSeries.createPriceLine({
          price: px,
          color: color,
          lineWidth: width || 1,
          lineStyle: style, // 0 solid, 1 dotted, 2 dashed, 4 sparse dotted
          axisLabelVisible: axisLabel === true,
          title: title || "",
        })
      );
    } catch (_) {
      /* chart not ready */
    }
  }

  /** True if there is an open position in the currently shown symbol. */
  function hasActivePosition() {
    const positions = (state.account && state.account.positions) || [];
    return positions.some(function (p) {
      return symMatch(p.symbol, state.symbol) && Math.abs(Number(p.hold_vol) || 0) > 0;
    });
  }

  /** Symbol equality. On Hyperliquid, positions/orders/WS ticks carry a bare
   *  base coin ("BTC") while state.symbol may be the full pair ("BTC_USDC"),
   *  so HL compares base-coin-only (ca.split("_")[0]).
   *  On MEXC, comparing only the base coin is WRONG and unsafe: MEXC lists
   *  both USDT-M and USDC-M futures for the same coin (e.g. BTC_USDT vs
   *  BTC_USDC are two DIFFERENT instruments), so base-coin-only matching
   *  would conflate them and mis-assign positions/orders/stops/markers
   *  between them (F-11). MEXC always reports the full "COIN_QUOTE" symbol
   *  on both sides being compared, so require a full-string match there.
   *  If the exchange isn't known yet, default to the SAFER full-string
   *  compare (a temporary false-negative "no match" is much less harmful
   *  than mixing up two different instruments). */
  function symMatch(a, b) {
    const na = String(a || "").toUpperCase();
    const nb = String(b || "").toUpperCase();
    if (na === "" || nb === "") return false;
    const isHyperliquid = !!(state.health && state.health.exchange === "hyperliquid");
    if (isHyperliquid) {
      const ca = na.split("_")[0];
      const cb = nb.split("_")[0];
      return ca !== "" && ca === cb;
    }
    return na === nb;
  }

  function drawProposalLines() {
    state.proposalLines = clearLineGroup(state.proposalLines);
    if (!state.candleSeries || !state.showAiLines) return;
    // With a live trade open, the chart focuses on the trade (position + zones);
    // the KI planning overlay would just clutter it.
    if (hasActivePosition()) return;
    const p = state.proposal;
    if (!p || p.action === "STAY_OUT") return;
    if (state.proposalSymbol && !symMatch(state.proposalSymbol, state.symbol)) return;

    const g = state.proposalLines;
    // R-multiple labels: how much reward per unit of risk each TP pays
    const risk =
      p.entry_price && p.stop_loss
        ? Math.abs(Number(p.entry_price) - Number(p.stop_loss))
        : null;
    function tpTitle(name, tp) {
      if (!risk || tp == null) return name;
      const r = Math.abs(Number(tp) - Number(p.entry_price)) / risk;
      return name + " +" + r.toFixed(1) + "R";
    }
    // Core trade — hidden once applied to the ticket (ticket lines take over)
    if (!state.proposalApplied) {
      addChartLine(g, p.entry_price, "#b07ae0", 2, "KI Entry", 2);
      addChartLine(g, p.stop_loss, "#e35349", 1, "KI SL -1R");
      addChartLine(g, p.tp1, "#4fbe8e", 1, tpTitle("KI TP1", p.tp1));
    }
    addChartLine(g, p.tp2, "#4fbe8e", 4, tpTitle("KI TP2", p.tp2));
    addChartLine(g, p.tp3, "#4fbe8e", 4, tpTitle("KI TP3", p.tp3));

    // Analysis levels
    const kl = p.key_levels || {};
    addChartLine(g, kl.immediate_support, "#9d9ab6", 4, "Support");
    addChartLine(g, kl.immediate_resistance, "#9d9ab6", 4, "Resist");
    const pools = Array.isArray(kl.major_liquidity_pools)
      ? kl.major_liquidity_pools
      : [];
    pools.slice(0, 3).forEach(function (v) {
      const n = Number(v);
      if (Number.isFinite(n) && n > 0) {
        addChartLine(g, n, "#6b6788", 4, "Pool");
      }
    });
  }

  function drawPositionLines() {
    state.positionLines = clearLineGroup(state.positionLines);
    if (!state.candleSeries) return;
    const positions = (state.account && state.account.positions) || [];
    positions.forEach(function (p) {
      if (!symMatch(p.symbol, state.symbol)) return;
      const short = String(p.side || "").toLowerCase() === "short";
      const entry = Number(p.entry_price);
      addChartLine(
        state.positionLines,
        entry,
        short ? "#e35349" : "#4fbe8e",
        0,
        (short ? "Short" : "Long") + " " + fmt(p.hold_vol, 4),
        2
      );
      // Break-even incl. ~round-trip taker fees (0.06% total) so "SL to BE"
      // actually covers costs, not just the raw entry.
      if (Number.isFinite(entry) && entry > 0) {
        const feeRt = 0.0006;
        const be = short ? entry * (1 - feeRt) : entry * (1 + feeRt);
        addChartLine(state.positionLines, be, "#9d9ab6", 1, "BE≈");
      }
      // Liquidation — the survival line; keeps its numeric axis label.
      addChartLine(state.positionLines, p.liquidate_price, "#c0392b", 3, "⚠ LIQ", undefined, true);
    });
  }

  function drawOrderLines() {
    state.orderLines = clearLineGroup(state.orderLines);
    if (!state.candleSeries) return;
    const d = state.openOrders || {};
    (d.orders || []).forEach(function (o) {
      if (o.symbol && !symMatch(o.symbol, state.symbol)) return;
      addChartLine(state.orderLines, o.price, "#8b7ae6", 2, "Order");
    });
    (d.stop_orders || []).forEach(function (s) {
      if (s.symbol && !symMatch(s.symbol, state.symbol)) return;
      let drew = false;
      const slPx = Number(s.stopLossPrice);
      const tpPx = Number(s.takeProfitPrice);
      if (Number.isFinite(slPx) && slPx > 0) {
        addChartLine(state.orderLines, slPx, "#e35349", 2, "SL aktiv");
        drew = true;
      }
      if (Number.isFinite(tpPx) && tpPx > 0) {
        addChartLine(state.orderLines, tpPx, "#4fbe8e", 2, "TP aktiv");
        drew = true;
      }
      if (!drew) {
        const px = Number(s.triggerPrice != null ? s.triggerPrice : s.price);
        const t = String(s.orderType || "").toLowerCase();
        const isTp = t.indexOf("take") >= 0 || t.indexOf("tp") === 0;
        addChartLine(
          state.orderLines,
          px,
          isTp ? "#4fbe8e" : "#e35349",
          2,
          isTp ? "TP aktiv" : "SL aktiv"
        );
      }
    });
  }

  function drawTicketLines() {
    if (!state.candleSeries) return;
    clearPriceLines();

    // SL/TP resolve through the price/% mode; entry & limit are always prices
    const specs = [
      { price: numOrNull($("ticket-entry")), color: "#5aa6e6", title: "Entry" },
      { price: numOrNull($("ticket-price")), color: "#8b7ae6", title: "Limit" },
      { price: resolveStop(), color: "#e35349", title: "SL" },
      { price: resolveTp(), color: "#4fbe8e", title: "TP1" },
    ];

    for (const s of specs) {
      const val = s.price;
      if (!Number.isFinite(val) || val <= 0) continue;
      const pl = state.candleSeries.createPriceLine({
        price: val,
        color: s.color,
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: s.title,
      });
      state.priceLines.push(pl);
    }
  }

  function updateContext(data) {
    $("ctx-price").textContent = fmt(data.last_price, 6);

    const funding = data.funding || {};
    const fr = funding.fundingRate != null ? funding.fundingRate : null;
    const fEl = $("ctx-funding");
    // keep the countdown span; only replace the rate text node
    const cd = $("ctx-funding-cd");
    if (fEl) {
      fEl.childNodes[0] &&
        (fEl.childNodes[0].nodeValue = fmtPct(fr));
      if (!fEl.childNodes[0]) fEl.textContent = fmtPct(fr);
    }
    // remember next settle time for the live countdown (ms; may be absent)
    const nst = Number(funding.nextSettleTime);
    state.fundingNextSettle =
      Number.isFinite(nst) && nst > 0 ? (nst > 1e12 ? nst : nst * 1000) : null;
    updateFundingCountdown();
    if (cd && !state._fundingCdTimer) {
      state._fundingCdTimer = setInterval(updateFundingCountdown, 1000);
    }

    const ind = (data.ltf && data.ltf.indicators) || {};
    const last = ind.last || {};
    const rsiEl = $("ctx-rsi");
    rsiEl.textContent = last.rsi14 != null ? fmt(last.rsi14, 2) : "—";
    // Overbought / oversold at a glance
    const rsiV = Number(last.rsi14);
    rsiEl.style.color = !Number.isFinite(rsiV)
      ? ""
      : rsiV >= 70
        ? "var(--short)"
        : rsiV <= 30
          ? "var(--long)"
          : "";
    $("ctx-ema").textContent =
      (last.ema20 != null ? fmt(last.ema20, 4) : "—") +
      " / " +
      (last.ema50 != null ? fmt(last.ema50, 4) : "—");

    const st = (data.ltf && data.ltf.structure) || {};
    $("ctx-support").textContent = fmt(st.support, 6);
    $("ctx-resistance").textContent = fmt(st.resistance, 6);
    const rh = st.range_high;
    const rl = st.range_low;
    $("ctx-range").textContent =
      rl != null && rh != null ? fmt(rl, 4) + " – " + fmt(rh, 4) : "—";

    const c = data.contract || {};
    $("ctx-contract").textContent = c.contractSize != null
      ? "size " + c.contractSize + " · maxLev " + (c.maxLeverage ?? "—")
      : "—";
    const allowed = c.apiAllowed;
    state.apiAllowed = allowed === true ? true : allowed === false ? false : null;
    const allowEl = $("ctx-api-allowed");
    if (allowed === true) {
      allowEl.textContent = "true";
      allowEl.style.color = "var(--green)";
    } else if (allowed === false) {
      allowEl.textContent = "false — Orders gesperrt";
      allowEl.style.color = "var(--red)";
    } else {
      allowEl.textContent = "—";
      allowEl.style.color = "";
    }
    updateOrderButtonsEnabled();
  }

  /** Live funding countdown (mm:ss) next to the rate. Null-safe: hidden when
   *  the exchange doesn't report nextSettleTime (e.g. Hyperliquid). */
  function updateFundingCountdown() {
    const cd = $("ctx-funding-cd");
    if (!cd) return;
    const t = state.fundingNextSettle;
    if (!t) {
      cd.textContent = "";
      return;
    }
    let left = Math.floor((t - Date.now()) / 1000);
    if (left < 0) left = 0;
    const h = Math.floor(left / 3600);
    const m = Math.floor((left % 3600) / 60);
    const s = left % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    cd.textContent = " · " + (h > 0 ? h + ":" + mm + ":" + ss : mm + ":" + ss);
  }

  function setChartMeta(symbol, tf, htf, n) {
    const el = $("chart-meta");
    if (el) {
      const live =
        state.wsStatus === "live"
          ? " · LIVE"
          : state.wsStatus === "connecting"
            ? " · …"
            : state.wsStatus === "error"
              ? " · WS err"
              : "";
      el.textContent =
        symbol + " · LTF " + tf + " · HTF " + htf + " · " + n + " bars" + live;
    }
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

  /** Manual positions carry NO exchange stop. When the live price reaches the
   *  SL zone, warn LOUDLY — but only while the browser is open. Dedup per
   *  symbol so it fires once per breach, not on every tick. */
  function checkManualSlAlarm(px) {
    if (px == null || !Number.isFinite(Number(px))) return;
    px = Number(px);
    const positions = (state.account && state.account.positions) || [];
    positions.forEach(function (p) {
      const key = String(p.symbol || "").toUpperCase();
      if (!symMatch(p.symbol, state.symbol)) return; // only compare live price against the active symbol's SL
      const mk = state.tradeMarkers && state.tradeMarkers[key];
      if (!mk || !mk.manual || !mk.sl) { state.slAlarm[key] = false; return; }
      const sl = Number(mk.sl);
      if (!Number.isFinite(sl) || sl <= 0) return;
      const short = String(p.side || "").toLowerCase() === "short";
      const touched = short ? px >= sl : px <= sl;
      if (touched && !state.slAlarm[key]) {
        state.slAlarm[key] = true;
        showToast(
          "⚠ SL BERÜHRT (MANUELL) " + key + " @ " + fmt(px, 4) +
            " — jetzt selbst schließen! Schutz greift nur bei offenem Browser.",
          "err"
        );
        const banner = document.querySelector(
          '.pos-cockpit[data-sym="' + key + '"] .cp-sl-status'
        );
        if (banner) banner.classList.add("cp-sl-alarm");
      } else if (!touched) {
        state.slAlarm[key] = false;
        const banner = document.querySelector(
          '.pos-cockpit[data-sym="' + key + '"] .cp-sl-status'
        );
        if (banner) banner.classList.remove("cp-sl-alarm");
      }
    });
  }

  function setLivePrice(px) {
    if (px == null || !Number.isFinite(Number(px))) return;
    const prev = state.lastPx;
    state.lastPx = Number(px);
    const priceEl = $("ctx-price");
    if (priceEl) {
      priceEl.textContent = fmt(state.lastPx, 6);
      // Tick direction coloring, like on the exchange tape
      if (prev != null && state.lastPx !== prev) {
        priceEl.style.color = state.lastPx > prev ? "var(--long)" : "var(--short)";
      }
    }
    if (state.market) state.market.last_price = state.lastPx;
    const entryEl = $("ticket-entry");
    if (entryEl && !entryEl.value) {
      entryEl.placeholder = String(state.lastPx);
    }
    updateNotionalHint();
    updateLivePnl(state.lastPx); // real-time uPnL on open positions
    try { checkManualSlAlarm(state.lastPx); } catch (e) { console.error("slAlarm", e); }
  }

  /** Live uPnL: recompute from the streaming price without waiting for the
   *  30s account poll. Updates the position tiles in place (no re-render). */
  function updateLivePnl(px) {
    try {
      _updateLivePnl(px);
    } catch (e) {
      console.error("updateLivePnl", e);
    }
  }

  function _updateLivePnl(px) {
    if (px == null || !Number.isFinite(Number(px))) return;
    const el = $("positions-body");
    if (!el) return;

    // Cockpit PnL, live per tick. All open positions now render as cockpit
    // cards (account-wide), but the streamed px is only valid for the
    // currently active symbol — each card still gates on its own data-sym.
    const cockpits = el.querySelectorAll(".pos-cockpit[data-entry]");
    cockpits.forEach(function (cp) {
      const entry = Number(cp.getAttribute("data-entry"));
      const vol = Number(cp.getAttribute("data-vol"));
      const cs = Number(cp.getAttribute("data-cs")) || 1;
      const im = Number(cp.getAttribute("data-im"));
      const short = cp.getAttribute("data-side") === "short";
      const sym = cp.getAttribute("data-sym");
      if (!symMatch(sym, state.symbol)) return; // px is for the active symbol only
      if (!Number.isFinite(entry) || !Number.isFinite(vol)) return;
      const pnl = (px - entry) * vol * cs * (short ? -1 : 1);
      const roe = Number.isFinite(im) && im > 0 ? (pnl / im) * 100 : null;
      const cls = "cp-pnl js-upnl-big " + (pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "");
      const big = cp.querySelector(".js-upnl-big");
      if (big) {
        big.className = cls;
        big.firstChild &&
          (big.firstChild.nodeValue =
            (pnl >= 0 ? "+" : "") + fmt(pnl, 2) + " " + ccy() + " ");
      }
      const sub = cp.querySelector(".js-roe-big");
      if (sub && roe != null) {
        sub.textContent = (roe >= 0 ? "+" : "") + fmt(roe, 1) + "% ROE";
      }
    });
    try { updateRailPnl(px); } catch (_) {}
    drawTradeZones(); // keep zones aligned as price moves
  }

  function applyLiveTrade(px, timeMs) {
    if (px == null || !Number.isFinite(Number(px))) return;
    px = Number(px);
    // Update the live candle FIRST. The chart must keep moving in real time
    // even if a cosmetic update (uPnL/zones) below were to throw.
    if (state.candleSeries) {
      const t = barOpenTimeSec(timeMs || Date.now(), state.tf || "15m");
      let bar = state.liveBar;
      if (!bar || bar.time !== t) {
        // New bar: open at px (or previous close if available)
        const open = bar && bar.close != null ? bar.close : px;
        bar = { time: t, open: open, high: px, low: px, close: px };
        state.liveBar = bar;
      } else {
        bar.high = Math.max(bar.high, px);
        bar.low = Math.min(bar.low, px);
        bar.close = px;
      }
      try {
        state.candleSeries.update(bar);
      } catch (e) {
        // if chart empty, ignore until loadMarket seeds data
      }
    }
    setLivePrice(px);
  }

  function applyLiveCandle(barIn) {
    if (!barIn || !state.candleSeries) return;
    const t = barOpenTimeSec(barIn.time_ms || barIn.time, state.tf || "15m");
    const bar = {
      time: t,
      open: Number(barIn.open),
      high: Number(barIn.high),
      low: Number(barIn.low),
      close: Number(barIn.close),
    };
    state.liveBar = bar;
    setLivePrice(bar.close);
    try {
      state.candleSeries.update(bar);
    } catch (e) {
      /* ignore */
    }
  }

  function stopRealtime() {
    // Cancel pending reconnect so a symbol/TF change does not revive the old WS.
    if (state._wsReconnect) {
      clearTimeout(state._wsReconnect);
      state._wsReconnect = null;
    }
    if (state.ws) {
      try {
        state.ws.close();
      } catch (_) {
        /* ignore */
      }
      state.ws = null;
    }
    state.wsStatus = "off";
  }

  function startRealtime(symbol, tf) {
    stopRealtime();
    symbol = (symbol || state.symbol || "BTC").toUpperCase().trim();
    tf = tf || state.tf || "15m";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url =
      proto +
      "//" +
      location.host +
      "/ws/market?symbol=" +
      encodeURIComponent(symbol) +
      "&tf=" +
      encodeURIComponent(tf);

    state.wsStatus = "connecting";
    setChartMeta(symbol, tf, state.htf || "1H", "…");

    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.error("WS open failed", e);
      state.wsStatus = "error";
      return;
    }
    state.ws = ws;

    ws.onopen = function () {
      state.wsStatus = "connecting";
    };

    ws.onmessage = function (ev) {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      if (!msg || !msg.type) return;

      if (msg.type === "status") {
        if (msg.status === "live" || msg.status === "poll_fallback") {
          state.wsStatus = "live";
          state.wsRetry = 0; // healthy connection resets the backoff
        } else if (msg.status === "error") {
          state.wsStatus = "error";
          console.warn("WS status error", msg.error);
        } else if (msg.status === "connecting") {
          state.wsStatus = "connecting";
        }
        setChartMeta(
          state.symbol,
          state.tf,
          state.htf,
          (state.market &&
            state.market.ltf &&
            state.market.ltf.candles &&
            state.market.ltf.candles.length) ||
            "…"
        );
        const badge = $("rt-badge");
        if (badge) {
          badge.classList.toggle("ok-live", state.wsStatus === "live");
          badge.title =
            state.wsStatus === "live"
              ? "Realtime verbunden"
              : "Realtime: " + state.wsStatus;
        }
        return;
      }

      if (msg.type === "trade") {
        // Bind ticks to the ACTIVE symbol: a stale tick from the previous coin
        // must never be checked against the new position's SL (false alarm).
        if (msg.coin && !symMatch(msg.coin, state.symbol)) return;
        applyLiveTrade(msg.px, msg.time);
        return;
      }
      if (msg.type === "mid") {
        if (msg.coin && !symMatch(msg.coin, state.symbol)) return;
        applyLiveTrade(msg.px, msg.time || Date.now());
        return;
      }
      if (msg.type === "candle" && msg.bar) {
        applyLiveCandle(msg.bar);
      }
    };

    ws.onerror = function () {
      state.wsStatus = "error";
    };

    ws.onclose = function () {
      if (state.ws === ws) {
        state.ws = null;
        state.wsStatus = "off";
        // Exponential backoff (2s → 30s cap) so a dead upstream is not
        // hammered every 2s; resets on the next successful connect.
        state.wsRetry = Math.min((state.wsRetry || 0) + 1, 5);
        const delay = Math.min(2000 * Math.pow(2, state.wsRetry - 1), 30000);
        clearTimeout(state._wsReconnect);
        state._wsReconnect = setTimeout(function () {
          if (!state.ws && state.symbol) {
            startRealtime(state.symbol, state.tf);
          }
        }, delay);
      }
    };
  }

  async function loadMarket(symbol, tf, htf, opts) {
    const silent = !!(opts && opts.silent);
    const _prevSym = String(state.symbol || "").toUpperCase();
    symbol = (symbol || state.symbol || "BTC_USDT").toUpperCase().trim();
    tf = tf || state.tf || "15m";
    htf = htf || state.htf || "1H";
    state.symbol = symbol;
    state.tf = tf;
    state.htf = htf;
    // A real coin switch must tear down the old WS at once so no residual tick
    // of the previous coin is evaluated against the new symbol's SL (audit F2).
    if (!silent && _prevSym && _prevSym !== symbol) {
      stopRealtime();
      // Clear the previous coin's chart overlays IMMEDIATELY so no stale marker,
      // zone, or KI-proposal of the old symbol lingers on the new chart until
      // the new data loads (UB-2 "AVAX marker im BTC-Chart" / UB-3 "Tab-Wechsel
      // zieht Daten mit"). Each draw path re-filters by symMatch, but setMarkers
      // persists the old arrows until called again — so wipe them now.
      state.fills = [];
      try { renderTrades(); } catch (_) {}
      try {
        if (state.candleSeries && typeof state.candleSeries.setMarkers === "function") {
          state.candleSeries.setMarkers([]);
        }
      } catch (_) {}
      if (state.proposalSymbol && !symMatch(state.proposalSymbol, symbol)) {
        state.proposal = null;
        state.proposalSymbol = null;
      }
      try { drawProposalLines(); } catch (_) {}
      try { drawTradeZones(); } catch (_) {}
    }
    // Sequence guard: fast coin switches can let an older fetch resolve AFTER a
    // newer one and overwrite the chart with stale data. Stamp each call and
    // bail if a newer load (different symbol/tf) superseded this one.
    const reqSeq = (state._marketSeq || 0) + 1;
    state._marketSeq = reqSeq;
    const reqKey = symbol + "|" + tf + "|" + htf;

    // Never clobber the field while the user is choosing / during silent polls
    const input = $("symbol-input");
    if (input && !silent && document.activeElement !== input) {
      ensureSymbolOption(symbol);
      input.value = symbol;
    }

    const url =
      "/api/market/" +
      encodeURIComponent(symbol) +
      "?tf=" +
      encodeURIComponent(tf) +
      "&htf=" +
      encodeURIComponent(htf);

    if (!silent) setChartMeta(symbol, tf, htf, "…");

    let res;
    try {
      res = await fetch(url);
    } catch (err) {
      console.error("loadMarket network error", err);
      setChartMeta(symbol, tf, htf, "error");
      return null;
    }

    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || JSON.stringify(body);
      } catch (_) {
        /* ignore */
      }
      console.error("loadMarket failed", res.status, detail);
      setChartMeta(symbol, tf, htf, "HTTP " + res.status);
      return null;
    }

    const data = await res.json();
    // A newer loadMarket (coin/tf switch) started while we awaited → this
    // response is stale; drop it so it can't overwrite the current chart.
    if (reqSeq !== state._marketSeq) return null;
    state.market = data;
    const candles = (data.ltf && data.ltf.candles) || [];
    const indicators = (data.ltf && data.ltf.indicators) || {};
    const chartKey = symbol + "|" + tf;
    const keyChanged = state._chartKey !== chartKey;

    // Capture the viewport BEFORE setData so ANY refresh of the same coin/TF
    // never moves it — silent polls AND explicit reloads alike (Load button,
    // re-clicking the already-active tab/TF, WS-triggered refreshes). Only an
    // actual coin/TF switch (keyChanged) is allowed to reset the view; the
    // user's manual zoom/scroll must otherwise survive every tick/refresh.
    // Exception: user parked at the live edge → keep following new candles.
    let savedRange = null;
    let stickRight = true;
    if (!keyChanged && state.chart) {
      try {
        const ts = state.chart.timeScale();
        stickRight = ts.scrollPosition() > -2; // ~at the right edge
        if (!stickRight) savedRange = ts.getVisibleRange();
      } catch (_) {
        /* ignore */
      }
    }

    if (state.candleSeries) {
      state.candleSeries.setData(candlesToSeries(candles));
    }
    if (state.ema20Series) {
      state.ema20Series.setData(emaToSeries(candles, indicators.ema20));
    }
    if (state.ema50Series) {
      state.ema50Series.setData(emaToSeries(candles, indicators.ema50));
    }
    if (state.volumeSeries) {
      state.volumeSeries.setData(volumeToSeries(candles));
    }

    if (state.chart) {
      const ts = state.chart.timeScale();
      if (keyChanged) {
        // Fresh view: show a wide history by default (~260 candles) so the
        // past price action is visible without zooming out manually.
        const n = candles.length;
        const want = 260;
        if (n > want + 10) {
          try {
            ts.setVisibleLogicalRange({ from: n - want, to: n + 6 });
          } catch (_) {
            ts.fitContent();
          }
        } else {
          ts.fitContent();
        }
        try {
          state.chart.applyOptions({
            watermark: { text: symbol + " · " + tf },
          });
        } catch (_) {
          /* ignore */
        }
      } else if (savedRange) {
        try {
          ts.setVisibleRange(savedRange); // exact same window as before
        } catch (_) {
          /* ignore */
        }
      } else if (stickRight) {
        try {
          ts.scrollToRealTime(); // stay glued to the live candle
        } catch (_) {
          /* ignore */
        }
      }
    }
    state._chartKey = chartKey;

    updateContext(data);
    // Cap the leverage field to what THIS coin allows on the exchange (HL sets
    // a per-coin max: BTC 40×, ETH 25×, most alts 10×). Clamp the typed value
    // only when the coin actually changed, so silent polls never fight the user.
    applyLeverageCap(!silent || keyChanged);
    drawTicketLines();
    drawProposalLines();
    drawPositionLines();
    drawOrderLines();
    applyTradeMarkers(); // fills already loaded; time axis/TF may have changed
    drawTradeZones();
    updateNotionalHint();
    setChartMeta(data.symbol || symbol, tf, htf, candles.length);

    // Seed live bar from last REST candle for seamless WS updates
    if (candles.length) {
      const last = candles[candles.length - 1];
      const t = toChartTime(last.time);
      state.liveBar = {
        time: t,
        open: last.open,
        high: last.high,
        low: last.low,
        close: last.close,
      };
      if (data.last_price != null) setLivePrice(data.last_price);
    }

    // Seed entry ref with last price if empty
    const entryEl = $("ticket-entry");
    if (entryEl && !entryEl.value && data.last_price != null) {
      entryEl.placeholder = String(data.last_price);
    }

    // (Re)start realtime feed on explicit load / symbol change
    if (!silent || state._rtKey !== chartKey) {
      state._rtKey = chartKey;
      startRealtime(data.symbol || symbol, tf);
    }

    return data;
  }

  /** Unmissable environment strip under the topbar. Testnet → amber "no real
   *  funds"; mainnet + armed (live_trading) → red "LIVE"; otherwise a neutral
   *  mainnet/disarmed note. Guards a real-money switch from ever being mistaken
   *  for testnet. */
  function renderEnvBanner(h) {
    const el = $("env-banner");
    if (!el) return;
    h = h || state.health || {};
    const ex = String(h.exchange || "—").toUpperCase();
    const testnet = h.exchange === "hyperliquid" && h.hl_testnet === true;
    const live = h.live_trading === true;
    el.classList.remove("hidden", "env-testnet", "env-live", "env-safe");
    if (testnet) {
      el.classList.add("env-testnet");
      el.textContent = "⚠ TESTNET (" + ex + ") — keine echten Gelder. Sicher zum Testen.";
    } else if (live) {
      el.classList.add("env-live");
      el.textContent =
        "● MAINNET · LIVE (" + ex + ") — echtes Geld. Orders treffen den echten Markt.";
    } else {
      el.classList.add("env-safe");
      el.textContent =
        "MAINNET (" + ex + ") · DISARMED — Trading gesperrt (TRADING_ENABLED=false).";
    }
  }

  async function loadHealth() {
    try {
      const res = await fetch("/api/health");
      if (!res.ok) throw new Error("health " + res.status);
      const h = await res.json();
      state.health = h;
      try { renderEnvBanner(h); } catch (_) {}

      const arm = $("arm-status");
      if (arm) {
        arm.classList.toggle("armed", h.trading_enabled === true);
        const t = arm.querySelector(".arm-text");
        if (t) t.textContent = h.trading_enabled ? "LIVE" : "DISARMED";
      }
      try { renderInstrumentRail(); } catch (_) {}

      // Active-exchange LED + label (works for hyperliquid AND mexc)
      setDot($("dot-exchange"), !!h.exchange_configured);
      const exLabel = $("exchange-label");
      if (exLabel) {
        exLabel.textContent =
          String(h.exchange || "?").toUpperCase() +
          (h.hl_testnet ? " (TESTNET)" : "");
      }
      const llmOk = !!(h.llm_configured || h.claude_configured || h.xai_configured);
      setDot($("dot-xai"), llmOk);
      const llmLabel = $("llm-label");
      if (llmLabel) {
        const p = (h.llm_provider || "claude").toLowerCase();
        llmLabel.textContent =
          p === "xai" || p === "grok" ? "xAI" : "Claude";
      }

      const ccyEl = $("equity-ccy");
      if (ccyEl) {
        ccyEl.textContent = h.exchange === "hyperliquid" ? "USDC" : "USDT";
      }

      if (h.default_symbol && $("symbol-input") && !$("symbol-input").dataset.touched) {
        $("symbol-input").value = h.default_symbol;
        state.symbol = h.default_symbol;
      }
      return h;
    } catch (err) {
      console.error("loadHealth", err);
      setDot($("dot-exchange"), false);
      setDot($("dot-xai"), false);
      return null;
    }
  }

  /** Compact equity breakdown in the side-rail (below the ticket). Pulls from
   *  the already-fetched /api/account snapshot: equity, aggregate unrealized
   *  PnL and used margin summed over open positions, plus free margin. */
  function renderAccounts(data) {
    const acct = data || state.account || {};
    const eqEl = $("acct-equity");
    if (!eqEl) return;
    const c = ccy();
    const eq = Number(acct.equity_usdt);
    eqEl.textContent = Number.isFinite(eq) ? fmt(eq, 2) + " " + c : "—";

    const positions = (acct.positions || []).filter(function (p) {
      return Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    let upnl = 0, haveUpnl = false, used = 0, haveUsed = false;
    positions.forEach(function (p) {
      const u = Number(p.unrealized_pnl);
      if (Number.isFinite(u)) { upnl += u; haveUpnl = true; }
      const im = Number(p.im != null ? p.im : p.margin);
      if (Number.isFinite(im)) { used += im; haveUsed = true; }
    });

    const upnlEl = $("acct-upnl");
    if (upnlEl) {
      if (haveUpnl) {
        upnlEl.textContent = (upnl >= 0 ? "+" : "") + fmt(upnl, 2) + " " + c;
        upnlEl.className =
          "acct-val " + (upnl > 0 ? "pnl-pos" : upnl < 0 ? "pnl-neg" : "");
      } else {
        upnlEl.textContent = "—";
        upnlEl.className = "acct-val";
      }
    }
    const free = Number(acct.available_usdt);
    const freeEl = $("acct-free");
    if (freeEl) freeEl.textContent = Number.isFinite(free) ? fmt(free, 2) + " " + c : "—";
    const usedEl = $("acct-used");
    if (usedEl) usedEl.textContent = haveUsed ? fmt(used, 2) + " " + c : "—";
  }

  function updateEquity(data) {
    const el = $("equity-value");
    if (!el) return;
    if (!data || data.equity_usdt == null || Number.isNaN(Number(data.equity_usdt))) {
      el.textContent = "—";
      return;
    }
    el.textContent = fmt(data.equity_usdt, 2);
    if (data.error) {
      el.title = String(data.error);
    } else {
      el.title = "available " + fmt(data.available_usdt, 2) + " " + ccy();
    }
  }

  function sideTag(side) {
    const s = String(side || "").toLowerCase();
    if (s === "long" || s === "b" || s === "buy" || s === "1") {
      return '<span class="side-tag tag-long">LONG</span>';
    }
    if (s === "short" || s === "a" || s === "sell" || s === "3") {
      return '<span class="side-tag tag-short">SHORT</span>';
    }
    return s ? '<span class="side-tag tag-flat">' + escapeHtml(s.toUpperCase()) + "</span>" : "";
  }

  /** Classify a reduce-only order's price against its position's entry to
   *  guess whether it sits in the stop-loss or take-profit zone. Returns
   *  "SL", "TP", or null when there's no matching position / usable price. */
  function reduceOrderRegion(o) {
    const positions = (state.account && state.account.positions) || [];
    const pos = positions.find(function (p) {
      return o.symbol && symMatch(p.symbol, o.symbol);
    });
    if (!pos) return null;
    const entry = Number(pos.entry_price);
    const price = Number(o.price);
    if (!Number.isFinite(entry) || entry <= 0 || !Number.isFinite(price) || price <= 0) {
      return null;
    }
    const posLong = String(pos.side || "").toLowerCase() !== "short";
    const below = price < entry;
    return (posLong ? below : !below) ? "SL" : "TP";
  }

  /** Order-side tag that understands reduce-only closes. A reduce-only BUY
   *  closes a SHORT (and vice-versa), so show "Reduce Short", not "LONG".
   *  When the order's price falls in the SL/TP zone of the open position,
   *  that's called out too so a resting reduce-only isn't mistaken for a
   *  fresh entry. */
  function orderSideTag(o) {
    const s = String(o.side || "").toLowerCase();
    const isBuy = s === "b" || s === "buy" || s === "long" || s === "1";
    const isSell = s === "a" || s === "sell" || s === "short" || s === "3";
    if (o.reduceOnly) {
      // closes the opposite side of the order
      const region = reduceOrderRegion(o);
      const suffix = region === "SL" ? " (SL-Bereich)" : region === "TP" ? " (TP-Bereich)" : "";
      if (isBuy) return '<span class="side-tag tag-short">↓ Reduce Short' + suffix + '</span>';
      if (isSell) return '<span class="side-tag tag-long">↑ Reduce Long' + suffix + '</span>';
      return '<span class="side-tag tag-flat">Reduce</span>';
    }
    if (isBuy) return '<span class="side-tag tag-long">LONG</span>';
    if (isSell) return '<span class="side-tag tag-short">SHORT</span>';
    return s ? '<span class="side-tag tag-flat">' + escapeHtml(s.toUpperCase()) + "</span>" : "";
  }

  function posKv(label, value, cls) {
    return (
      '<span class="pos-kv"><b>' +
      label +
      "</b><span" +
      (cls ? ' class="' + cls + '"' : "") +
      ">" +
      value +
      "</span></span>"
    );
  }

  function _posDataAttrs(p, sideVal, cs) {
    return (
      ' data-sym="' + escapeHtml(String(p.symbol || "")) + '"' +
      ' data-entry="' + escapeHtml(String(p.entry_price != null ? p.entry_price : "")) + '"' +
      ' data-vol="' + escapeHtml(String(p.hold_vol != null ? p.hold_vol : "")) + '"' +
      ' data-cs="' + escapeHtml(String(cs)) + '"' +
      ' data-im="' + escapeHtml(String(p.im != null ? p.im : "")) + '"' +
      ' data-side="' + sideVal + '"'
    );
  }

  /** Find SL/TP protection for a position from the exchange trigger orders
   *  (+ manual-mode markers). Returns {sl, tp, ordersKnown}. ordersKnown=false
   *  means open orders aren't loaded yet → show "loading", never a false
   *  "no stop-loss" alarm. */
  function findPositionProtection(p) {
    let sl = null;
    let tp = null;
    const oo = state.openOrders;
    // ordersKnown only when the stop-order lookup actually SUCCEEDED. A
    // stops_error means the endpoint failed → SL state is UNKNOWN, not "none",
    // so we must not raise a false "no stop-loss" alarm.
    const ordersKnown = !!(oo && oo.stop_orders && !oo.stops_error);
    const stops = (oo && oo.stop_orders) || [];
    const entry = Number(p.entry_price);
    const hasEntry = Number.isFinite(entry) && entry > 0;
    const short = String(p.side || "").toLowerCase() === "short";
    stops.forEach(function (s) {
      if (s.symbol && !symMatch(s.symbol, p.symbol)) return;
      const slField = Number(s.stopLossPrice);
      const tpField = Number(s.takeProfitPrice);
      // 1) Explicit SL/TP field always wins (MEXC create body echoes these).
      if (Number.isFinite(slField) && slField > 0) { sl = slField; return; }
      if (Number.isFinite(tpField) && tpField > 0) { tp = tpField; return; }
      // 2) Trigger price + orderType label (Hyperliquid: "Stop"/"Take Profit").
      const trg = Number(s.triggerPrice != null ? s.triggerPrice : s.price);
      if (!Number.isFinite(trg) || trg <= 0) return;
      const t = String(s.orderType || "").toLowerCase();
      if (t.indexOf("take") >= 0 || t.indexOf("tp") === 0) { tp = trg; return; }
      if (t.indexOf("stop") >= 0 || t.indexOf("sl") === 0) { sl = trg; return; }
      // 3) No field/label available → classify by side vs entry (mirrors the
      // backend's _classify_unlabeled_trigger): a stop sits on the LOSS side
      // of entry, a take-profit on the PROFIT side. A trigger at/very near
      // entry is a break-even stop — it IS protection, so classify it as SL
      // rather than "unknown" (a real breakeven stop must not read as
      // unprotected). If side/entry can't be resolved, leave it unknown
      // rather than guessing SL (F-12b: a fabricated SL can mask an actually
      // unprotected position, same as a fabricated "unprotected" can hide a
      // real breakeven stop).
      if (!hasEntry) return;
      const beTolerance = entry * 0.001; // within ~0.1% of entry = breakeven
      if (Math.abs(trg - entry) <= beTolerance) { sl = trg; return; }
      const below = trg < entry;
      if (short ? !below : below) sl = trg;
      else tp = trg;
    });
    const mk = state.tradeMarkers && state.tradeMarkers[String(p.symbol || "").toUpperCase()];
    let manual = false;
    if (mk) {
      if (sl == null && mk.sl) { sl = mk.sl; manual = !!mk.manual; }
      if (tp == null && mk.tp) tp = mk.tp;
    }
    return { sl: sl, tp: tp, ordersKnown: ordersKnown, manual: manual };
  }

  /** SL-status banner HTML for a position — green when protected, loud red when
   *  genuinely unprotected. This is the single most important safety nudge. */
  function slStatusBanner(p) {
    const prot = findPositionProtection(p);
    const entry = Number(p.entry_price);
    if (prot.manual && prot.sl != null && Number.isFinite(entry) && entry > 0) {
      const pct = ((prot.sl - entry) / entry) * 100;
      return (
        '<div class="cp-sl-status cp-sl-manual">SL: MANUELL ' + fmt(prot.sl, 4) +
        " (" + (pct >= 0 ? "+" : "") + fmt(pct, 2) + "%) — nur bei offenem Browser</div>"
      );
    }
    if (prot.sl != null && Number.isFinite(entry) && entry > 0) {
      const pct = ((prot.sl - entry) / entry) * 100;
      return (
        '<div class="cp-sl-status cp-sl-ok">🛡 Stop-Loss ' + fmt(prot.sl, 4) +
        " (" + (pct >= 0 ? "+" : "") + fmt(pct, 2) + "%)</div>"
      );
    }
    if (!prot.ordersKnown) {
      return '<div class="cp-sl-status cp-sl-unknown">Stop-Loss-Status wird geladen…</div>';
    }
    return '<div class="cp-sl-status cp-sl-missing">⚠ KEIN STOP-LOSS AKTIV — Position ungeschützt</div>';
  }

  /* ── Instrument rail (signature) ─────────────────────────────────────
     Keeps the critical read — armed state, exchange, equity, and the active
     position's live P&L / protection / liq distance — always visible above
     the workspace, instead of scattered across header + panels. */
  function renderInstrumentRail() {
    const rail = $("instrument-rail");
    if (!rail) return;
    const h = state.health || {};
    const armed = h.trading_enabled === true;
    const armEl = $("ir-arm");
    if (armEl) {
      armEl.className = "ir-arm" + (armed ? " on" : "");
      const t = armEl.querySelector(".ir-arm-txt");
      if (t) t.textContent = armed ? "ARMED" : "DISARMED";
    }
    const exEl = $("ir-exchange");
    if (exEl) {
      exEl.textContent =
        String(h.exchange || "—").toUpperCase() + (h.hl_testnet ? " · TESTNET" : "");
    }
    const acct = state.account || {};
    const eqEl = $("ir-equity");
    if (eqEl) {
      eqEl.textContent =
        acct.equity_usdt != null ? fmt(acct.equity_usdt, 2) + " " + ccy() : "—";
    }
    const inst = $("ir-position");
    if (!inst) return;
    const positions = (acct.positions || []).filter(function (p) {
      return Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    const active = positions.find(function (p) {
      return symMatch(p.symbol, state.symbol);
    });
    if (!active) {
      inst.className = "ir-position ir-flat";
      const msg =
        positions.length > 0
          ? positions.length + " Position(en) offen — Coin wechseln zum Ansehen"
          : "Keine offene Position";
      inst.innerHTML = '<span class="ir-flatmsg">' + escapeHtml(msg) + "</span>";
      return;
    }
    const short = String(active.side || "").toLowerCase() === "short";
    const entry = Number(active.entry_price);
    const vol = Number(active.hold_vol);
    const cs = contractSize();
    const im = Number(active.margin != null ? active.margin : active.im);
    const px = Number(state.lastPx);
    let pnl = active.unrealized_pnl != null ? Number(active.unrealized_pnl) : null;
    if (Number.isFinite(px) && Number.isFinite(entry) && Number.isFinite(vol)) {
      pnl = (px - entry) * vol * cs * (short ? -1 : 1);
    }
    const roe = pnl != null && Number.isFinite(im) && im > 0 ? (pnl / im) * 100 : null;
    const prot = findPositionProtection(active);
    const liq = Number(active.liquidate_price);
    let liqPct = null;
    if (Number.isFinite(liq) && liq > 0 && Number.isFinite(px) && px > 0) {
      liqPct = (Math.abs(px - liq) / px) * 100;
    }
    const pnlCls = pnl == null ? "" : pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "";
    let protHtml;
    if (prot.sl != null && prot.manual) {
      protHtml = '<span class="ir-shield warn">SL MANUELL ' + fmt(prot.sl, 4) + "</span>";
    } else if (prot.sl != null) {
      protHtml = '<span class="ir-shield ok">🛡 SL ' + fmt(prot.sl, 4) + "</span>";
    } else if (!prot.ordersKnown) {
      protHtml = '<span class="ir-shield">SL lädt…</span>';
    } else {
      protHtml = '<span class="ir-shield danger">⚠ KEIN SL</span>';
    }
    // liq gauge: fills as price nears liq (small distance = high fill)
    const gaugeFill =
      liqPct != null ? Math.max(4, Math.min(100, 100 - Math.min(liqPct, 100))) : 0;
    inst.className = "ir-position";
    inst.innerHTML =
      '<div class="ir-cell"><span class="ir-lbl">Position</span>' +
      '<span class="ir-posline"><span class="ir-side ' +
      (short ? "short" : "long") +
      '">' +
      (short ? "SHORT" : "LONG") +
      "</span> " +
      escapeHtml(String(active.symbol || "").split("_")[0]) +
      " · " +
      fmt(vol, 4) +
      "</span></div>" +
      '<div class="ir-cell ir-pnlcell"><span class="ir-lbl">Unrealisiert</span>' +
      '<span class="ir-bigpnl ' +
      pnlCls +
      '"><span class="js-ir-pnl">' +
      (pnl == null ? "—" : (pnl >= 0 ? "+" : "") + fmt(pnl, 2)) +
      "</span>" +
      (roe != null
        ? '<small class="js-ir-roe">' + (roe >= 0 ? "+" : "") + fmt(roe, 1) + "% ROE</small>"
        : "") +
      "</span></div>" +
      '<div class="ir-cell"><span class="ir-lbl">Schutz</span>' +
      protHtml +
      "</div>" +
      '<div class="ir-cell"><span class="ir-lbl">Liq-Distanz</span>' +
      '<span class="ir-v ir-num">' +
      (liqPct != null ? fmt(liqPct, 1) + "%" : "—") +
      "</span>" +
      '<span class="ir-gauge"><i style="width:' +
      gaugeFill +
      '%"></i></span></div>';
  }

  /** Cheap per-tick update of just the rail's live P&L number (active symbol). */
  function updateRailPnl(px) {
    const inst = $("ir-position");
    if (!inst || px == null || !Number.isFinite(Number(px))) return;
    const acct = state.account || {};
    const active = (acct.positions || []).find(function (p) {
      return (
        symMatch(p.symbol, state.symbol) && Math.abs(Number(p.hold_vol) || 0) > 0
      );
    });
    if (!active) return;
    const short = String(active.side || "").toLowerCase() === "short";
    const entry = Number(active.entry_price);
    const vol = Number(active.hold_vol);
    const im = Number(active.margin != null ? active.margin : active.im);
    if (!Number.isFinite(entry) || !Number.isFinite(vol)) return;
    const pnl = (Number(px) - entry) * vol * contractSize() * (short ? -1 : 1);
    const roe = Number.isFinite(im) && im > 0 ? (pnl / im) * 100 : null;
    const big = inst.querySelector(".ir-bigpnl");
    const numEl = inst.querySelector(".js-ir-pnl");
    if (numEl) numEl.textContent = (pnl >= 0 ? "+" : "") + fmt(pnl, 2);
    if (big) {
      big.className =
        "ir-bigpnl " + (pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "");
    }
    const roeEl = inst.querySelector(".js-ir-roe");
    if (roeEl && roe != null) roeEl.textContent = (roe >= 0 ? "+" : "") + fmt(roe, 1) + "% ROE";
  }

  function renderPositions(data) {
    const el = $("positions-body");
    if (!el) return;
    const positions = (data && data.positions) || [];
    const open = positions.filter(function (p) {
      return Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    if (!open.length) {
      el.className = "positions-body muted";
      el.textContent = data && data.error ? String(data.error) : "Keine offenen Positionen.";
      return;
    }

    // ALL open positions render as full cockpit cards, account-wide — not
    // just the active symbol. The active symbol's card is visually
    // highlighted (cp-active) but every other coin is equally full/clickable,
    // no more "hidden + ansehen" compact rows.
    const active = open.filter(function (p) {
      return symMatch(p.symbol, state.symbol);
    });
    const others = open.filter(function (p) {
      return !symMatch(p.symbol, state.symbol);
    });
    const ordered = active.concat(others);

    const cs =
      (state.market && state.market.contract && state.market.contract.contractSize) || 1;

    function cockpit(p) {
      const pnl = Number(p.unrealized_pnl);
      const pnlCls = Number.isFinite(pnl) && pnl !== 0 ? (pnl > 0 ? "pnl-pos" : "pnl-neg") : "";
      const im = Number(p.im);
      const roe = Number.isFinite(pnl) && Number.isFinite(im) && im > 0 ? (pnl / im) * 100 : null;
      const sideVal = String(p.side || "").toLowerCase() === "short" ? "short" : "long";
      const isActive = symMatch(p.symbol, state.symbol);
      // `cs` above is the ACTIVE chart symbol's contractSize — correct only
      // for that symbol. MEXC coins can have different contract sizes, so
      // reusing it for every other open position's notional would be wrong
      // (F-10). For any other symbol, derive notional from exchange-reported
      // margin × leverage instead (independent of contractSize); if those
      // aren't available, show no notional rather than a fabricated number.
      const posLev = Number(p.leverage);
      const posIm = Number(p.im);
      const notional = isActive
        ? Number(p.hold_vol) * cs * Number(p.entry_price || 0)
        : Number.isFinite(posIm) && posIm > 0 && Number.isFinite(posLev) && posLev > 0
          ? posIm * posLev
          : null;
      // Break-even stop incl. ~round-trip taker fees (0.06% total) — same math
      // as the BE chart line. Long: entry above; short: entry below, so the
      // stop at BE actually covers fees rather than sitting at raw entry.
      const beEntry = Number(p.entry_price);
      const beFeeRt = 0.0006;
      const bePrice =
        Number.isFinite(beEntry) && beEntry > 0
          ? sideVal === "short"
            ? beEntry * (1 - beFeeRt)
            : beEntry * (1 + beFeeRt)
          : null;
      const beRow =
        bePrice != null
          ? '<div class="cp-actions"' + _posDataAttrs(p, sideVal, cs) +
            ' data-be="' + escapeHtml(String(bePrice)) + '">' +
            '<span class="cp-actions-label">Stop</span>' +
            '<button type="button" class="cp-be-btn" title="Stop-Loss auf Break-Even (inkl. Gebühren) setzen — ersetzt einen bestehenden Stop">SL → Break-Even</button>' +
            "</div>"
          : "";
      return (
        '<div class="pos-cockpit ' + (sideVal === "short" ? "cp-short" : "cp-long") +
        (isActive ? " cp-active" : "") + '"' +
        _posDataAttrs(p, sideVal, cs) + ">" +
        '<div class="cp-head">' +
        sideTag(p.side) +
        '<span class="cp-sym">' + escapeHtml(p.symbol || "—") + "</span>" +
        '<span class="cp-lev">' + escapeHtml(String(p.leverage != null ? p.leverage : "—")) + "×</span>" +
        "</div>" +
        slStatusBanner(p) +
        '<div class="cp-pnl js-upnl-big ' + pnlCls + '">' +
        (pnl >= 0 ? "+" : "") + fmt(p.unrealized_pnl, 2) + " " + ccy() +
        '<span class="cp-pnl-sub js-roe-big ' + pnlCls + '">' +
        (roe != null ? (roe >= 0 ? "+" : "") + fmt(roe, 1) + "% ROE" : "") + "</span>" +
        "</div>" +
        '<div class="cp-grid">' +
        _cpCell("Entry", fmt(p.entry_price, 4)) +
        _cpCell(
          "Größe",
          fmt(p.hold_vol, 4) + (notional != null ? " · " + fmt(notional, 0) + " " + ccy() : "")
        ) +
        _cpCell("Liq", fmt(p.liquidate_price, 4), "cp-liq") +
        _cpCell("Margin", p.im != null ? fmt(p.im, 2) + " " + ccy() : "—") +
        "</div>" +
        beRow +
        '<div class="cp-close" ' + _posDataAttrs(p, sideVal, cs) + ">" +
        '<span class="cp-close-label">Schließen</span>' +
        '<button type="button" class="cp-close-btn" data-frac="0.25">25%</button>' +
        '<button type="button" class="cp-close-btn" data-frac="0.5">50%</button>' +
        '<button type="button" class="cp-close-btn" data-frac="0.75">75%</button>' +
        '<button type="button" class="cp-close-btn cp-close-full" data-frac="1">100%</button>' +
        "</div>" +
        '<div class="cp-reeval">' +
        '<button type="button" class="cp-reeval-btn" data-sym="' +
        escapeHtml(String(p.symbol || "")) + '">KI: Position bewerten</button>' +
        '<div class="cp-reeval-result" data-sym-result="' +
        escapeHtml(String(p.symbol || "").toUpperCase()) + '">' +
        reevalResultHtml(p.symbol) +
        "</div>" +
        "</div>" +
        "</div>"
      );
    }

    el.className = "positions-body";
    el.innerHTML = ordered.map(cockpit).join("");

    // Partial-close buttons (cockpit) send a fraction; the server closes that
    // share of the CURRENT hold with lot rounding.
    el.querySelectorAll(".cp-close-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation(); // never let this bubble into the card's click-to-open-chart
        const box = btn.closest(".cp-close");
        if (!box) return;
        closePositionFrac(
          box.getAttribute("data-sym"),
          box.getAttribute("data-side"),
          Number(btn.getAttribute("data-frac"))
        );
      });
    });

    // SL → Break-Even (cockpit): places/moves the stop to the fee-adjusted
    // break-even price via /api/orders/modify-sl (new stop → verify → cancel
    // old). A real money action — confirmed before it fires.
    el.querySelectorAll(".cp-be-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation(); // never bubble into the card's click-to-open-chart
        const box = btn.closest(".cp-actions");
        if (!box) return;
        moveStopToBreakEven(
          box.getAttribute("data-sym"),
          box.getAttribute("data-side"),
          Number(box.getAttribute("data-be"))
        );
      });
    });

    // "KI: Position bewerten" — advisory reevaluation of this OPEN position.
    // Never places/moves/closes anything; purely informational.
    el.querySelectorAll(".cp-reeval-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation(); // never let this bubble into the card's click-to-open-chart
        runReevaluate(btn.getAttribute("data-sym"));
      });
    });

    // K4: clicking a position card opens that coin's chart (goToSymbol).
    // Event delegation on the panel so it survives re-renders; ignore clicks
    // that land on a button or other interactive element inside the card
    // (e.g. the partial-close buttons) so those keep their own behavior.
    el.querySelectorAll(".pos-cockpit").forEach(function (card) {
      card.addEventListener("click", function (e) {
        if (e.target.closest("button, a, input, select, textarea")) return;
        const sym = card.getAttribute("data-sym");
        if (sym) goToSymbol(sym);
      });
    });
  }

  function _cpCell(label, value, cls) {
    return (
      '<div class="cp-cell ' + (cls || "") + '">' +
      '<span class="cp-cell-label">' + escapeHtml(label) + "</span>" +
      '<span class="cp-cell-val">' + escapeHtml(String(value)) + "</span></div>"
    );
  }

  /** German label for a /api/reevaluate action code. */
  function _reevalActionLabel(action) {
    switch (String(action || "").toUpperCase()) {
      case "HOLD": return "Halten";
      case "MOVE_SL_BE": return "SL → Break-Even";
      case "PARTIAL_CLOSE": return "Teilweise schließen";
      case "CLOSE": return "Ganz schließen";
      default: return action || "—";
    }
  }

  function _reevalConfCls(conf) {
    const c = String(conf || "").toLowerCase();
    return c === "high" ? "conf-high" : c === "medium" ? "conf-med" : "conf-low";
  }

  function _reevalConfLabel(conf) {
    const c = String(conf || "").toLowerCase();
    return c === "high" ? "Hoch" : c === "medium" ? "Mittel" : "Niedrig";
  }

  /** Render the cached /api/reevaluate result (or error) for one symbol, or
   *  "" when nothing has been fetched yet — the position card then just
   *  shows the "KI: Position bewerten" button with no extra block. */
  function reevalResultHtml(sym) {
    const key = String(sym || "").toUpperCase().trim();
    const entry = key ? state.reevalResults[key] : null;
    if (!entry) return "";
    if (entry.error) {
      return (
        '<div class="cp-reeval-out cp-reeval-error">' + escapeHtml(entry.error) + "</div>"
      );
    }
    const r = entry.reevaluation || {};
    const actionCls = "reeval-action-" + String(r.action || "").toLowerCase();
    let html =
      '<div class="cp-reeval-out">' +
      '<div class="cp-reeval-head">' +
      '<span class="cp-reeval-action ' + actionCls + '">' +
      escapeHtml(_reevalActionLabel(r.action)) + "</span>" +
      (r.confidence
        ? '<span class="pattern-conf ' + _reevalConfCls(r.confidence) + '">' +
          escapeHtml(_reevalConfLabel(r.confidence)) + "</span>"
        : "") +
      "</div>";

    const levels = [];
    if (r.new_sl != null) {
      levels.push('<span class="cp-reeval-lvl"><b>neuer SL:</b> ' + fmt(r.new_sl, 6) + "</span>");
    }
    if (r.new_tp != null) {
      levels.push('<span class="cp-reeval-lvl"><b>neuer TP:</b> ' + fmt(r.new_tp, 6) + "</span>");
    }
    if (r.partial_close_pct != null) {
      levels.push(
        '<span class="cp-reeval-lvl"><b>Anteil:</b> ' + fmt(r.partial_close_pct, 0) + "%</span>"
      );
    }
    if (levels.length) html += '<div class="cp-reeval-levels">' + levels.join(" ") + "</div>";

    if (r.reason) html += '<div class="cp-reeval-reason">' + escapeHtml(r.reason) + "</div>";
    if (r.risk_notes) {
      html += '<div class="cp-reeval-risk">⚠ ' + escapeHtml(r.risk_notes) + "</div>";
    }
    html +=
      '<div class="cp-reeval-foot">Nur Vorschlag — keine automatische Ausführung. ' +
      "Halten/SL/Schließen macht der Trader selbst.</div>";
    html += "</div>";
    return html;
  }

  /** KI reevaluation of one ALREADY OPEN position ("KI: Position bewerten").
   *  Advisory only — never places, moves or closes anything itself. Guards
   *  against double-click/race per symbol via state.reevalBusy. */
  async function runReevaluate(sym) {
    const key = String(sym || "").toUpperCase().trim();
    if (!key) return;
    if (state.reevalBusy[key]) return;
    state.reevalBusy[key] = true;

    const btn = document.querySelector('.cp-reeval-btn[data-sym="' + key + '"]');
    const out = document.querySelector('.cp-reeval-result[data-sym-result="' + key + '"]');
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Bewerte…";
    }
    if (out) {
      out.innerHTML = '<div class="cp-reeval-out cp-reeval-loading">KI bewertet Position…</div>';
    }

    try {
      const res = await apiFetch("/api/reevaluate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: key,
          tf: state.tf || "15m",
          htf: state.htf || "1H",
        }),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }
      if (!res.ok) {
        const detail =
          (data && (data.detail || data.message)) || res.statusText || "Bewertung fehlgeschlagen";
        const msg = typeof detail === "string" ? detail : JSON.stringify(detail);
        state.reevalResults[key] = { error: msg };
      } else {
        state.reevalResults[key] = data;
      }
    } catch (err) {
      state.reevalResults[key] = {
        error: "Netzwerkfehler: " + (err && err.message ? err.message : err),
      };
    } finally {
      state.reevalBusy[key] = false;
      // Re-render just this card's result block; a full renderPositions()
      // would be fine too, but this avoids reshuffling the whole panel.
      const out2 = document.querySelector('.cp-reeval-result[data-sym-result="' + key + '"]');
      if (out2) out2.innerHTML = reevalResultHtml(key);
      const btn2 = document.querySelector('.cp-reeval-btn[data-sym="' + key + '"]');
      if (btn2) {
        btn2.disabled = false;
        btn2.textContent = "KI: Position bewerten";
      }
    }
  }

  /** Compact "also open on other coins" overview — grouped per symbol, click
   *  switches the chart to that coin. So a resting order/stop on another coin is
   *  never forgotten while looking at the current chart. */
  function renderOtherCoinOrders(otherOrders, otherStops) {
    const bySym = {};
    function bump(sym, key) {
      const k = String(sym || "").toUpperCase();
      if (!k) return;
      if (!bySym[k]) bySym[k] = { orders: 0, trig: 0 };
      bySym[k][key]++;
    }
    (otherOrders || []).forEach(function (o) { bump(o.symbol, "orders"); });
    (otherStops || []).forEach(function (s) { bump(s.symbol, "trig"); });
    const syms = Object.keys(bySym).sort();
    if (!syms.length) return "";
    let h = '<div class="other-coins-head">↔ Auch offen auf anderen Coins</div>';
    h += syms
      .map(function (k) {
        const c = bySym[k];
        const parts = [];
        if (c.orders) parts.push(c.orders + " Order" + (c.orders > 1 ? "s" : ""));
        if (c.trig) parts.push(c.trig + " SL/TP");
        return (
          '<button type="button" class="other-coin-row" data-sym="' +
          escapeHtml(k) + '">' +
          '<span class="pos-sym">' + escapeHtml(k) + "</span>" +
          '<span class="other-coin-detail">' + parts.join(" · ") + "</span>" +
          '<span class="other-coin-go">ansehen →</span>' +
          "</button>"
        );
      })
      .join("");
    return h;
  }

  async function loadOpenOrders() {
    const el = $("open-orders-body");
    // Sequence guard: loadOpenOrders() is called from many places (30s poll,
    // cancelOrder, symbol switch, WS fill handler, manual refresh). Two calls
    // can overlap and resolve out of order — e.g. the periodic poll fires
    // right before the user cancels an order, and its (now-stale) response
    // lands AFTER cancelOrder's own refresh, silently resurrecting the
    // just-cancelled order (and its chart line) until the next poll. Stamp
    // each call and drop any response that isn't the most recent one.
    const reqSeq = (state._ordersSeq || 0) + 1;
    state._ordersSeq = reqSeq;
    try {
      const sym =
        ($("symbol-input") && $("symbol-input").value) || state.symbol || "";
      // Fetch ALL coins in one call: the active symbol is shown in detail, the
      // rest as a compact "also open on…" overview so you never forget a
      // resting order/stop on another coin while looking at this chart.
      const res = await apiFetch("/api/orders/open");
      const data = await res.json();
      if (reqSeq !== state._ordersSeq) return null; // superseded by a newer call
      state.openOrders = data && !data.error ? data : null;
      drawOrderLines(); // these already filter to the active symbol internally
      drawTradeZones();
      // Refresh the position cockpit so its SL-status chip reflects the freshly
      // loaded trigger orders (protected vs. unprotected).
      if (state.account) {
        try { renderPositions(state.account); } catch (_) {}
      }
      if (!el) return data;
      if (data.error) {
        el.className = "orders-body muted";
        el.textContent = String(data.error);
        return data;
      }
      const allOrders = data.orders || [];
      const allStops = data.stop_orders || [];
      const isActive = function (s) {
        return !s || !s.symbol || symMatch(s.symbol, sym);
      };
      const orders = allOrders.filter(isActive);
      const stops = allStops.filter(isActive);
      const otherOrders = allOrders.filter(function (o) {
        return o.symbol && !symMatch(o.symbol, sym);
      });
      const otherStops = allStops.filter(function (s) {
        return s.symbol && !symMatch(s.symbol, sym);
      });
      const otherHtml = renderOtherCoinOrders(otherOrders, otherStops);
      if (!orders.length && !stops.length && !otherHtml) {
        el.className = "orders-body muted";
        el.textContent = "Keine offenen Orders";
        return data;
      }
      el.className = "orders-body";
      el.innerHTML = orders
        .map(function (o) {
          const oid = o.orderId != null ? o.orderId : o.order_id;
          // A non-reduce limit order is a resting ENTRY that only fills when the
          // price reaches it — flag it so it is never mistaken for an open position.
          const waiting = o.reduceOnly
            ? ""
            : '<span class="side-tag tag-wait">⏳ wartet auf Fill</span>';
          return (
            '<div class="order-row">' +
            '<span class="pos-sym">' +
            escapeHtml(o.symbol || "—") +
            "</span>" +
            orderSideTag(o) +
            waiting +
            posKv("Vol", fmt(o.vol != null ? o.vol : o.quantity, 4)) +
            posKv("Preis", fmt(o.price, 4)) +
            '<span class="order-oid">#' +
            escapeHtml(String(oid)) +
            "</span>" +
            '<button type="button" class="btn-cancel-order" data-oid="' +
            escapeHtml(String(oid)) +
            '">Cancel</button>' +
            "</div>"
          );
        })
        .join("");
      // Active SL/TP triggers (protective orders on the exchange)
      el.innerHTML += stops
        .map(function (s) {
          const slPx = Number(s.stopLossPrice);
          const tpPx = Number(s.takeProfitPrice);
          const trgPx = Number(s.triggerPrice != null ? s.triggerPrice : s.price);
          const t = String(s.orderType || "").toLowerCase();
          const isTp =
            (Number.isFinite(tpPx) && tpPx > 0 && !(Number.isFinite(slPx) && slPx > 0)) ||
            t.indexOf("take") >= 0;
          const px = Number.isFinite(slPx) && slPx > 0 ? slPx
            : Number.isFinite(tpPx) && tpPx > 0 ? tpPx
            : trgPx;
          return (
            '<div class="order-row order-row-trigger">' +
            '<span class="pos-sym">' + escapeHtml(s.symbol || "—") + "</span>" +
            '<span class="side-tag ' + (isTp ? "tag-long" : "tag-short") + '">' +
            (isTp ? "TP AKTIV" : "SL AKTIV") + "</span>" +
            posKv("Trigger", fmt(px, 4)) +
            "</div>"
          );
        })
        .join("");
      // Cross-coin overview: resting orders / stops on OTHER coins.
      el.innerHTML += otherHtml;
      el.querySelectorAll(".btn-cancel-order").forEach(function (btn) {
        btn.addEventListener("click", function () {
          cancelOrder(btn.getAttribute("data-oid"));
        });
      });
      el.querySelectorAll(".other-coin-row").forEach(function (btn) {
        btn.addEventListener("click", function () {
          switchSymbol(btn.getAttribute("data-sym"));
        });
      });
      return data;
    } catch (err) {
      console.error("loadOpenOrders", err);
      if (el) {
        el.className = "orders-body muted";
        el.textContent = "Orders laden fehlgeschlagen";
      }
      return null;
    }
  }

  async function cancelOrder(orderId) {
    if (!orderId) return;
    try {
      const res = await apiFetch("/api/orders/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          order_id: orderId,
          symbol: state.symbol || undefined,
        }),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        showToast(detailToText(data.detail || data), "err");
        return;
      }
      showToast("Cancel gesendet", data.ok === false ? "err" : "ok");
      loadOpenOrders();
      loadAccount();
      loadHistory();
    } catch (err) {
      showToast("Cancel Fehler: " + (err && err.message), "err");
    }
  }

  async function loadAccount() {
    let data;
    try {
      const res = await apiFetch("/api/account");
      if (!res.ok) throw new Error("account " + res.status);
      data = await res.json();
    } catch (err) {
      console.error("loadAccount fetch", err);
      // Transient fetch error: keep the last-known account instead of wiping
      // equity + positions. Only blank if we never had any data at all.
      if (!state.account) {
        updateEquity(null);
        renderPositions(null);
      }
      return null;
    }
    // Soft error from the exchange (rate-limit etc.) with no usable positions:
    // keep the previous good snapshot rather than blanking the whole panel.
    if (
      data &&
      data.error &&
      !(data.positions && data.positions.length) &&
      state.account &&
      state.account.positions &&
      state.account.positions.length
    ) {
      console.warn("account soft-error, keeping last snapshot:", data.error);
      return state.account;
    }
    state.account = data;
    pruneTradeMarkers();
    // Data application and drawing are isolated: a cosmetic drawing error must
    // NEVER cascade into blanking equity/positions.
    try {
      updateEquity(data);
      renderPositions(data);
      renderInstrumentRail();
      renderAccounts(data);
    } catch (e) {
      console.error("account render", e);
    }
    try {
      drawPositionLines();
      drawTradeZones();
      renderSymbolTabs(); // refresh the long/short dots on the chart tabs
      if (state.activeView === "overview") refreshOverview(); // position tiles + PnL badges
    } catch (e) {
      console.error("account draw", e);
    }
    return data;
  }

  /* ── Trade markers (E7): real executions on the time axis ───────────── */
  async function loadFills() {
    if (!state.symbol) return null;
    // Only HL has a fill-history API today; skip the request entirely on MEXC.
    if (!(state.health && state.health.exchange === "hyperliquid")) return null;
    // Sequence guard: a fast coin switch can leave an in-flight request from
    // the previous symbol resolving AFTER the new symbol's own request,
    // silently overwriting state.fills with stale data (markers flicker).
    // Stamp each call and drop any response that isn't the most recent one.
    const reqSeq = (state._fillsSeq || 0) + 1;
    state._fillsSeq = reqSeq;
    try {
      const res = await apiFetch(
        "/api/fills?symbol=" + encodeURIComponent(state.symbol) + "&limit=100"
      );
      if (!res.ok) return null;
      const data = await res.json();
      if (reqSeq !== state._fillsSeq) return null; // superseded by a newer call
      state.fills = Array.isArray(data.fills) ? data.fills : [];
      applyTradeMarkers();
      try { renderTrades(); } catch (_) {}
      return data;
    } catch (e) {
      console.error("loadFills", e);
      return null;
    }
  }

  /** Many partial fills can land on the same candle (e.g. a large order that
   *  ladders in over 30 small executions). One marker per fill turns into an
   *  unreadable column of arrows + text stacked on one bar, and the actual
   *  entry gets buried. Aggregate fills by (bar, side) into a single marker:
   *  size = sum, price = size-weighted average. Open fills (per Hyperliquid's
   *  `dir`, e.g. "Open Long") get the full-strength color so the entry stays
   *  visually obvious; close-only groups get a dimmed variant. Text labels
   *  are capped to the biggest groups by notional so a busy symbol doesn't
   *  regress back into text spam, and the marker count itself is capped. */
  const TRADE_MARKER_TEXT_CAP = 6;
  const TRADE_MARKER_TOTAL_CAP = 40;

  function applyTradeMarkers() {
    if (
      !state.candleSeries ||
      typeof state.candleSeries.setMarkers !== "function"
    )
      return;
    const tf = state.tf || "15m";
    const groups = new Map(); // "time|side" -> aggregated group

    // Fills older than the loaded chart window are clamped by the chart to the
    // first bar, piling dozens of markers on the left edge. Only mark fills that
    // fall inside the visible candle range.
    const candles =
      (state.market && state.market.ltf && state.market.ltf.candles) || [];
    let firstT = null;
    let lastT = null;
    if (candles.length) {
      firstT = barOpenTimeSec(candles[0].time || candles[0].time_ms, tf);
      lastT = barOpenTimeSec(
        candles[candles.length - 1].time || candles[candles.length - 1].time_ms,
        tf
      );
    }

    (state.fills || []).forEach(function (f) {
      if (!symMatch(f.symbol, state.symbol) || !(Number(f.time) > 0)) return;
      const side = f.side === "buy" ? "buy" : "sell";
      const time = barOpenTimeSec(f.time, tf);
      if (firstT != null && (time < firstT || time > lastT)) return; // outside window
      const sz = Number(f.sz) || 0;
      const px = Number(f.px) || 0;
      const dirStr = String(f.dir || "").toLowerCase();
      const isClose = dirStr.indexOf("close") !== -1;
      const key = time + "|" + side;
      let g = groups.get(key);
      if (!g) {
        g = { time: time, side: side, sz: 0, notional: 0, anyOpen: false, anyClose: false };
        groups.set(key, g);
      }
      g.sz += sz;
      g.notional += sz * px;
      if (isClose) g.anyClose = true;
      else g.anyOpen = true;
    });

    let groupList = Array.from(groups.values());
    groupList.sort(function (a, b) {
      return a.time - b.time;
    });
    // Cap total markers: drop the oldest groups first.
    if (groupList.length > TRADE_MARKER_TOTAL_CAP) {
      groupList = groupList.slice(groupList.length - TRADE_MARKER_TOTAL_CAP);
    }
    // Only label the biggest groups (by notional) once there are more than
    // a handful — otherwise text spam creeps back in on busy symbols.
    let textKeys = null;
    if (groupList.length > TRADE_MARKER_TEXT_CAP) {
      textKeys = new Set(
        groupList
          .slice()
          .sort(function (a, b) {
            return b.notional - a.notional;
          })
          .slice(0, TRADE_MARKER_TEXT_CAP)
          .map(function (g) {
            return g.time + "|" + g.side;
          })
      );
    }

    const markers = groupList.map(function (g) {
      const buy = g.side === "buy";
      const avgPx = g.sz > 0 ? g.notional / g.sz : 0;
      // A bucket with ANY open fill is treated as an entry (full color) even
      // if it also contains a close fill — the entry is what must stand out.
      const closeOnly = g.anyClose && !g.anyOpen;
      const color = closeOnly
        ? buy
          ? "rgba(79, 190, 142, 0.45)" // dimmed: close of a long
          : "rgba(227, 83, 73, 0.45)" // dimmed: close of a short
        : buy
          ? "#4fbe8e"
          : "#e35349";
      const marker = {
        time: g.time,
        position: buy ? "belowBar" : "aboveBar",
        color: color,
        shape: buy ? "arrowUp" : "arrowDown",
      };
      const wantText = !textKeys || textKeys.has(g.time + "|" + g.side);
      if (wantText) {
        marker.text = (buy ? "▲ " : "▼ ") + fmt(g.sz, 4) + " @ " + fmt(avgPx, 4);
      }
      return marker;
    });

    try {
      state.candleSeries.setMarkers(markers);
    } catch (e) {
      console.error("setMarkers", e);
    }
  }

  /** Reference entry price for size/risk math: limit price, else entry ref,
   *  else the live last price. */
  function refEntryPrice() {
    return (
      numOrNull($("ticket-price")) ||
      numOrNull($("ticket-entry")) ||
      (state.market && state.market.last_price) ||
      state.lastPx ||
      null
    );
  }

  function contractSize() {
    const cs =
      state.market && state.market.contract && state.market.contract.contractSize;
    return cs && cs > 0 ? cs : 1;
  }

  /** Effective max leverage = min(exchange per-coin cap, app MAX_LEVERAGE).
   *  Updates the input's max, shows the cap next to the field, and (optionally)
   *  clamps a too-high typed value. Prevents the "blocked at preview" surprise. */
  function applyLeverageCap(clampValue) {
    const contract = state.market && state.market.contract;
    const coinMax = contract && Number(contract.maxLeverage);
    const globalMax = (state.health && Number(state.health.max_leverage)) || 100;
    const max =
      coinMax && coinMax > 0 ? Math.min(coinMax, globalMax) : globalMax;
    const hint = $("lev-max-hint");
    if (hint) hint.textContent = max ? " · max " + max + "×" : "";
    const el = $("ticket-leverage");
    if (!el) return;
    el.max = String(max);
    if (clampValue) {
      const cur = Number(el.value);
      if (Number.isFinite(cur) && cur > max) {
        el.value = String(max);
        showToast("Hebel auf " + max + "× begrenzt (Börsen-Cap)", "warn");
      }
    }
  }

  function currentSide() {
    return ($("ticket-side") && $("ticket-side").value) || "long";
  }

  function sltpMode() {
    return state.sltpMode === "pct" ? "pct" : "price";
  }

  /** Effective stop-loss PRICE — resolves a %-distance to an absolute price.
   *  SL is adverse: below entry for long, above for short. */
  function resolveStop() {
    const raw = numOrNull($("ticket-sl"));
    if (raw == null || raw <= 0) return null;
    if (sltpMode() === "price") return raw;
    const entry = refEntryPrice();
    if (!entry) return null;
    return currentSide() === "long"
      ? entry * (1 - raw / 100)
      : entry * (1 + raw / 100);
  }

  /** Effective take-profit PRICE — favorable: above entry long, below short. */
  function resolveTp() {
    const raw = numOrNull($("ticket-tp1"));
    if (raw == null || raw <= 0) return null;
    if (sltpMode() === "price") return raw;
    const entry = refEntryPrice();
    if (!entry) return null;
    return currentSide() === "long"
      ? entry * (1 + raw / 100)
      : entry * (1 - raw / 100);
  }

  function setSltpMode(mode) {
    state.sltpMode = mode === "pct" ? "pct" : "price";
    document.querySelectorAll(".sltp-mode-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === state.sltpMode);
    });
    const sl = $("ticket-sl");
    const tp = $("ticket-tp1");
    if (state.sltpMode === "pct") {
      if (sl) sl.placeholder = "Abstand %, z.B. 2";
      if (tp) tp.placeholder = "Abstand %, z.B. 4";
    } else {
      if (sl) sl.placeholder = "Kurs, z.B. 61000";
      if (tp) tp.placeholder = "Kurs, z.B. 64000";
    }
    drawTicketLines();
    updateRiskReadout();
  }

  /** USDT notional → contract volume, written to the hidden #ticket-size. */
  function usdtToVol() {
    const usdt = notionalFromField();
    const px = refEntryPrice();
    const cs = contractSize();
    const sizeEl = $("ticket-size");
    if (usdt == null || !px) {
      if (sizeEl) sizeEl.value = "";
      return null;
    }
    const vol = usdt / (px * cs);
    if (sizeEl) sizeEl.value = String(vol);
    return vol;
  }

  /** Size mode: "position" (field = notional) or "margin" (field = margin). */
  function sizeMode() {
    return state.sizeMode === "margin" ? "margin" : "position";
  }

  /** Resolve the size field to a NOTIONAL amount regardless of size mode.
   *  Margin mode: notional = margin × leverage. Returns null when invalid or
   *  (margin mode) leverage is missing/0 so callers show a hint, never NaN. */
  function notionalFromField() {
    const raw = numOrNull($("ticket-usdt"));
    if (raw == null || raw <= 0) return null;
    if (sizeMode() === "position") return raw;
    const lev = numOrNull($("ticket-leverage"));
    if (!lev || lev <= 0) return null;
    return raw * lev;
  }

  /** Switch size mode, converting the field value so the resulting POSITION
   *  stays equivalent (no silent jump). Persists the choice in localStorage. */
  function setSizeMode(mode) {
    const next = mode === "margin" ? "margin" : "position";
    if (next !== sizeMode()) {
      const lev = numOrNull($("ticket-leverage"));
      const el = $("ticket-usdt");
      const cur = numOrNull(el);
      if (el && cur != null && cur > 0 && lev && lev > 0) {
        const conv = next === "margin" ? cur / lev : cur * lev;
        el.value = String(Math.round(conv * 100) / 100);
      }
    }
    state.sizeMode = next;
    try { localStorage.setItem("obsidian_size_mode", next); } catch (_) {}
    document.querySelectorAll(".size-mode-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-size-mode") === next);
    });
    const lbl = $("size-mode-label");
    if (lbl) lbl.textContent = (next === "margin" ? "Margin (" : "Größe (") + ccy() + ")";
    updateRiskReadout();
  }

  function ccy() {
    if (state.health) return state.health.exchange === "hyperliquid" ? "USDC" : "USDT";
    // Before /health resolves, trust the server-rendered currency badge so the
    // UI never flashes the wrong quote currency on Hyperliquid.
    const el = $("equity-ccy");
    const txt = el && el.textContent ? el.textContent.trim() : "";
    return txt === "USDC" || txt === "USDT" ? txt : "USDT";
  }

  /** Modus-abhängiger Readout unter dem Größe-Feld: zeigt Coin-Menge plus den
   *  jeweils ANDEREN Wert (Position-Modus → Margin, Margin-Modus → Position). */
  function updateSizeHint() {
    const el = $("ticket-size-hint");
    if (!el) return;
    const field = numOrNull($("ticket-usdt"));
    const px = refEntryPrice();
    const lev = numOrNull($("ticket-leverage"));
    if (field == null || field <= 0 || !px) {
      el.textContent = sizeMode() === "margin" ? "≈ — · Position —" : "≈ — · Margin —";
      return;
    }
    if (sizeMode() === "margin" && (!lev || lev <= 0)) {
      el.textContent = "Hebel eintragen für Margin-Modus";
      return;
    }
    const notional = notionalFromField();
    if (notional == null) { el.textContent = "≈ —"; return; }
    const base = notional / px; // coin amount = notional / price
    if (sizeMode() === "margin") {
      el.textContent =
        "≈ " + fmt(base, 6) + " " + (state.symbol || "") +
        " · Position " + fmt(notional, 2) + " " + ccy() + " (" + lev + "×)";
    } else {
      const margin = lev && lev > 0 ? notional / lev : null;
      el.textContent =
        "≈ " + fmt(base, 6) + " " + (state.symbol || "") +
        (margin != null ? " · Margin " + fmt(margin, 2) + " " + ccy() + " (" + lev + "×)" : "");
    }
  }

  var TRADE_MARKERS_KEY = "obsidian_trade_markers";
  // Snapshot of the last localStorage state we reconciled against. Needed so
  // saveTradeMarkers() can tell "we intentionally deleted this key (prune)"
  // apart from "we simply never learned about this key" — without it, every
  // save would resurrect pruned markers straight back out of storage.
  var _tradeMarkersSynced = {};

  function _readMarkersRaw() {
    try {
      const raw = localStorage.getItem(TRADE_MARKERS_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  /** Merge our in-memory markers with whatever another tab persisted, keeping
   *  the newer entry per symbol (by ts), so concurrent tabs don't clobber each
   *  other's manual-SL markers (audit F3). A key we deleted locally (e.g. via
   *  pruneTradeMarkers) only stays deleted in storage if no other tab wrote a
   *  newer version of it since our last sync; otherwise their write wins. */
  function saveTradeMarkers() {
    try {
      const stored = _readMarkersRaw();
      const mine = state.tradeMarkers || {};
      const merged = {};
      const keys = new Set(
        Object.keys(stored).concat(Object.keys(mine), Object.keys(_tradeMarkersSynced))
      );
      keys.forEach(function (key) {
        const a = mine[key];
        const b = stored[key];
        if (a && (!b || Number(a.ts || 0) >= Number(b.ts || 0))) {
          merged[key] = a; // ours is newer, or nobody else has it
          return;
        }
        if (b) {
          const synced = _tradeMarkersSynced[key];
          if (!a && synced && Number(b.ts || 0) <= Number(synced.ts || 0)) {
            return; // we deleted it locally and nobody else touched it since -> stays deleted
          }
          merged[key] = b; // theirs is newer, or unknown to us -> keep it
        }
      });
      state.tradeMarkers = merged;
      localStorage.setItem(TRADE_MARKERS_KEY, JSON.stringify(merged));
      _tradeMarkersSynced = merged;
    } catch (_) {}
  }

  function loadTradeMarkers() {
    state.tradeMarkers = _readMarkersRaw();
    _tradeMarkersSynced = Object.assign({}, state.tradeMarkers);
  }

  /** Wipe all persisted manual trade markers (localStorage + in-memory) for a
   *  clean production start. Exchange fills stay (they redraw from state.fills);
   *  only the manual/persisted markers are cleared and the chart is refreshed. */
  function resetTradeMarkers() {
    state.tradeMarkers = {};
    _tradeMarkersSynced = {};
    try {
      localStorage.removeItem(TRADE_MARKERS_KEY);
    } catch (_) {}
    applyTradeMarkers();
  }

  /** Drop persisted markers whose symbol no longer has an open position.
   *  Fresh markers (< 5 min) are kept: confirm() sets the marker before the
   *  position shows up in the next account poll. Markers without ts are
   *  treated as old (pre-feature persistence) and prunable. */
  function pruneTradeMarkers() {
    const positions = (state.account && state.account.positions) || [];
    let changed = false;
    Object.keys(state.tradeMarkers || {}).forEach(function (key) {
      const hasPos = positions.some(function (p) { return symMatch(p.symbol, key); });
      if (hasPos) return;
      const mk = state.tradeMarkers[key] || {};
      const fresh = Number(mk.ts) > 0 && Date.now() - Number(mk.ts) < 5 * 60 * 1000;
      if (fresh) return;
      delete state.tradeMarkers[key];
      changed = true;
    });
    if (changed) saveTradeMarkers();
  }

  /** Live SL/TP readout: turns raw prices into %, risk and reward multiples
   *  so it is obvious the fields are PRICES, not percentages. */
  function updateRiskReadout() {
    usdtToVol();
    updateSizeHint();
    const side = ($("ticket-side") && $("ticket-side").value) || "long";
    const entry = refEntryPrice();
    const sl = resolveStop(); // effective price (handles % mode)
    const tp = resolveTp();
    const usdt = notionalFromField();
    const isLong = side === "long";

    const slEl = $("rr-sl");
    const tpEl = $("rr-tp");
    const rrrEl = $("rr-rrr");

    function pct(price) {
      if (!entry || !price) return null;
      return ((price - entry) / entry) * 100;
    }
    // risk/reward in USDT scales with notional (usdt) since notional = vol*cs*entry
    function money(price) {
      if (!entry || !price || usdt == null) return null;
      return Math.abs((price - entry) / entry) * usdt;
    }

    if (slEl) {
      if (entry && sl) {
        const p = pct(sl);
        const bad = isLong ? sl >= entry : sl <= entry;
        const m = money(sl);
        const eq = state.account && Number(state.account.equity_usdt);
        const eqPct = m != null && eq && eq > 0 ? (m / eq) * 100 : null;
        slEl.innerHTML =
          (p != null ? (p > 0 ? "+" : "") + fmt(p, 2) + "%" : "—") +
          (m != null ? ' <span class="rr-money">−' + fmt(m, 2) + " " + ccy() + "</span>" : "") +
          (eqPct != null ? ' <span class="rr-money">= ' + fmt(eqPct, 2) + "% Equity</span>" : "") +
          (bad ? ' <span class="rr-bad">falsche Seite!</span>' : "");
        slEl.className = "risk-val" + (bad ? " rr-error" : "");
      } else {
        slEl.textContent = "Kurs eintragen";
        slEl.className = "risk-val rr-dim";
      }
    }
    if (tpEl) {
      if (entry && tp) {
        const p = pct(tp);
        const m = money(tp);
        tpEl.innerHTML =
          (p != null ? (p > 0 ? "+" : "") + fmt(p, 2) + "%" : "—") +
          (m != null ? ' <span class="rr-money">+' + fmt(m, 2) + " " + ccy() + "</span>" : "");
        tpEl.className = "risk-val";
      } else {
        tpEl.textContent = "optional";
        tpEl.className = "risk-val rr-dim";
      }
    }
    if (rrrEl) {
      if (entry && sl && tp) {
        // Directional geometry, mirrors the backend's compute_simple_rrr:
        // for a long, reward = tp-entry and risk = entry-sl; for a short,
        // mirrored. abs()-only distances would report a positive RRR even
        // when TP/SL sit on the wrong side of entry (e.g. a long's TP below
        // entry) — exactly the geometry the server rejects — so the UI must
        // not show a misleading "good" ratio before that rejection (F-23).
        const risk = isLong ? entry - sl : sl - entry;
        const reward = isLong ? tp - entry : entry - tp;
        const rrr = risk > 0 && reward > 0 ? reward / risk : null;
        rrrEl.textContent = rrr != null ? "1 : " + fmt(rrr, 2) : "—";
        rrrEl.className =
          "risk-val" + (rrr != null && rrr >= 2 ? " rr-good" : rrr != null ? " rr-warn" : "");
      } else {
        rrrEl.textContent = "—";
        rrrEl.className = "risk-val rr-dim";
      }
    }
    updateLiqReadout(entry, sl, isLong);
  }

  /** Rough liquidation-distance estimate from leverage alone (no exchange
   *  maintenance-margin schedule available client-side): dist ≈ (1/lev)*0.9,
   *  i.e. a bit tighter than the pure 1/lev so it never looks safer than it
   *  is. Drives both the readout next to the leverage field and the
   *  "Liq ≈ price" line in the risk-readout, plus a warning when the
   *  liquidation distance is tighter than the configured stop-loss. */
  function liqDistFraction(lev) {
    const l = Number(lev);
    if (!l || l <= 0) return null;
    // Isolated liq distance ≈ 1/lev − maintenance-margin-fraction. On HL the
    // MMF ≈ 1/(2·maxLeverage) of the coin, which makes this match the real liq
    // (e.g. BTC 40× → 1/40 − 1/80 = 1.25%). Without contract maxLeverage fall
    // back to a small generic buffer. Never returns a wider (too-optimistic)
    // distance than 1/lev.
    const maxLev = Number(
      state.market && state.market.contract && state.market.contract.maxLeverage
    );
    const mmf = maxLev > 0 ? 1 / (2 * maxLev) : (1 / l) * 0.1;
    const dist = 1 / l - mmf;
    // Guard: if lev exceeds the coin cap (shouldn't happen) keep it positive.
    return dist > 0 ? dist : (1 / l) * 0.5;
  }

  function updateLiqReadout(entry, sl, isLong) {
    const lev = numOrNull($("ticket-leverage"));
    const dist = liqDistFraction(lev);
    const liqEl = $("liq-readout");
    const liqWarnEl = $("liq-warn");
    const rrLiqEl = $("rr-liq");

    let liqPrice = null;
    if (dist != null && entry) {
      liqPrice = isLong ? entry * (1 - dist) : entry * (1 + dist);
    }

    if (liqEl) {
      if (dist != null) {
        liqEl.innerHTML =
          "Hebel " + fmt(lev, 0) + "× → Liq ≈ " + (isLong ? "−" : "+") +
          fmt(dist * 100, 1) + "% vom Entry" +
          (liqPrice != null ? ' <span class="liq-price">(' + fmt(liqPrice, 4) + ")</span>" : "");
      } else {
        liqEl.textContent = "";
      }
    }

    if (rrLiqEl) {
      rrLiqEl.textContent = liqPrice != null ? "≈ " + fmt(liqPrice, 4) : "—";
      rrLiqEl.className = "risk-val" + (liqPrice != null ? "" : " rr-dim");
    }

    if (liqWarnEl) {
      let tooClose = false;
      if (dist != null && entry && sl) {
        const slDist = Math.abs(entry - sl) / entry;
        tooClose = slDist > dist;
      }
      liqWarnEl.classList.toggle("hidden", !tooClose);
      liqWarnEl.textContent = tooClose ? "⚠ Liquidation näher als dein Stop!" : "";
    }
  }

  async function suggestVol() {
    setTicketError("");
    const ticket = readTicket();
    if (ticket.stop_loss == null) {
      setTicketError("Erst Stop-Loss-Kurs eintragen — dann kann die Größe berechnet werden.");
      return;
    }
    try {
      const res = await apiFetch("/api/sizing/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({}, ticket, { risk_pct: 2.0 })),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        setTicketError(detailToText(data.detail || data));
        return;
      }
      if (!data.vol || Number(data.vol) <= 0) {
        setTicketError(
          "Kein gate-sicheres Volumen: Risk-Budget liegt unter der Börsen-Mindestgröße. " +
            "Stop enger setzen oder Equity/Risk erhöhen."
        );
        return;
      }
      // Fill the size field; in margin mode store margin (=notional/lev), not notional
      const usdtEl = $("ticket-usdt");
      if (usdtEl && data.notional_usdt != null) {
        let val = data.notional_usdt;
        if (sizeMode() === "margin") {
          const lev = numOrNull($("ticket-leverage")) || 1;
          val = data.notional_usdt / lev;
        }
        usdtEl.value = String(Math.round(val * 100) / 100);
      }
      updateRiskReadout();
      showToast(
        "Größe für 2 % Risiko: " +
          fmt(data.notional_usdt, 2) + " " + ccy() +
          " ≈ " + fmt(data.base_amount, 6) + " " + (state.symbol || ""),
        "ok"
      );
    } catch (err) {
      setTicketError(err && err.message ? err.message : String(err));
    }
  }

  // kept as an alias so older call sites still work
  function updateNotionalHint() {
    updateRiskReadout();
  }

  function shortIso(iso) {
    if (!iso) return "—";
    const s = String(iso);
    // 2026-07-09T12:34:56+00:00 → 07-09 12:34
    const m = s.match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (m) return m[2] + "-" + m[3] + " " + m[4] + ":" + m[5];
    return s.slice(0, 16);
  }

  function renderHistory(data) {
    const body = $("history-body");
    if (!body) return;

    if (!data) {
      body.className = "placeholder";
      body.textContent = "Historie nicht geladen.";
      return;
    }

    const proposals = Array.isArray(data.proposals) ? data.proposals : [];
    const orders = Array.isArray(data.orders) ? data.orders : [];

    if (!proposals.length && !orders.length) {
      body.className = "placeholder";
      body.textContent = "Noch keine Einträge.";
      return;
    }

    let html = '<div class="history-content">';

    html += '<div class="history-section"><h3>Proposals (letzte ' + proposals.length + ")</h3>";
    if (proposals.length) {
      html +=
        '<table class="history-table"><thead><tr>' +
        "<th>Zeit</th><th>Symbol</th><th>Action</th><th>Entry</th><th>SL</th><th>RRR</th>" +
        "</tr></thead><tbody>";
      for (const row of proposals) {
        const p = row.proposal || {};
        html +=
          "<tr>" +
          "<td>" +
          escapeHtml(shortIso(row.created_at)) +
          "</td>" +
          "<td>" +
          escapeHtml(row.symbol || "—") +
          "</td>" +
          '<td class="hist-action action-' +
          escapeHtml(p.action || "") +
          '">' +
          escapeHtml(p.action || "—") +
          "</td>" +
          "<td>" +
          (p.entry_price != null ? fmt(p.entry_price, 4) : "—") +
          "</td>" +
          "<td>" +
          (p.stop_loss != null ? fmt(p.stop_loss, 4) : "—") +
          "</td>" +
          "<td>" +
          (p.rrr != null ? fmt(p.rrr, 2) : "—") +
          "</td>" +
          "</tr>";
      }
      html += "</tbody></table>";
    } else {
      html += '<p class="muted history-empty">Keine Proposals.</p>';
    }
    html += "</div>";

    html += '<div class="history-section"><h3>Orders (letzte ' + orders.length + ")</h3>";
    if (orders.length) {
      html +=
        '<table class="history-table"><thead><tr>' +
        "<th>Zeit</th><th>Symbol</th><th>Side</th><th>Status</th><th>Fehler</th>" +
        "</tr></thead><tbody>";
      for (const o of orders) {
        const st = o.status || "—";
        // Anything with error/unverified/partial is a red flag
        const stClass =
          st === "placed" || st === "cancelled" || st === "closed"
            ? "hist-ok"
            : /error|unverified|unknown|partial/.test(st)
              ? "hist-err"
              : "";
        html +=
          "<tr>" +
          "<td>" +
          escapeHtml(shortIso(o.created_at)) +
          "</td>" +
          "<td>" +
          escapeHtml(o.symbol || "—") +
          "</td>" +
          "<td>" +
          escapeHtml(o.side || "—") +
          "</td>" +
          '<td class="' +
          stClass +
          '">' +
          escapeHtml(st) +
          "</td>" +
          "<td class=\"hist-err\">" +
          escapeHtml(o.error ? String(o.error).slice(0, 80) : "—") +
          "</td>" +
          "</tr>";
      }
      html += "</tbody></table>";
    } else {
      html += '<p class="muted history-empty">Keine Orders.</p>';
    }
    html += "</div></div>";

    body.className = "history-body";
    body.innerHTML = html;
  }

  async function loadHistory() {
    const body = $("history-body");
    try {
      const res = await apiFetch("/api/history?limit=20");
      if (!res.ok) throw new Error("history " + res.status);
      const data = await res.json();
      renderHistory(data);
      return data;
    } catch (err) {
      console.error("loadHistory", err);
      if (body) {
        body.className = "placeholder";
        body.textContent =
          "Historie-Fehler: " + (err && err.message ? err.message : err);
      }
      return null;
    }
  }

  /** Trades tab: executed fills (state.fills) as a compact ledger. Reuses the
   *  same fill objects that drive the chart markers, newest first. HL-only
   *  today (loadFills no-ops on MEXC) → empty state elsewhere. */
  function renderTrades() {
    const el = $("trades-body");
    if (!el) return;
    const fills = Array.isArray(state.fills) ? state.fills.slice() : [];
    if (!fills.length) {
      el.className = "trades-body muted";
      el.innerHTML =
        '<div class="trades-empty">Noch keine ausgeführten Trades für ' +
        escapeHtml(String(state.symbol || "—").split("_")[0]) +
        ".</div>";
      return;
    }
    fills.sort(function (a, b) {
      return (Number(b.time) || 0) - (Number(a.time) || 0);
    });
    el.className = "trades-body";
    el.innerHTML =
      '<div class="trades-list">' +
      fills
        .map(function (f) {
          const side = f.side === "buy" ? "buy" : "sell";
          const sym = String(f.symbol || state.symbol || "—").split("_")[0];
          const t = Number(f.time);
          const iso = Number.isFinite(t) && t > 0 ? new Date(t).toISOString() : null;
          return (
            '<div class="trade-row">' +
            '<span class="trade-sym">' + escapeHtml(sym) + "</span>" +
            '<span class="trade-side ' + side + '">' +
            (side === "buy" ? "BUY" : "SELL") + "</span>" +
            '<span class="trade-sz">' + fmt(f.sz, 4) + "</span>" +
            '<span class="trade-px">' + fmt(f.px, 4) + "</span>" +
            '<span class="trade-time">' + (iso ? escapeHtml(relTime(iso)) : "") + "</span>" +
            "</div>"
          );
        })
        .join("") +
      "</div>";
  }

  /** Tabbed data panel: toggle the active pane, contextual action buttons and
   *  trigger an immediate render/refresh of the selected tab's data. Polling
   *  keeps writing into hidden panes; they simply show when re-selected. */
  function switchDataTab(name) {
    const tabs = document.querySelectorAll(".data-tab");
    if (!tabs.length) return;
    state.dataTab = name;
    tabs.forEach(function (t) {
      const on = t.getAttribute("data-tab") === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".data-pane").forEach(function (p) {
      const on = p.getAttribute("data-pane") === name;
      p.classList.toggle("active", on);
      p.classList.toggle("hidden", !on);
    });
    document.querySelectorAll(".data-act").forEach(function (b) {
      b.classList.toggle("hidden", b.getAttribute("data-for") !== name);
    });
    if (name === "positions") {
      try { renderPositions(state.account); } catch (_) {}
    } else if (name === "orders") {
      loadOpenOrders();
    } else if (name === "history") {
      loadHistory();
    } else if (name === "trades") {
      try { renderTrades(); } catch (_) {}
      loadFills();
    }
  }

  async function clearHistory() {
    if (state.historyClearBusy) return;
    const text =
      "Alles für sauberen Produktionsstart zurücksetzen?\n\n" +
      "• Lokale Audit-Historie (KI-Vorschläge + Order-Log)\n" +
      "• Persistierte Chart-Marker\n\n" +
      "Börsen-Positionen und -Fills bleiben unberührt. Nicht umkehrbar.";
    if (!window.confirm(text)) return;
    state.historyClearBusy = true;
    try {
      const res = await apiFetch("/api/history/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok || data.ok === false) {
        showToast(detailToText(data.detail || data), "err");
        return;
      }
      resetTradeMarkers(); // clean slate: also drop persisted chart markers
      const del = data.deleted || {};
      showToast(
        "Zurückgesetzt: " +
          fmt(del.proposals, 0) +
          " Vorschläge, " +
          fmt(del.orders, 0) +
          " Orders, Chart-Marker",
        "ok"
      );
      loadHistory();
    } catch (err) {
      showToast(
        "Historie leeren fehlgeschlagen: " + (err && err.message),
        "err"
      );
    } finally {
      state.historyClearBusy = false;
    }
  }

  function setApplyEnabled(enabled) {
    const btn = $("btn-apply-proposal");
    if (!btn) return;
    btn.disabled = !enabled;
    if (!enabled) {
      btn.title = "Nur bei Aktion ungleich STAY_OUT";
    } else {
      btn.title = "Proposal in Order-Ticket übernehmen";
    }
  }

  function renderProposal(data) {
    const body = $("proposal-body");
    if (!body) return;

    if (!data || !data.proposal) {
      body.className = "placeholder";
      body.innerHTML = "Noch keine Analyse. Klicke Analysieren.";
      state.proposal = null;
      state.proposalSymbol = null;
      drawProposalLines();
      setApplyEnabled(false);
      return;
    }

    const p = data.proposal;
    const anno = data.annotations || {};
    state.proposal = p;
    state.proposalSymbol = data.symbol || state.symbol;
    state.proposalApplied = false;
    const stayOut = p.action === "STAY_OUT";
    setApplyEnabled(!stayOut);
    drawProposalLines();

    const levels = p.key_levels || {};
    const mgmt = p.management || {};
    const fmtN = (v) => (v == null || v === "" ? "—" : fmt(v, 6));

    // R-multiples for the TP tiles (reward per unit of risk)
    const risk =
      p.entry_price != null && p.stop_loss != null
        ? Math.abs(Number(p.entry_price) - Number(p.stop_loss))
        : null;
    const rMult = (tp) =>
      risk && tp != null && p.entry_price != null
        ? "+" + fmt(Math.abs(Number(tp) - Number(p.entry_price)) / risk, 1) + "R"
        : "";

    // Header: big action badge + symbol
    let html =
      '<div class="an-head">' +
      '<span class="proposal-action action-' + escapeHtml(p.action || "") + '">' +
      escapeHtml((p.action || "—").replace(/_/g, " ")) + "</span>" +
      '<span class="an-sym">' + escapeHtml(state.proposalSymbol || state.symbol || "") + "</span>" +
      '<span class="an-lev">' + escapeHtml(p.recommended_leverage || "") + "</span>" +
      "</div>";

    // Multi-timeframe trend row (inspired by the MTF signal tables)
    html +=
      '<div class="mtf-row">' +
      _trendCell("HTF", data.htf || state.htf || "HTF", p.htf_trend) +
      _trendCell("LTF", data.tf || state.tf || "LTF", p.ltf_trend) +
      "</div>";

    // Overall setup confidence (structured signal quality from the model).
    // The badge itself is KEPT — it now lives as the value of the
    // "Setup-Konfidenz" tile in the unified header grid below.
    const sconf = String(p.setup_confidence || "").toLowerCase();
    let confBadge = "";
    if (sconf) {
      const scCls =
        sconf === "high" ? "conf-high" : sconf === "medium" ? "conf-med" : "conf-low";
      const scLabel =
        sconf === "high" ? "Hoch" : sconf === "medium" ? "Mittel" : "Niedrig";
      confBadge = '<span class="pattern-conf ' + scCls + '">' + scLabel + "</span>";
    }

    // Detected chart pattern (only shown when the model found a real one)
    const pat = (p.chart_pattern || "").trim();
    if (pat && pat.toLowerCase() !== "none") {
      const conf = (p.pattern_confidence || "").toLowerCase();
      const confCls =
        conf === "high" ? "conf-high" : conf === "medium" ? "conf-med" : "conf-low";
      html +=
        '<div class="pattern-badge">' +
        '<span class="pattern-icon">◇</span>' +
        '<span class="pattern-name">' + escapeHtml(pat) + "</span>" +
        (conf ? '<span class="pattern-conf ' + confCls + '">' +
          escapeHtml(conf) + "</span>" : "") +
        "</div>";
    }

    // Unified tile header: Action + Setup-Konfidenz are ALWAYS shown as tiles
    // (even on STAY_OUT); Entry/SL/TP/RRR join the grid when there's a trade.
    // One clean grid instead of prose+fields mixed; mono + tabular-nums values,
    // directional green/red where meaningful. Analyst prose stays BELOW.
    const actDir = _actionDir(p.action);
    html += '<div class="setup-tiles">';
    html += _tileHtml(
      "Aktion",
      '<span class="tile-action-val">' +
        escapeHtml((p.action || "—").replace(/_/g, " ")) +
        "</span>",
      "",
      "tile-action" + (actDir ? " tile-dir-" + actDir : "")
    );
    html += _tileHtml("Setup-Konfidenz", confBadge || "—", "", "tile-conf");
    if (!stayOut) {
      html +=
        _tile("Entry", fmtN(p.entry_price),
              anno.entry_vs_last_pct != null ? fmt(anno.entry_vs_last_pct, 2) + "% vs. Preis" : "", "tile-entry") +
        _tile("Stop-Loss", fmtN(p.stop_loss),
              anno.sl_distance_atr != null ? fmt(anno.sl_distance_atr, 2) + "× ATR" : "-1R", "tile-sl") +
        _tile("Take-Profit 1", fmtN(p.tp1), rMult(p.tp1), "tile-tp") +
        (p.tp2 != null ? _tile("Take-Profit 2", fmtN(p.tp2), rMult(p.tp2), "tile-tp") : "") +
        (p.tp3 != null ? _tile("Take-Profit 3", fmtN(p.tp3), rMult(p.tp3), "tile-tp") : "") +
        _tile("Chance/Risiko", p.rrr != null ? "1 : " + fmt(p.rrr, 2) : "—",
              p.rrr != null && p.rrr >= 2 ? "solide" : "knapp", "tile-rrr");
    }
    html += "</div>";
    if (!stayOut && p.trigger_entry_zone) {
      html += '<div class="an-zone"><b>Einstiegszone:</b> ' +
        escapeHtml(p.trigger_entry_zone) + "</div>";
    }

    // Key levels + funding as a compact strip
    html +=
      '<div class="an-levels">' +
      _lvl("Support", fmtN(levels.immediate_support)) +
      _lvl("Resistance", fmtN(levels.immediate_resistance)) +
      _lvl("Funding", p.funding_alert ? p.funding_alert : "neutral") +
      "</div>";

    // Reasoning
    html += '<div class="an-reason">' + escapeHtml(p.rationale || "") + "</div>";

    // Management (structured invalidation price first, then any free-text)
    const invPx = Number(p.invalidation_price);
    const hasInvPx = Number.isFinite(invPx) && invPx > 0;
    if (mgmt.move_sl_to_be || mgmt.early_invalidation || hasInvPx) {
      html +=
        '<div class="an-mgmt">' +
        (mgmt.move_sl_to_be ? '<div><b>SL→BE:</b> ' + escapeHtml(mgmt.move_sl_to_be) + "</div>" : "") +
        (hasInvPx
          ? '<div><b>Invalidierung:</b> ' + fmtN(invPx) +
            (p.invalidation_tf ? " (" + escapeHtml(p.invalidation_tf) + ")" : "") + "</div>"
          : "") +
        (mgmt.early_invalidation ? '<div><b>Hinweis:</b> ' + escapeHtml(mgmt.early_invalidation) + "</div>" : "") +
        "</div>";
    }
    html += '<div class="an-foot">Nur Vorschlag — jede Order wird von den Risk-Gates neu geprüft. Kein Auto-Trading.</div>';

    body.className = "proposal-body analysis-mode";
    body.innerHTML = html;
  }

  function _trendLabel(t) {
    const s = String(t || "").toLowerCase();
    if (s === "bullish") return { icon: "▲", cls: "tr-up", txt: "Bullish" };
    if (s === "bearish") return { icon: "▼", cls: "tr-down", txt: "Bearish" };
    if (s === "ranging") return { icon: "▬", cls: "tr-flat", txt: "Range" };
    return { icon: "•", cls: "tr-unknown", txt: t || "—" };
  }

  function _trendCell(label, tf, trend) {
    const t = _trendLabel(trend);
    return (
      '<div class="mtf-cell ' + t.cls + '">' +
      '<span class="mtf-tf">' + escapeHtml(String(tf)) + "</span>" +
      '<span class="mtf-arrow">' + t.icon + "</span>" +
      '<span class="mtf-trend">' + escapeHtml(t.txt) + "</span>" +
      "</div>"
    );
  }

  function _tile(label, value, sub, cls) {
    return (
      '<div class="setup-tile ' + (cls || "") + '">' +
      '<span class="tile-label">' + escapeHtml(label) + "</span>" +
      '<span class="tile-value">' + escapeHtml(String(value)) + "</span>" +
      (sub ? '<span class="tile-sub">' + escapeHtml(String(sub)) + "</span>" : "") +
      "</div>"
    );
  }

  /** Like _tile but the value is trusted HTML (e.g. a confidence badge chip),
   *  not an escaped string. Label and sub are still escaped. */
  function _tileHtml(label, valueHtml, sub, cls) {
    return (
      '<div class="setup-tile ' + (cls || "") + '">' +
      '<span class="tile-label">' + escapeHtml(label) + "</span>" +
      '<span class="tile-value">' + (valueHtml || "—") + "</span>" +
      (sub ? '<span class="tile-sub">' + escapeHtml(String(sub)) + "</span>" : "") +
      "</div>"
    );
  }

  /** Directional bucket for a TradeAction — drives the green/red tile accent. */
  function _actionDir(action) {
    const a = String(action || "").toUpperCase();
    if (a === "STRONG_BUY" || a === "BUY") return "long";
    if (a === "SELL" || a === "STRONG_SHORT") return "short";
    return "";
  }

  function _lvl(label, value) {
    return (
      '<div class="an-lvl"><span class="an-lvl-label">' + escapeHtml(label) +
      "</span><span class=\"an-lvl-val\">" + escapeHtml(String(value)) + "</span></div>"
    );
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showProposalError(msg) {
    const body = $("proposal-body");
    if (!body) return;
    body.className = "proposal-body";
    body.innerHTML = '<p class="error-text">' + escapeHtml(msg) + "</p>";
    state.proposal = null;
    setApplyEnabled(false);
  }

  function parseLeverageHint(text) {
    if (!text) return null;
    const m = String(text).match(/(\d+)/);
    if (!m) return null;
    const n = parseInt(m[1], 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  function applyProposalToTicket() {
    const p = state.proposal;
    if (!p || p.action === "STAY_OUT") return;

    const sideEl = $("ticket-side");
    const typeEl = $("ticket-type");
    const entryEl = $("ticket-entry");
    const priceEl = $("ticket-price");
    const slEl = $("ticket-sl");
    const tp1El = $("ticket-tp1");
    const levEl = $("ticket-leverage");

    // Proposal gives absolute prices → make sure we're in price mode
    setSltpMode("price");

    const isLong = p.action === "BUY" || p.action === "STRONG_BUY";
    const isShort = p.action === "SELL" || p.action === "STRONG_SHORT";
    if (sideEl && (isLong || isShort)) {
      sideEl.value = isLong ? "long" : "short";
    }

    if (entryEl && p.entry_price != null) {
      entryEl.value = String(p.entry_price);
    }
    // Prefer limit when entry present; user can switch to market
    if (typeEl && p.entry_price != null) {
      typeEl.value = "limit";
    }
    if (priceEl && p.entry_price != null) {
      priceEl.value = String(p.entry_price);
    }
    if (slEl && p.stop_loss != null) {
      slEl.value = String(p.stop_loss);
    }
    if (tp1El && p.tp1 != null) {
      tp1El.value = String(p.tp1);
    }
    const lev = parseLeverageHint(p.recommended_leverage);
    if (levEl && lev != null) {
      levEl.value = String(lev);
    }

    syncTicketSegments();
    // The proposal turned the ticket into a LIMIT order (pullback entry). Make
    // that unmistakable — otherwise the trader sends a resting limit thinking
    // they are in the market now (exactly the AVAX confusion).
    if (typeEl && typeEl.value === "limit" && p.entry_price != null) {
      showToast(
        "⏳ Als LIMIT bei " + fmt(p.entry_price, 4) + " übernommen — die Order " +
          "wartet, bis der Kurs dieses Niveau erreicht. Für sofortigen Einstieg " +
          'auf „Market" wechseln.',
        null
      );
    }
    // Core KI lines are now represented by the ticket lines — keep only levels
    state.proposalApplied = true;
    drawProposalLines();
    drawTicketLines();
  }

  async function runAnalyze() {
    if (state.analyzeBusy) return;
    const btn = $("btn-analyze");
    const symbol =
      ($("symbol-input") && $("symbol-input").value) || state.symbol || "BTC_USDT";
    const active = document.querySelector(".tf-btn.active");
    const tf = active ? active.getAttribute("data-tf") : state.tf;
    const htf = state.htf || "1H";

    state.analyzeBusy = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Analysiere…";
    }
    setApplyEnabled(false);
    const bodyEl = $("proposal-body");
    if (bodyEl) {
      bodyEl.className = "placeholder";
      bodyEl.textContent = (state.llmLabel || "KI") + " analysiert…";
    }
    state.proposal = null;
    state.proposalSymbol = null;
    drawProposalLines();

    try {
      const res = await apiFetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: String(symbol).toUpperCase().trim(),
          tf: tf,
          htf: htf,
        }),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }
      if (!res.ok) {
        const detail =
          (data && (data.detail || data.message)) ||
          res.statusText ||
          "Analyse fehlgeschlagen";
        const msg = typeof detail === "string" ? detail : JSON.stringify(detail);
        showProposalError(msg);
        return;
      }
      renderProposal(data);
      loadHistory();
    } catch (err) {
      console.error("runAnalyze", err);
      showProposalError("Netzwerkfehler: " + (err && err.message ? err.message : err));
    } finally {
      state.analyzeBusy = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Analysieren";
      }
    }
  }

  /** Make sure the symbol <select> contains `sym` (e.g. default from .env). */
  function ensureSymbolOption(sym) {
    sym = String(sym || "").toUpperCase().trim();
    if (!sym) return;
    if (state.allSymbols.indexOf(sym) === -1) state.allSymbols.push(sym);
  }

  async function loadSymbols() {
    try {
      const res = await fetch("/api/symbols");
      const data = await res.json();
      if (!Array.isArray(data.symbols) || !data.symbols.length) return;
      state.allSymbols = data.symbols.map(function (s) {
        return String(s).toUpperCase();
      });
      ensureSymbolOption($("symbol-input") && $("symbol-input").value);
      renderSymbolTabs();
    } catch (err) {
      console.error("loadSymbols", err);
    }
  }

  /* ── Custom Coin-Suche ───────────────────────────────────── */
  const picker = { open: false, items: [], hl: -1 };

  function filterSymbols(query) {
    const q = String(query || "").toUpperCase().trim();
    const pool = state.allSymbols || [];
    if (!q) return pool.slice(0, 60);
    const starts = [];
    const contains = [];
    pool.forEach(function (s) {
      if (s.indexOf(q) === -1) return;
      (s.indexOf(q) === 0 ? starts : contains).push(s);
    });
    return starts.concat(contains).slice(0, 60);
  }

  function renderSymbolDropdown() {
    const box = $("symbol-dropdown");
    if (!box) return;
    if (!picker.items.length) {
      box.innerHTML = '<div class="symbol-empty">Kein Treffer</div>';
      return;
    }
    const cur = String(state.symbol || "").toUpperCase();
    box.innerHTML = picker.items
      .map(function (s, i) {
        const cls =
          "symbol-option" + (i === picker.hl ? " hl" : "") + (s === cur ? " current" : "");
        const safe = escapeHtml(s);
        return '<div class="' + cls + '" data-symbol="' + safe + '" role="option">' + safe + "</div>";
      })
      .join("");
  }

  function openSymbolDropdown() {
    const box = $("symbol-dropdown");
    const input = $("symbol-input");
    if (!box || !input) return;
    picker.items = filterSymbols(input.value);
    picker.hl = picker.items.length ? 0 : -1;
    renderSymbolDropdown();
    box.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    picker.open = true;
  }

  function closeSymbolDropdown() {
    const box = $("symbol-dropdown");
    const input = $("symbol-input");
    if (box) box.classList.add("hidden");
    if (input) input.setAttribute("aria-expanded", "false");
    picker.open = false;
    picker.hl = -1;
  }

  function moveSymbolHighlight(delta) {
    if (!picker.items.length) return;
    picker.hl = (picker.hl + delta + picker.items.length) % picker.items.length;
    renderSymbolDropdown();
    const hlEl = document.querySelector("#symbol-dropdown .symbol-option.hl");
    if (hlEl) hlEl.scrollIntoView({ block: "nearest" });
  }

  function pickHighlightedSymbol() {
    if (picker.hl < 0 || !picker.items[picker.hl]) return;
    switchSymbol(picker.items[picker.hl]);
    closeSymbolDropdown();
  }

  /** Central coin switch used by search, dropdown, tabs, scanner. Keeps the tab
   *  bar in perfect sync with state.symbol:
   *   - target already a tab  → just activate it (no duplicate)
   *   - opts.newTab / pending  → append a new tab
   *   - otherwise              → rename the ACTIVE tab in place (fixes E3)
   *  Loads market/orders/account exactly once (no change-event round-trip). */
  function goToSymbol(sym, opts) {
    opts = opts || {};
    sym = String(sym || "").toUpperCase().trim();
    if (!sym) return;
    const fromOverview = state.activeView === "overview"; // coming from overview → open a tab
    const newTab = !!opts.newTab || state._pendingNewTab === true || fromOverview;
    state._pendingNewTab = false;
    ensureSymbolOption(sym);

    showChart();

    const existing = state.openTabs.indexOf(sym);
    if (existing === -1) {
      if (newTab) {
        state.openTabs.push(sym);
      } else {
        const activeIdx = state.openTabs.indexOf(
          String(state.symbol || "").toUpperCase()
        );
        if (activeIdx !== -1) state.openTabs[activeIdx] = sym;
        else state.openTabs.push(sym);
      }
      saveOpenTabs();
    }

    const input = $("symbol-input");
    if (input) {
      input.value = sym;
      input.dataset.touched = "1";
    }
    const activeTf = document.querySelector(".tf-btn.active");
    const tf = activeTf ? activeTf.getAttribute("data-tf") : state.tf || "15m";
    loadMarket(sym, tf, state.htf || "1H");
    loadOpenOrders();
    loadAccount();
    loadFills();
    renderSymbolTabs();
  }

  /** Thin wrapper kept for the many existing call sites (dropdown, tabs,
   *  other-coin rows, closeOpenTab). Routes everything through goToSymbol. */
  function switchSymbol(sym) {
    goToSymbol(sym);
  }

  function wireSymbolPicker() {
    const input = $("symbol-input");
    if (!input) return;
    input.addEventListener("focus", openSymbolDropdown);
    input.addEventListener("input", openSymbolDropdown);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        picker.open ? moveSymbolHighlight(1) : openSymbolDropdown();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        picker.open ? moveSymbolHighlight(-1) : openSymbolDropdown();
      } else if (e.key === "Enter") {
        if (picker.open) {
          e.preventDefault();
          pickHighlightedSymbol();
        }
      } else if (e.key === "Escape") {
        closeSymbolDropdown();
        input.blur();
      }
    });
    document.addEventListener("click", function (e) {
      const pickerEl = $("symbol-picker");
      if (picker.open && pickerEl && !pickerEl.contains(e.target)) closeSymbolDropdown();
    });
    const box = $("symbol-dropdown");
    if (box) {
      box.addEventListener("mousedown", function (e) {
        const opt = e.target.closest(".symbol-option");
        if (!opt) return;
        e.preventDefault();
        switchSymbol(opt.getAttribute("data-symbol"));
        closeSymbolDropdown();
      });
    }
  }

  /* ── Chart-Tab-Leiste ─────────────────────────────────────── */
  function loadOpenTabs() {
    try {
      const raw = localStorage.getItem("obsidian_open_tabs");
      const arr = raw ? JSON.parse(raw) : null;
      state.openTabs =
        Array.isArray(arr) && arr.length ? arr : [state.symbol || "BTC_USDT"];
    } catch (_) {
      state.openTabs = [state.symbol || "BTC_USDT"];
    }
  }

  function saveOpenTabs() {
    try {
      localStorage.setItem("obsidian_open_tabs", JSON.stringify(state.openTabs));
    } catch (_) {}
  }

  function addOpenTab(sym) {
    sym = String(sym || "").toUpperCase().trim();
    if (!sym) return;
    if (state.openTabs.indexOf(sym) === -1) state.openTabs.push(sym);
    saveOpenTabs();
    renderSymbolTabs();
  }

  function closeOpenTab(sym, evt) {
    if (evt) evt.stopPropagation();
    if (state.openTabs.length <= 1) return; // never orphan the last tab
    const idx = state.openTabs.indexOf(sym);
    if (idx === -1) return;
    const wasActive = sym === String(state.symbol || "").toUpperCase();
    state.openTabs.splice(idx, 1);
    saveOpenTabs();
    if (wasActive) {
      switchSymbol(state.openTabs[idx] || state.openTabs[idx - 1] || state.openTabs[0]);
    } else {
      renderSymbolTabs();
    }
  }

  function tabPositionSide(sym) {
    const positions = (state.account && state.account.positions) || [];
    const p = positions.find(function (p) {
      return symMatch(p.symbol, sym) && Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    return p ? String(p.side || "").toLowerCase() : "";
  }

  function renderSymbolTabs() {
    const bar = $("symbol-tabs");
    const addBtn = $("tab-add-btn");
    if (!bar || !addBtn) return;
    bar.querySelectorAll(".symbol-tab").forEach(function (el) {
      el.remove();
    });
    const inChart = state.activeView !== "overview";
    const active = String(state.symbol || "").toUpperCase();

    // Fixed, non-closable overview tab in position 1
    const ov = document.createElement("button");
    ov.type = "button";
    ov.className = "symbol-tab" + (!inChart ? " active" : "");
    ov.setAttribute("role", "tab");
    ov.setAttribute("data-view", "overview");
    ov.innerHTML = '<span class="tab-dot"></span><span>Übersicht</span>';
    ov.addEventListener("click", function () {
      showOverview();
    });
    bar.insertBefore(ov, addBtn);

    state.openTabs.forEach(function (sym) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "symbol-tab" + (inChart && sym === active ? " active" : "");
      tab.setAttribute("role", "tab");
      tab.setAttribute("data-symbol", sym);
      const side = tabPositionSide(sym);
      tab.innerHTML =
        '<span class="tab-dot' + (side ? " " + side : "") + '"></span><span>' +
        escapeHtml(sym) + "</span>" +
        '<span class="tab-close" data-close="' + escapeHtml(sym) + '" title="Schließen">×</span>';
      tab.addEventListener("click", function (e) {
        if (!e.target.closest(".tab-close")) switchSymbol(sym);
      });
      tab.querySelector(".tab-close").addEventListener("click", function (e) {
        closeOpenTab(sym, e);
      });
      bar.insertBefore(tab, addBtn);
    });
  }

  function wireSymbolTabs() {
    const addBtn = $("tab-add-btn");
    if (addBtn) {
      addBtn.addEventListener("click", function () {
        state._pendingNewTab = true;
        const input = $("symbol-input");
        if (input) {
          input.value = "";
          input.focus();
          openSymbolDropdown();
        }
      });
    }
  }

  /* ── Overview tab (E5): static mini-charts, ~30s snapshot refresh ────── */
  function loadWatchlist() {
    try {
      const raw = localStorage.getItem("obsidian_watchlist");
      const arr = raw ? JSON.parse(raw) : null;
      state.watchlist = Array.isArray(arr) ? arr : [];
    } catch (_) {
      state.watchlist = [];
    }
  }
  function saveWatchlist() {
    try {
      localStorage.setItem("obsidian_watchlist", JSON.stringify(state.watchlist));
    } catch (_) {}
  }
  function addWatch(sym) {
    sym = String(sym || "").toUpperCase().trim();
    if (!sym) return;
    ensureSymbolOption(sym);
    if (state.watchlist.indexOf(sym) === -1) state.watchlist.push(sym);
    saveWatchlist();
    state._miniLast = 0; // bypass the 10s throttle so the new symbol's candles load immediately
    refreshOverview();
  }
  function removeWatch(sym) {
    const i = state.watchlist.indexOf(sym);
    if (i !== -1) {
      state.watchlist.splice(i, 1);
      saveWatchlist();
    }
    renderOverviewGrid();
  }

  function showOverview() {
    state.activeView = "overview";
    const layout = document.querySelector(".layout");
    const ov = $("overview-view");
    if (layout) layout.classList.add("hidden");
    if (ov) ov.classList.remove("hidden");
    renderSymbolTabs();
    refreshOverview();
    renderNews(); // paint cached headlines immediately
    refreshNews(); // then refresh if stale (5-min guard)
  }
  function showChart() {
    state.activeView = "chart";
    const layout = document.querySelector(".layout");
    const ov = $("overview-view");
    if (layout) layout.classList.remove("hidden");
    if (ov) ov.classList.add("hidden");
    // The chart container was display:none while the overview tab was
    // active (clientWidth 0). Force a fresh size read now that it's visible
    // again — the ResizeObserver normally catches this, but do it
    // explicitly too since display:none → block isn't reliably observed by
    // all ResizeObserver implementations in the same frame it becomes true.
    scheduleChartResize();
  }

  /** Coins to show: every open position (auto) + the watchlist, de-duped. */
  function overviewSymbols() {
    const out = [];
    const push = function (s) {
      const u = String(s || "").toUpperCase().trim();
      if (u && out.indexOf(u) === -1) out.push(u);
    };
    ((state.account && state.account.positions) || []).forEach(function (p) {
      if (Math.abs(Number(p.hold_vol) || 0) > 0) push(p.symbol);
    });
    state.watchlist.forEach(push);
    return out;
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

  /** Render cached headlines. EVERY feed string goes through escapeHtml
   *  (feeds are untrusted), links are http(s)-whitelisted + noopener. */
  function renderNews() {
    const box = $("overview-news");
    if (!box) return;
    const all = state.newsItems || [];
    if (!all.length) {
      box.innerHTML = '<div class="news-empty">Keine aktuellen Schlagzeilen.</div>';
      return;
    }
    // Curated desk, not a log: a handful of items, the freshest featured.
    const items = all.slice(0, 14);
    const leadCount = Math.min(3, items.length);

    // Build one clickable card. EVERY feed string is escaped (feeds are
    // untrusted); links are http(s)-whitelisted + rel="noopener noreferrer".
    function card(it, kind) {
      const url = String((it && it.url) || "");
      const safe = /^https?:\/\//i.test(url) ? url : "";
      const cls = kind === "lead" ? "news-lead" : "news-row";
      const open = safe
        ? '<a class="' + cls + '" href="' + escapeHtml(safe) +
          '" target="_blank" rel="noopener noreferrer">'
        : '<div class="' + cls + '">';
      const close = safe ? "</a>" : "</div>";
      const titleCls = kind === "lead" ? "news-lead-title" : "news-row-title";
      const metaCls = kind === "lead" ? "news-lead-meta" : "news-row-meta";
      // Lead cards get a teaser line from the feed summary; compact rows stay
      // title-only. Summary is server-side tag-stripped, escaped again here.
      let teaser = "";
      if (kind === "lead") {
        const s = String((it && it.summary) || "").trim();
        if (s) {
          const clipped = s.length > 160 ? s.slice(0, 157).replace(/\s+\S*$/, "") + "…" : s;
          teaser = '<span class="news-lead-teaser">' + escapeHtml(clipped) + "</span>";
        }
      }
      return (
        open +
        '<span class="' + titleCls + '">' + escapeHtml((it && it.title) || "") + "</span>" +
        teaser +
        '<span class="' + metaCls + '">' +
        '<span class="news-src">' + escapeHtml((it && it.source) || "") + "</span>" +
        '<span class="news-dot" aria-hidden="true">·</span>' +
        '<span class="news-time">' + escapeHtml(relTime(it && it.published)) + "</span>" +
        "</span>" +
        close
      );
    }

    const leads = items.slice(0, leadCount)
      .map(function (it) { return card(it, "lead"); })
      .join("");
    const rest = items.slice(leadCount)
      .map(function (it) { return card(it, "row"); })
      .join("");

    let html = '<div class="news-leads">' + leads + "</div>";
    if (rest) html += '<div class="news-rest">' + rest + "</div>";
    box.innerHTML = html;
  }

  /** 5-min-throttled news refresh. Mirrors the _miniLast guard so the 30s
   *  overview timer never hammers the feeds. */
  async function refreshNews() {
    if (state.activeView !== "overview") return;
    if (state._newsBusy) return;
    const now = Date.now();
    if (now - (state._newsLast || 0) < 300000 && (state.newsItems || []).length) {
      renderNews();
      return;
    }
    state._newsBusy = true;
    try {
      const res = await fetch("/api/news");
      if (res.ok) {
        const data = await res.json();
        state.newsItems = data.items || [];
        state._newsLast = Date.now();
      }
    } catch (e) {
      console.error("refreshNews", e);
    } finally {
      state._newsBusy = false;
    }
    if (state.activeView === "overview") renderNews();
  }

  /** Hardened against double-fetches: a busy guard plus a 10s min-interval so
   *  the account poll (30s) and the overview timer (30s) landing close
   *  together can't fire two /api/mini requests back to back. Reuses the
   *  cached candles (fresh position/PnL badges still redraw from state.account). */
  async function refreshOverview() {
    if (state.activeView !== "overview") return; // never redraw in background
    const syms = overviewSymbols();
    if (!syms.length) {
      renderOverviewGrid();
      return;
    }
    if (state._miniBusy) return;
    const now = Date.now();
    if (now - (state._miniLast || 0) < 10000 && Object.keys(state.overviewData).length) {
      renderOverviewGrid(); // fresh enough — reuse cached candles, update PnL badges
      return;
    }
    state._miniBusy = true;
    try {
      const res = await fetch(
        "/api/mini?symbols=" + encodeURIComponent(syms.join(",")) + "&tf=15m&limit=96"
      );
      if (res.ok) {
        const data = await res.json();
        (data.results || []).forEach(function (r) {
          state.overviewData[String(r.symbol || "").toUpperCase()] = r;
        });
        state._miniLast = Date.now();
      }
    } catch (e) {
      console.error("refreshOverview", e);
    } finally {
      state._miniBusy = false;
    }
    if (state.activeView === "overview") renderOverviewGrid();
  }

  function positionFor(sym) {
    return (
      ((state.account && state.account.positions) || []).find(function (p) {
        return symMatch(p.symbol, sym) && Math.abs(Number(p.hold_vol) || 0) > 0;
      }) || null
    );
  }

  function renderOverviewGrid() {
    const grid = $("overview-grid");
    if (!grid) return;
    const syms = overviewSymbols();
    if (!syms.length) {
      grid.innerHTML =
        '<div class="overview-empty">Keine offenen Positionen. Coins über „+ Beobachten" hinzufügen.</div>';
      return;
    }
    grid.innerHTML = "";
    const cs = contractSize();
    syms.forEach(function (sym) {
      const key = sym.toUpperCase();
      const d = state.overviewData[key] || {};
      const pos = positionFor(sym);
      const isWatch = state.watchlist.indexOf(key) !== -1 && !pos;

      const tile = document.createElement("div");
      tile.className = "mini-tile" + (pos ? " pos " + String(pos.side || "").toLowerCase() : "");
      tile.setAttribute("data-symbol", key);

      const last = Number(d.last_price);
      const chg = Number(d.change_pct);
      const chgCls = Number.isFinite(chg) ? (chg >= 0 ? "pos-pos" : "pos-neg") : "";
      const chgTxt = Number.isFinite(chg) ? (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%" : "—";

      let pnlHtml = "";
      if (pos) {
        // `cs` is the ACTIVE chart symbol's contractSize. Reusing it to
        // recompute PnL for every tile would be wrong on MEXC, where coins
        // can have different contract sizes (F-10). Only the active
        // symbol's own tile may use that local recompute (correct cs, and
        // it doubles as a live refresh against the streaming price); every
        // other tile uses the exchange's own unrealized_pnl straight from
        // /api/account instead of guessing with another symbol's cs.
        const isActiveSym = symMatch(sym, state.symbol);
        let pnl = null;
        if (isActiveSym && Number.isFinite(last)) {
          const entry = Number(pos.entry_price);
          const vol = Number(pos.hold_vol);
          const short = String(pos.side || "").toLowerCase() === "short";
          if (Number.isFinite(entry) && Number.isFinite(vol)) {
            pnl = (last - entry) * vol * cs * (short ? -1 : 1);
          }
        } else {
          pnl = Number(pos.unrealized_pnl);
        }
        if (Number.isFinite(pnl)) {
          const pc = pnl >= 0 ? "pos-pos" : "pos-neg";
          pnlHtml =
            '<span class="mini-pnl ' + pc + '">' + (pnl >= 0 ? "+" : "") + fmt(pnl, 2) + "</span>";
        }
      }

      tile.innerHTML =
        (isWatch ? '<button type="button" class="mini-remove" title="Entfernen">×</button>' : "") +
        '<div class="mini-head"><span class="mini-sym">' + escapeHtml(key) + "</span>" +
        '<span class="mini-price">' + (Number.isFinite(last) ? fmt(last, 6) : "—") + "</span></div>" +
        '<div class="mini-badges">' + pnlHtml +
        '<span class="mini-chg ' + chgCls + '">' + chgTxt + "</span></div>" +
        '<canvas class="mini-canvas"></canvas>';

      const cv = tile.querySelector(".mini-canvas");
      const marks = [];
      if (pos) {
        const mk = state.tradeMarkers && state.tradeMarkers[key];
        marks.push({ price: Number(pos.entry_price), color: "#9d9ab6" });
        if (mk && mk.sl) marks.push({ price: Number(mk.sl), color: "#e35349" });
        if (mk && mk.tp) marks.push({ price: Number(mk.tp), color: "#4fbe8e" });
      }
      // Draw after insertion so the canvas has a measured width.
      requestAnimationFrame(function () {
        drawMiniCandles(cv, d.candles || [], marks);
      });

      tile.addEventListener("click", function (e) {
        if (e.target.closest(".mini-remove")) {
          removeWatch(key);
          return;
        }
        goToSymbol(key, { newTab: true });
      });
      grid.appendChild(tile);
    });
  }

  function drawMiniCandles(cv, candles, marks) {
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 240;
    const h = cv.clientHeight || 84;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const cs = (candles || []).slice(-60);
    if (!cs.length) return;
    let lo = Infinity,
      hi = -Infinity;
    cs.forEach(function (c) {
      lo = Math.min(lo, c.low);
      hi = Math.max(hi, c.high);
    });
    (marks || []).forEach(function (m) {
      if (Number.isFinite(m.price)) {
        lo = Math.min(lo, m.price);
        hi = Math.max(hi, m.price);
      }
    });
    if (!(hi > lo)) return;
    const pad = 4,
      plotH = h - 2 * pad;
    const y = function (p) {
      return pad + ((hi - p) / (hi - lo)) * plotH;
    };
    const n = cs.length,
      bw = Math.max(1, (w - 2) / n);
    cs.forEach(function (c, i) {
      const x = 1 + i * bw + bw / 2;
      const up = c.close >= c.open;
      ctx.strokeStyle = up ? "#4fbe8e" : "#e35349";
      ctx.fillStyle = up ? "#4fbe8e" : "#e35349";
      ctx.beginPath();
      ctx.moveTo(x, y(c.high));
      ctx.lineTo(x, y(c.low));
      ctx.stroke();
      const bodyTop = y(Math.max(c.open, c.close));
      const bodyH = Math.max(1, Math.abs(y(c.open) - y(c.close)));
      ctx.fillRect(x - bw * 0.32, bodyTop, Math.max(1, bw * 0.64), bodyH);
    });
    (marks || []).forEach(function (m) {
      if (!Number.isFinite(m.price)) return;
      ctx.strokeStyle = m.color;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 2]);
      const yy = y(m.price);
      ctx.beginPath();
      ctx.moveTo(0, yy);
      ctx.lineTo(w, yy);
      ctx.stroke();
      ctx.setLineDash([]);
    });
  }

  async function loadLlm() {
    try {
      const res = await apiFetch("/api/llm");
      if (!res.ok) return;
      const data = await res.json();
      applyLlmStatus(data);
    } catch (err) {
      console.error("loadLlm", err);
    }
  }

  function llmLabelFor(data, providerId) {
    const p = (data && data.providers || []).find(function (x) {
      return x.id === providerId;
    });
    return (p && p.label) || providerId || "KI";
  }

  function applyLlmStatus(data) {
    if (!data) return;
    const sel = $("llm-select");
    if (sel && data.provider) sel.value = data.provider;
    const label = $("llm-label");
    if (label && data.provider) {
      label.textContent = llmLabelFor(data, data.provider);
    }
    // Remember the active provider's label so the analyze spinner names the
    // real KI (not a hardcoded "Claude") — UB-1.
    if (data.provider) state.llmLabel = llmLabelFor(data, data.provider);
    const providers = data.providers || [];
    if (sel) {
      providers.forEach(function (p) {
        const opt = Array.prototype.find.call(sel.options, function (o) {
          return o.value === p.id;
        });
        if (opt) {
          opt.disabled = !p.configured;
          opt.textContent = p.label + (p.configured ? "" : " — kein Key");
        }
      });
    }
    const me = providers.find(function (p) {
      return p.id === data.provider;
    });
    if (me) setDot($("dot-xai"), !!me.configured);
  }

  async function switchLlm(provider) {
    try {
      const res = await apiFetch("/api/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: provider }),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        showToast(detailToText(data.detail || data), "err");
        loadLlm(); // revert dropdown to server truth
        return;
      }
      applyLlmStatus(data);
      showToast("KI gewechselt: " + llmLabelFor(data, data.provider), "ok");
    } catch (err) {
      showToast("KI-Wechsel fehlgeschlagen: " + (err && err.message), "err");
      loadLlm();
    }
  }

  /** Close a share of the position. fraction 1 = full, 0.25 = 25 %.
   *  The server closes that share of the CURRENT hold with lot rounding. */
  async function closePositionFrac(symbol, side, fraction) {
    if (state.closeBusy || state.orderBusy) return;
    if (!symbol || !side) return;
    const pct = Math.round((fraction || 1) * 100);
    const text =
      pct + "% der " + side.toUpperCase() + "-Position " + symbol +
      " jetzt per MARKET schließen?";
    if (!window.confirm(text)) return;
    state.closeBusy = true;
    try {
      const payload = { symbol: symbol, side: side };
      if (fraction >= 1) payload.fraction = 1;
      else payload.fraction = fraction;
      const res = await apiFetch("/api/orders/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        showToast(detailToText(data.detail || data), "err");
        return;
      }
      showToast(
        "Geschlossen: " + fmt(data.closed_vol, 4) + " von " + fmt(data.hold_vol, 4),
        "ok"
      );
      loadAccount();
      loadOpenOrders();
      loadHistory();
    } catch (err) {
      showToast("Schließen fehlgeschlagen: " + (err && err.message), "err");
    } finally {
      state.closeBusy = false;
    }
  }

  /** Move/replace the stop-loss of an OPEN position to its fee-adjusted
   *  break-even. A REAL money action: always confirmed, guarded against
   *  double-submit (state.slBusy), and any backend detail is surfaced verbatim.
   *  The server places the new stop, OID-verifies it, THEN cancels the old one,
   *  so the position is never left unprotected during the move. */
  async function moveStopToBreakEven(symbol, side, be) {
    if (state.slBusy || state.closeBusy || state.orderBusy) return;
    if (!symbol || !side || !Number.isFinite(be) || be <= 0) return;
    const text =
      "Stop-Loss der " + String(side).toUpperCase() + "-Position " + symbol +
      " auf Break-Even " + fmt(be, 6) + " setzen?\n\n" +
      "Ein neuer Stop wird platziert und verifiziert, danach ein bestehender " +
      "alter Stop gecancelt. Dies ist eine echte Order-Aktion.";
    if (!window.confirm(text)) return;
    state.slBusy = true;
    try {
      const res = await apiFetch("/api/orders/modify-sl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: symbol, side: side, new_sl: be }),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        showToast(detailToText(data.detail || data), "err");
        return;
      }
      const warn =
        Array.isArray(data.warnings) && data.warnings.length
          ? " — ⚠ " + data.warnings.join("; ")
          : "";
      showToast(
        "SL → Break-Even gesetzt: " +
          fmt(data.new_sl != null ? data.new_sl : be, 6) +
          warn,
        warn ? "err" : "ok"
      );
      loadAccount();
      loadOpenOrders();
    } catch (err) {
      showToast(
        "SL verschieben fehlgeschlagen: " + (err && err.message),
        "err"
      );
    } finally {
      state.slBusy = false;
    }
  }

  /* ── Markt-Scanner: Sonnet screent den Pool, Opus analysiert per Klick ── */

  async function openCoinAndAnalyze(sym) {
    if (!sym) return;
    highlightScanChip(sym);
    goToSymbol(sym, { newTab: true });
    runAnalyze();
  }

  function highlightScanChip(sym) {
    const strip = $("scan-strip");
    if (!strip) return;
    strip.querySelectorAll(".scan-chip").forEach(function (c) {
      c.classList.toggle("active", c.getAttribute("data-symbol") === sym);
    });
  }

  /** True if the coin already has an open position (hold_vol != 0) or an open
   *  order/stop-order — scan suggestions for it are hidden, no point proposing
   *  a trade that's already running. */
  function hasOpenExposure(sym) {
    if (!sym) return false;
    const positions = (state.account && state.account.positions) || [];
    const inPosition = positions.some(function (p) {
      return symMatch(p.symbol, sym) && Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    if (inPosition) return true;
    const oo = state.openOrders;
    const orders = (oo && oo.orders) || [];
    const stops = (oo && oo.stop_orders) || [];
    return (
      orders.some(function (o) { return o.symbol && symMatch(o.symbol, sym); }) ||
      stops.some(function (s) { return s.symbol && symMatch(s.symbol, sym); })
    );
  }

  /** Scan results live in a PERSISTENT strip above the analysis. Clicking a
   *  coin analyses it without destroying the other findings. */
  function renderScanResults(data) {
    const strip = $("scan-strip");
    const allRows = (data && data.results) || [];
    const rows = allRows.filter(function (r) {
      return !hasOpenExposure(r.symbol);
    });
    const hiddenCount = allRows.length - rows.length;
    state.scanResults = data;

    if (!strip) return;
    if (!rows.length) {
      strip.className = "scan-strip";
      const emptyMsg = allRows.length
        ? "Alle Top-Kandidaten bereits offen (Position oder Order) — nichts Neues vorzuschlagen."
        : "Kein Setup mit klarem Edge — auch das ist ein Ergebnis.";
      strip.innerHTML =
        '<div class="scan-strip-head">Markt-Scan · ' +
        escapeHtml(String(data.model_used || "?")) + " · " +
        ((data.scanned || []).length || 0) + " Coins</div>" +
        '<div class="scan-empty">' + emptyMsg + '</div>';
      const body = $("proposal-body");
      if (body) {
        body.className = "placeholder";
        body.textContent = allRows.length
          ? "Alle gefundenen Setups laufen bereits (offene Position/Order). Einzelnen Coin wählen und Analysieren nutzen, oder später erneut scannen."
          : "Kein Coin mit klarem Setup gefunden. Einzelnen Coin wählen und Analysieren nutzen, oder später erneut scannen.";
      }
      return;
    }

    let html =
      '<div class="scan-strip-head">Markt-Scan · ' +
      escapeHtml(String(data.model_used || "?")) + " · Top " + rows.length +
      " von " + ((data.scanned || []).length || 0) + " Coins" +
      (hiddenCount
        ? " (" + hiddenCount + " bereits offen ausgeblendet)"
        : "") +
      ' <span class="scan-hint">— Coin anklicken für Detail-Analyse</span></div>' +
      '<div class="scan-chips">';
    rows.forEach(function (r) {
      const long = String(r.bias || "").toLowerCase() === "long";
      html +=
        '<button type="button" class="scan-chip ' + (long ? "chip-long" : "chip-short") +
        '" data-symbol="' + escapeHtml(String(r.symbol || "")) + '" ' +
        'title="' + escapeHtml((r.setup || "") + ": " + (r.reason || "")) + '">' +
        '<span class="chip-score">' + fmt(r.score, 1) + "</span>" +
        '<span class="chip-sym">' + escapeHtml(r.symbol || "—") + "</span>" +
        '<span class="chip-bias">' + (long ? "▲" : "▼") + "</span>" +
        '<span class="chip-setup">' + escapeHtml(r.setup || "") + "</span>" +
        "</button>";
    });
    html += "</div>";

    strip.className = "scan-strip";
    strip.innerHTML = html;
    strip.querySelectorAll(".scan-chip").forEach(function (b) {
      b.addEventListener("click", function () {
        openCoinAndAnalyze(b.getAttribute("data-symbol"));
      });
    });

    // Nudge the user toward the first result without auto-spending tokens
    const body = $("proposal-body");
    if (body && (!state.proposal || state.proposalSymbol !== state.symbol)) {
      body.className = "placeholder";
      body.textContent =
        "Scan fertig — " + rows.length +
        " Setups gefunden. Klick auf einen Coin oben startet die Detail-Analyse.";
    }
  }

  async function runScan() {
    if (state.scanBusy) return;
    const btn = $("btn-scan");
    state.scanBusy = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Scanne Markt…";
    }
    const body = $("proposal-body");
    if (body) {
      body.className = "placeholder";
      body.textContent =
        "Scanner lädt die Top-Coins und sucht nach Setups (dauert ~20-40 s)…";
    }
    // Explicit timeout so a slow/hung scan gives a clear message instead of
    // the browser's opaque "Failed to fetch".
    const ctrl =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctrl
      ? setTimeout(function () {
          ctrl.abort();
        }, 180000)
      : null;
    try {
      const res = await apiFetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tf: state.tf || "15m", htf: state.htf || "1H" }),
        signal: ctrl ? ctrl.signal : undefined,
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        showProposalError(detailToText(data && data.detail));
        return;
      }
      renderScanResults(data);
    } catch (err) {
      if (err && err.name === "AbortError") {
        showProposalError(
          "Scan-Zeitüberschreitung (>3 min) — Börse oder KI zu langsam. " +
            "Erneut versuchen oder ein schnelleres KI-Modell wählen."
        );
      } else {
        showProposalError(
          "Netzwerkfehler beim Scan: " +
            (err && err.message ? err.message : err) +
            " — läuft der Server noch?"
        );
      }
    } finally {
      if (timer) clearTimeout(timer);
      state.scanBusy = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "◎ Markt scannen";
      }
    }
  }

  /** Manual trigger mode means no exchange-side SL/TP protection — make that
   *  danger visible right in the ticket, not just after Preview. */
  function updateTriggerModeUi() {
    const manual = state.triggerMode === "manual";
    const warn = $("trigger-mode-warn");
    if (warn) warn.classList.toggle("hidden", !manual);
    const btn = $("btn-send-order");
    if (btn && !state.orderBusy) {
      btn.textContent = manual
        ? "Order prüfen — MANUELL (kein Börsen-Stop)"
        : "Order prüfen (Preview)";
    }
  }

  /** Sync segmented Long/Short + Market/Limit buttons from the hidden selects.
   *  Also tints the ticket panel and dims the limit-price field for market. */
  function syncTicketSegments() {
    const side = ($("ticket-side") && $("ticket-side").value) || "long";
    const type = ($("ticket-type") && $("ticket-type").value) || "market";

    document.querySelectorAll(".side-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-side") === side);
    });
    document.querySelectorAll(".type-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-type") === type);
    });

    const panel = $("ticket-panel");
    if (panel) panel.dataset.side = side;

    const form = $("order-form");
    if (form) form.classList.toggle("is-market", type === "market");

    // In % mode the side flips SL/TP price direction — refresh lines + readout
    drawTicketLines();
    updateRiskReadout();
  }

  function wireTicketSegments() {
    document.querySelectorAll(".side-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const sel = $("ticket-side");
        if (sel) sel.value = btn.getAttribute("data-side") || "long";
        syncTicketSegments();
      });
    });
    document.querySelectorAll(".type-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const sel = $("ticket-type");
        if (sel) sel.value = btn.getAttribute("data-type") || "market";
        syncTicketSegments();
      });
    });
    document.querySelectorAll(".sltp-mode-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setSltpMode(btn.getAttribute("data-mode") || "price");
      });
    });
    document.querySelectorAll(".trigger-mode-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.triggerMode = btn.getAttribute("data-trigger") === "manual" ? "manual" : "auto";
        document.querySelectorAll(".trigger-mode-btn").forEach(function (b) {
          b.classList.toggle(
            "active",
            b.getAttribute("data-trigger") === state.triggerMode
          );
        });
        updateTriggerModeUi();
      });
    });
    try {
      const savedSize = localStorage.getItem("obsidian_size_mode");
      if (savedSize === "margin" || savedSize === "position") state.sizeMode = savedSize;
    } catch (_) {}
    document.querySelectorAll(".size-mode-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setSizeMode(btn.getAttribute("data-size-mode") || "position");
      });
    });
    setSizeMode(state.sizeMode); // reflect restored mode in buttons + label
    syncTicketSegments();
    updateTriggerModeUi();
  }

  function wireUi() {
    wireTicketSegments();
    wireSymbolPicker();
    wireSymbolTabs();
    document.querySelectorAll(".tf-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tf-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const tf = btn.getAttribute("data-tf");
        // HTF always higher than LTF when possible
        let htf = "1H";
        if (tf === "5m" || tf === "15m") htf = "1H";
        else if (tf === "1H") htf = "4H";
        else if (tf === "4H") htf = "1D";
        else if (tf === "1D") htf = "1D";
        loadMarket($("symbol-input").value, tf, htf);
      });
    });

    const loadBtn = $("btn-load");
    if (loadBtn) {
      loadBtn.addEventListener("click", () => {
        const active = document.querySelector(".tf-btn.active");
        const tf = active ? active.getAttribute("data-tf") : state.tf;
        loadMarket($("symbol-input").value, tf, state.htf);
      });
    }

    const sym = $("symbol-input");
    if (sym) {
      sym.addEventListener("change", () => {
        sym.dataset.touched = "1";
        goToSymbol(sym.value);
      });
      sym.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          sym.dataset.touched = "1";
          goToSymbol(sym.value);
        }
      });
    }

    ["ticket-entry", "ticket-price", "ticket-sl", "ticket-tp1"].forEach((id) => {
      const el = $(id);
      if (el) {
        el.addEventListener("input", function () {
          drawTicketLines();
          updateRiskReadout();
        });
        el.addEventListener("change", drawTicketLines);
      }
    });
    // Size/leverage feed the derived vol + margin + risk readouts
    ["ticket-usdt", "ticket-leverage"].forEach(function (id) {
      const el = $(id);
      if (el) el.addEventListener("input", updateRiskReadout);
    });

    const form = $("order-form");
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        runPreview();
      });
    }

    const analyzeBtn = $("btn-analyze");
    if (analyzeBtn) {
      analyzeBtn.addEventListener("click", () => {
        runAnalyze();
      });
    }

    const applyBtn = $("btn-apply-proposal");
    if (applyBtn) {
      applyBtn.addEventListener("click", () => {
        applyProposalToTicket();
      });
    }

    document.querySelectorAll("[data-close-modal]").forEach((el) => {
      el.addEventListener("click", () => closeConfirmModal());
    });

    const confirmBtn = $("btn-confirm-live");
    if (confirmBtn) {
      confirmBtn.addEventListener("click", () => runConfirm());
    }

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeConfirmModal();
    });

    const histBtn = $("btn-history-refresh");
    if (histBtn) {
      histBtn.addEventListener("click", () => {
        loadHistory();
      });
    }

    const histClearBtn = $("btn-history-clear");
    if (histClearBtn) {
      histClearBtn.addEventListener("click", () => clearHistory());
    }

    const sugBtn = $("btn-suggest-vol");
    if (sugBtn) {
      sugBtn.addEventListener("click", () => suggestVol());
    }

    const ordBtn = $("btn-orders-refresh");
    if (ordBtn) {
      ordBtn.addEventListener("click", () => loadOpenOrders());
    }

    // Tabbed data panel: Positionen · Offene Orders · Historie · Trades
    document.querySelectorAll(".data-tab").forEach(function (t) {
      t.addEventListener("click", function () {
        switchDataTab(t.getAttribute("data-tab"));
      });
    });
    // Sync the default tab's action-button visibility (default = Positionen).
    document.querySelectorAll(".data-act").forEach(function (b) {
      b.classList.toggle("hidden", b.getAttribute("data-for") !== "positions");
    });

    const llmSel = $("llm-select");
    if (llmSel) {
      llmSel.addEventListener("change", () => switchLlm(llmSel.value));
    }

    const scanBtn = $("btn-scan");
    if (scanBtn) {
      scanBtn.addEventListener("click", () => runScan());
    }

    const aiLinesBtn = $("btn-toggle-ai-lines");
    if (aiLinesBtn) {
      aiLinesBtn.addEventListener("click", () => {
        state.showAiLines = !state.showAiLines;
        aiLinesBtn.classList.toggle("active", state.showAiLines);
        drawProposalLines();
      });
    }

    const zonesBtn = $("btn-toggle-zones");
    if (zonesBtn) {
      zonesBtn.addEventListener("click", () => {
        state.showZones = !state.showZones;
        zonesBtn.classList.toggle("active", state.showZones);
        drawTradeZones();
      });
    }
  }

  function numOrNull(el) {
    if (!el || el.value === "" || el.value == null) return null;
    const n = Number(el.value);
    return Number.isFinite(n) ? n : null;
  }

  function readTicket() {
    const symbol =
      ($("symbol-input") && $("symbol-input").value) || state.symbol || "BTC_USDT";
    const side = ($("ticket-side") && $("ticket-side").value) || "long";
    const orderType = ($("ticket-type") && $("ticket-type").value) || "market";
    // Derive contract vol from the USDT notional right before reading it
    const vol = usdtToVol();
    const leverage = numOrNull($("ticket-leverage")) || 5;
    const price = numOrNull($("ticket-price"));
    const entry = numOrNull($("ticket-entry"));
    // SL/TP go to the backend as absolute prices, resolved from % if needed
    const stopLoss = resolveStop();
    const takeProfit = resolveTp();

    const ticket = {
      symbol: String(symbol).toUpperCase().trim(),
      side: side,
      order_type: orderType,
      vol: vol,
      leverage: leverage,
      open_type: 1,
      trigger_mode: state.triggerMode === "manual" ? "manual" : "auto",
    };
    if (price != null) ticket.price = price;
    if (entry != null) ticket.entry = entry;
    if (stopLoss != null) ticket.stop_loss = stopLoss;
    if (takeProfit != null) ticket.take_profit = takeProfit;
    return ticket;
  }

  function setTicketError(msg) {
    const el = $("ticket-error");
    if (!el) return;
    if (!msg) {
      el.classList.add("hidden");
      el.textContent = "";
      return;
    }
    el.classList.remove("hidden");
    el.textContent = msg;
  }

  function showToast(msg, kind) {
    const el = $("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("hidden", "ok", "err");
    if (kind) el.classList.add(kind);
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      el.classList.add("hidden");
    }, 5000);
  }

  function detailToText(detail) {
    if (detail == null) return "Unbekannter Fehler";
    if (typeof detail === "string") return detail;
    if (detail.message) return detail.message;
    if (Array.isArray(detail.errors)) return detail.errors.join("; ");
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && d.msg) || JSON.stringify(d))
        .join("; ");
    }
    return JSON.stringify(detail);
  }

  function openConfirmModal(preview) {
    const modal = $("confirm-modal");
    const body = $("confirm-body");
    const errEl = $("confirm-error");
    const confirmBtn = $("btn-confirm-live");
    if (!modal || !body) return;

    state.previewToken = preview.token || null;
    state.previewSummary = preview.summary || null;

    const s = preview.summary || {};
    const gate = preview.gate || {};
    const okGates = preview.ok === true && !!preview.token;
    const armed = state.health && state.health.trading_enabled === true;
    const canConfirm = okGates && armed;
    const errors = preview.errors || gate.errors || [];

    let html = "";

    // 1) Blocker box FIRST — the single clear answer to "why can't I confirm?"
    if (!okGates && errors.length) {
      html +=
        '<div class="blocker-box">' +
        '<div class="blocker-title">⛔ Order blockiert — diese Punkte zuerst lösen:</div>' +
        '<ul class="blocker-list">' +
        errors.map((e) => "<li>" + escapeHtml(_humanGate(e)) + "</li>").join("") +
        "</ul></div>";
    } else if (okGates && !armed) {
      html +=
        '<div class="blocker-box blocker-disarmed">' +
        '<div class="blocker-title">🔒 DISARMED — Live-Trading ist aus</div>' +
        '<p>Alle Risk-Gates sind grün. Zum echten Senden <code>TRADING_ENABLED=true</code> ' +
        "in der <code>.env</code> setzen und die App neu starten.</p></div>";
    }

    // 2) Order summary as a compact, readable card
    const dirCls = (s.side || "").toLowerCase() === "short" ? "sum-short" : "sum-long";
    html +=
      '<div class="order-summary ' + dirCls + '">' +
      '<div class="sum-head">' +
      '<span class="sum-side">' + escapeHtml((s.side || "").toUpperCase()) + "</span>" +
      '<span class="sum-sym">' + escapeHtml(s.symbol || "—") + "</span>" +
      '<span class="sum-type">' + escapeHtml(s.order_type || "") +
      (s.price != null ? " @ " + fmt(s.price, 6) : " @ Market") + "</span>" +
      "</div>" +
      '<div class="sum-grid">' +
      _sumCell("Größe", fmt(s.notional_usdt, 2) + " " + ccy(), "≈ " + fmt(s.vol, 6) + " Kontrakte") +
      _sumCell("Hebel", (s.leverage != null ? s.leverage + "×" : "—"),
               s.notional_usdt != null && s.leverage ? "Margin " + fmt(s.notional_usdt / s.leverage, 2) : "") +
      _sumCell("Entry", fmt(s.entry_for_risk, 6), "Risk-Referenz") +
      _sumCell("Stop-Loss", s.stop_loss != null ? fmt(s.stop_loss, 6) : "—",
               _pctVs(s.stop_loss, s.entry_for_risk), "sum-sl") +
      _sumCell("Take-Profit", s.take_profit != null ? fmt(s.take_profit, 6) : "—",
               _pctVs(s.take_profit, s.entry_for_risk), "sum-tp") +
      _sumCell("Risiko", fmt(s.risk_usdt, 2) + " " + ccy(),
               s.risk_pct != null ? fmt(s.risk_pct, 2) + "% Equity" : "", "sum-sl") +
      _sumCell("Chance/Risiko", s.rrr != null ? "1 : " + fmt(s.rrr, 2) : "—",
               s.rrr != null && s.rrr >= 2 ? "gut" : "") +
      "</div></div>";

    // 3) Warnings (non-blocking)
    const warnings = preview.warnings || gate.warnings || [];
    if (warnings.length && okGates) {
      html +=
        '<div class="warn-box"><div class="warn-title">Hinweise:</div><ul class="warn-list">' +
        warnings.map((w) => "<li>" + escapeHtml(_humanGate(w)) + "</li>").join("") +
        "</ul></div>";
    }

    // Read the mode the PREVIEW TOKEN was made with (not the live toggle) so
    // the warning always matches what confirming this token actually does.
    const manual = (s.trigger_mode || state.triggerMode) === "manual";
    if (canConfirm && manual) {
      html +=
        '<div class="blocker-box manual-warn">' +
        '<div class="blocker-title">⚠ Manueller SL/TP — kein Börsen-Schutz</div>' +
        "<p>Diese Order wird <strong>ohne</strong> Stop-Loss/Take-Profit auf der " +
        "Börse platziert. Du musst die Position selbst schließen. Bei geschlossenem " +
        "Browser oder Verbindungsabbruch ist sie <strong>ungeschützt</strong>.</p>" +
        '<label class="manual-ack"><input type="checkbox" id="manual-ack-box" /> ' +
        "Ich verstehe das und manage den Exit selbst.</label></div>";
    }
    // Weak reward:risk is real send-friction too — require an explicit ack
    // checkbox before the confirm button unlocks, same as the manual warning.
    const weakRrr = canConfirm && s.rrr != null && s.rrr < 1.5;
    if (weakRrr) {
      html +=
        '<div class="blocker-box rrr-confirm">' +
        '<div class="blocker-title">⚠ Schwaches Chance/Risiko — 1 : ' + fmt(s.rrr, 2) + "</div>" +
        '<label class="manual-ack"><input type="checkbox" id="rrr-ack-box" /> ' +
        "Ich bestätige das schwache Chance/Risiko 1:" + fmt(s.rrr, 2) + "</label></div>";
    }

    if (canConfirm) {
      const ttl = preview.expires_in_seconds || 60;
      html +=
        '<p class="live-warn">⚠ LIVE-ORDER — nach Confirm sofort echt und irreversibel. ' +
        'Token läuft in <strong id="confirm-ttl">' + ttl + "</strong>s ab.</p>";
    }

    body.innerHTML = html;
    if (errEl) {
      errEl.classList.add("hidden");
      errEl.textContent = "";
    }
    if (confirmBtn) {
      // Manual mode and a weak RRR each additionally require their own
      // acknowledgement checkbox before the button unlocks.
      const needManualAck = canConfirm && manual;
      const needRrrAck = weakRrr;
      const ackState = { manual: !needManualAck, rrr: !needRrrAck };
      function recomputeConfirmDisabled() {
        confirmBtn.disabled = !canConfirm || !ackState.manual || !ackState.rrr;
      }
      confirmBtn.disabled = !canConfirm || needManualAck || needRrrAck;
      confirmBtn.title = canConfirm
        ? (needManualAck || needRrrAck ? "Bitte alle Bestätigungen ankreuzen" : "Live-Order jetzt senden")
        : !okGates
          ? "Risk-Gates blockieren die Order (siehe oben)"
          : "DISARMED — TRADING_ENABLED=false";
      if (needManualAck) {
        const ack = $("manual-ack-box");
        if (ack) {
          ack.addEventListener("change", function () {
            ackState.manual = ack.checked;
            recomputeConfirmDisabled();
          });
        }
      }
      if (needRrrAck) {
        const rrrAck = $("rrr-ack-box");
        if (rrrAck) {
          rrrAck.addEventListener("change", function () {
            ackState.rrr = rrrAck.checked;
            recomputeConfirmDisabled();
          });
        }
      }
    }
    modal.classList.remove("hidden");

    if (state._ttlTimer) clearInterval(state._ttlTimer);
    if (canConfirm) {
      let left = preview.expires_in_seconds || 60;
      state._ttlTimer = setInterval(function () {
        left -= 1;
        const tEl = $("confirm-ttl");
        if (tEl) tEl.textContent = String(Math.max(0, left));
        if (left <= 0) {
          clearInterval(state._ttlTimer);
          if (confirmBtn) confirmBtn.disabled = true;
          if (errEl) {
            errEl.classList.remove("hidden");
            errEl.textContent = "Token abgelaufen — bitte erneut Order prüfen.";
          }
          state.previewToken = null;
        }
      }, 1000);
    }
  }

  /** Turn a raw gate message into plain German for the modal. */
  function _humanGate(msg) {
    const m = String(msg || "");
    if (/stop_loss required/i.test(m))
      return "Stop-Loss fehlt — ohne SL sind Live-Einstiege gesperrt. SL-Kurs eintragen.";
    if (/below exchange minimum/i.test(m)) {
      const mm = m.match(/minimum\s+([\d.]+)/i);
      return "Position zu klein für die Börse (Minimum " +
        (mm ? mm[1] : "?") + " " + ccy() + "). Positionsgröße erhöhen.";
    }
    if (/exceeds MAX_NOTIONAL/i.test(m))
      return "Position über dem erlaubten Maximum (MAX_NOTIONAL_USDT). Größe reduzieren.";
    if (/risk .* exceeds MAX_RISK_PCT/i.test(m))
      return "Risiko über dem Limit (MAX_RISK_PCT). SL enger setzen oder Größe reduzieren.";
    if (/RRR .* < MIN_RRR|take_profit required/i.test(m))
      return "Chance/Risiko zu niedrig — Take-Profit weiter setzen oder SL enger (min. 1:2).";
    if (/leverage .* exceeds/i.test(m))
      return "Hebel über dem Limit — Hebel reduzieren.";
    if (/available|margin/i.test(m) && /exceeds|used/i.test(m))
      return "Nicht genug freie Margin für diese Größe.";
    if (/DISARMED/i.test(m))
      return "DISARMED: Live-Trading ist aus (TRADING_ENABLED=false).";
    return m;
  }

  function _sumCell(label, value, sub, cls) {
    return (
      '<div class="sum-cell ' + (cls || "") + '">' +
      '<span class="sum-label">' + escapeHtml(label) + "</span>" +
      '<span class="sum-value">' + escapeHtml(String(value)) + "</span>" +
      (sub ? '<span class="sum-sub">' + escapeHtml(String(sub)) + "</span>" : "") +
      "</div>"
    );
  }

  function _pctVs(price, entry) {
    if (price == null || !entry) return "";
    const p = ((Number(price) - Number(entry)) / Number(entry)) * 100;
    return (p > 0 ? "+" : "") + fmt(p, 2) + "%";
  }

  function closeConfirmModal() {
    const modal = $("confirm-modal");
    if (modal) modal.classList.add("hidden");
    state.previewToken = null;
    state.previewSummary = null;
    if (state._ttlTimer) {
      clearInterval(state._ttlTimer);
      state._ttlTimer = null;
    }
  }

  async function runPreview() {
    if (state.orderBusy) return;
    setTicketError("");
    const ticket = readTicket();
    if (ticket.vol == null || ticket.vol <= 0) {
      setTicketError(
        "Positionsgröße (" + ccy() + ") eintragen — und ein Symbol laden, damit der Preis bekannt ist."
      );
      return;
    }

    const btn = $("btn-send-order");
    state.orderBusy = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Prüfe Risk-Gates…";
    }

    try {
      // Refresh arming flag
      await loadHealth();

      const res = await apiFetch("/api/orders/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ticket),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }

      if (!res.ok) {
        // Server-side error (contract/ticker/exchange) — no summary to show
        const msg = detailToText(data && data.detail);
        setTicketError(_humanGate(msg));
        showToast(_humanGate(msg), "err");
        return;
      }

      // ok OR not-ok: the modal now renders blockers, summary and confirm
      // state itself, so the user always sees WHY confirm is (un)available.
      if (data.summary) {
        openConfirmModal(data);
      } else {
        const errs = (data.errors || []).map(_humanGate).join(" · ") || "Gates abgelehnt";
        setTicketError(errs);
      }
    } catch (err) {
      console.error("runPreview", err);
      setTicketError("Netzwerkfehler: " + (err && err.message ? err.message : err));
    } finally {
      state.orderBusy = false;
      if (btn) btn.disabled = false;
      updateTriggerModeUi();
    }
  }

  async function runConfirm() {
    // Set busy immediately (before any await) so a double-click cannot
    // fire two confirms with the same preview token.
    if (state.orderBusy) return;
    if (!state.previewToken) {
      const errEl = $("confirm-error");
      if (errEl) {
        errEl.classList.remove("hidden");
        errEl.textContent = "Kein Preview-Token — zuerst Preview.";
      }
      return;
    }

    const confirmBtn = $("btn-confirm-live");
    const token = state.previewToken;
    state.orderBusy = true;
    state.previewToken = null; // one-shot client-side; server also consumes
    if (confirmBtn) {
      confirmBtn.disabled = true;
      confirmBtn.textContent = "Sende…";
    }

    try {
      await loadHealth();
      if (!state.health || !state.health.trading_enabled) {
        const errEl = $("confirm-error");
        if (errEl) {
          errEl.classList.remove("hidden");
          errEl.textContent =
            "DISARMED: TRADING_ENABLED=false — Confirm gesperrt.";
        }
        // Token was already cleared client-side; user must re-preview after arming
        return;
      }

      const res = await apiFetch("/api/orders/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token }),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }

      if (!res.ok) {
        const msg = detailToText(data && data.detail);
        const errEl = $("confirm-error");
        if (errEl) {
          errEl.classList.remove("hidden");
          errEl.textContent = msg;
        }
        showToast(msg, "err");
        loadHistory();
        return;
      }

      if (data.sl_verified === false && data.sl_checked === false) {
        showToast(
          "SL-Status UNBEKANNT — Order ist platziert. Bitte Position auf der " +
            "Börse manuell prüfen! " + (data.sl_detail || ""),
          "err"
        );
      } else if (data.sl_verified === false) {
        showToast(
          "KRITISCH: SL nicht verifiziert — " +
            (data.sl_detail || "") +
            (data.flatten ? " (Flatten versucht)" : ""),
          "err"
        );
      } else {
        showToast(
          "Order platziert" +
            (data.external_oid ? " · " + data.external_oid : ""),
          "ok"
        );
      }
      // Remember the entry candle + SL/TP so the zone starts at the fill and —
      // crucially in MANUAL mode where no exchange trigger exists — the SL/TP
      // fields are still drawn. Use the current live bar's open time.
      const sm = state.previewSummary || {};
      const sym = sm.symbol;
      if (sym) {
        const key = String(sym).toUpperCase();
        if (state.liveBar && state.liveBar.time) {
          state.tradeEntryTimes[key] = state.liveBar.time;
        }
        state.tradeMarkers[key] = {
          sl: Number(sm.stop_loss) || null,
          tp: Number(sm.take_profit) || null,
          side: sm.side,
          manual: (sm.trigger_mode || state.triggerMode) === "manual",
          ts: Date.now(),
        };
        saveTradeMarkers();
      }
      closeConfirmModal();
      loadAccount();
      loadOpenOrders();
      loadHistory();
    } catch (err) {
      console.error("runConfirm", err);
      // The server may have ALREADY placed (confirm runs seconds due to SL
      // verify). Never imply "nothing happened": warn to check the exchange,
      // block a blind re-preview, and reconcile from the exchange.
      const errEl = $("confirm-error");
      if (errEl) {
        errEl.classList.remove("hidden");
        errEl.textContent =
          "NETZWERKFEHLER beim Bestätigen — die Order ist möglicherweise " +
          "bereits platziert. NICHT erneut previewen/bestätigen! Prüfe " +
          "Positionen und Orders auf der Börse. (" +
          (err && err.message ? err.message : err) + ")";
      }
      showToast(
        "⚠ Confirm-Antwort verloren — Order evtl. LIVE. Auf der Börse prüfen, " +
          "NICHT erneut bestätigen.",
        "err"
      );
      // Reconcile so the UI reflects any order the server actually placed.
      try { loadAccount(); } catch (_) {}
      try { loadOpenOrders(); } catch (_) {}
      try { loadHistory(); } catch (_) {}
    } finally {
      state.orderBusy = false;
      if (confirmBtn) {
        confirmBtn.textContent = "Confirm LIVE";
        confirmBtn.disabled = !state.previewToken;
      }
    }
  }

  // Expose for debugging
  window.loadMarket = loadMarket;
  window.Trader = window.MexcTrader = {
    loadMarket,
    loadHealth,
    loadAccount,
    loadHistory,
    runAnalyze,
    applyProposalToTicket,
    renderProposal,
    renderScanResults,
    runScan,
    runPreview,
    runConfirm,
    drawProposalLines,
    drawPositionLines,
    drawOrderLines,
    drawTradeZones,
    renderPositions,
    runReevaluate,
    state,
  };

  document.addEventListener("DOMContentLoaded", async () => {
    // F-19: auth token now arrives as an HttpOnly session cookie sent
    // automatically with same-origin requests — nothing to read from the DOM.
    initChart();
    wireUi();
    loadTradeMarkers();
    // Another tab persisted trade markers (e.g. set a manual SL) — pick up
    // its state and redraw so we don't keep showing our stale copy (audit F3).
    window.addEventListener("storage", function (e) {
      if (e.key !== TRADE_MARKERS_KEY) return;
      loadTradeMarkers();
      try { if (state.account) renderPositions(state.account); } catch (_) {}
      try { drawTradeZones(); } catch (_) {}
    });
    const h = await loadHealth();
    if (h && h.max_leverage && $("ticket-leverage")) {
      $("ticket-leverage").max = String(h.max_leverage);
    }
    loadAccount();
    loadOpenOrders();
    loadHistory();
    loadSymbols();
    loadLlm();
    setInterval(loadAccount, 30000);
    setInterval(loadFills, 30000); // same cadence as the account poll
    setInterval(loadOpenOrders, 30000);
    // Live chart poll, adaptive:
    //  - WS live: ticks stream in real time already; full refresh
    //    (indicators, structure) every 15 s to spare the exchange API.
    //  - WS down/error: full reload every 5 s so the chart stays live.
    let pollTick = 0;
    setInterval(function () {
      if (!state.symbol) return;
      if (state.activeView !== "chart") return; // chart hidden (overview active) → skip background loads
      if (!state._chartKey) return; // overview start: no chart loaded yet → no market polling
      pollTick += 1;
      if (state.wsStatus === "live" && pollTick % 3 !== 0) return;
      loadMarket(state.symbol, state.tf || "15m", state.htf || "1H", {
        silent: true,
      });
    }, 5000);
    // E5 start behavior: the overview tab is active on load. The big chart,
    // its /api/market snapshot and the realtime WS start ONLY when the user
    // opens a coin tab / tile (goToSymbol → showChart → loadMarket).
    const symbol =
      (h && h.default_symbol) ||
      ($("symbol-input") && $("symbol-input").value) ||
      "BTC";
    const active = document.querySelector(".tf-btn.active");
    state.tf = active ? active.getAttribute("data-tf") : "15m";
    state.htf = "1H";
    state.symbol = String(symbol).toUpperCase().trim();
    const bootInput = $("symbol-input");
    if (bootInput) bootInput.value = state.symbol;
    loadOpenTabs(); // needs state.symbol as fallback seed
    sizeTradeOverlay();

    loadWatchlist();
    renderSymbolTabs(); // draw the fixed overview tab now that tabs exist
    const watchForm = $("watch-add");
    if (watchForm) {
      watchForm.addEventListener("submit", function (e) {
        e.preventDefault();
        const inp = $("watch-input");
        if (inp && inp.value.trim()) {
          addWatch(inp.value);
          inp.value = "";
        }
      });
    }
    // Snapshot refresh, only while the overview tab is visible (no background work).
    state._overviewTimer = setInterval(function () {
      if (state.activeView === "overview") {
        refreshOverview();
        refreshNews(); // internally throttled to 5 min
      }
    }, 30000);

    showOverview(); // start on the overview tab; also triggers the first mini refresh
  });
})();

