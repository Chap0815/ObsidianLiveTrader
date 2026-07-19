/**
 * Obsidian Live Trader (Hyperliquid + MEXC) — trader cockpit chart + ticket.
 * Lightweight Charts expects Unix time in seconds.
 */
(function () {
  "use strict";

  // A3-02 (Task 43): the sealed `state` object + its `Object.seal` now live in
  // store.js (a classic global script loaded FIRST in base.html, before
  // utils.js). app.js references that ONE global `state` by bare name — it no
  // longer declares its own (a local `const state` here would SHADOW the store
  // and re-split the single source of truth). PERSIST (localStorage key table)
  // is likewise a store.js global. All `state.X` call sites below are unchanged.

  function $(id) {
    return document.getElementById(id);
  }

  // A3-07 (Task 42): authHeaders / apiFetch moved to api.js (the pure,
  // STATE-FREE network core), loaded as a global classic script BEFORE app.js
  // — call sites below reference them by bare name, unchanged. The per-resource
  // abort helpers apiFetchAbortable / abortResource also live there. The former
  // `state.localToken` fallback is now api.js's module-level `_localToken`
  // (the former field was always "" at runtime, never assigned, now removed).

  function updateOrderButtonsEnabled() {
    const btn = $("btn-send-order");
    if (!btn) return;
    const blocked = state.apiAllowed === false;
    btn.disabled = blocked || state.orderBusy;
    btn.title = blocked
      ? "apiAllowed=false — API-Orders für dieses Symbol gesperrt"
      : "Preview → Confirm";
  }

  // A3-01: fmt, fmtPct, toChartTime moved to utils.js (pure format helpers,
  // loaded as globals before app.js). Call sites unchanged.

  /** C3-11: LWC v4 renders epoch timestamps as UTC by default — for a DE
   *  user (UTC+2) the axis and crosshair are hours off the wall clock, and
   *  disagree with the trades panel's (already-local) relative times.
   *  FORMAT-ONLY: this only changes how a `time` value is displayed
   *  (crosshair label / axis tick), never the stored value itself — the bar
   *  time bucketing (barOpenTimeSec) that keeps candles/fills consistent is
   *  untouched. `time` here is always a plain UTCTimestamp (seconds) since
   *  every series in this file is fed via candlesToSeries/emaToSeries with
   *  numeric `time`, never LWC's BusinessDay form. */
  function chartLocalTime(time, withSeconds) {
    const sec = Number(time);
    if (!Number.isFinite(sec)) return "";
    const d = new Date(sec * 1000);
    return withSeconds
      ? d.toLocaleTimeString("de-DE")
      : d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  }

  // U-06: `ok` is normally boolean, but `null`/`undefined` (status not yet
  // known, e.g. before the first /api/health response) resets the dot to
  // the neutral "unknown" look instead of forcing a false "bad" red.
  function setDot(el, ok) {
    if (!el) return;
    el.classList.remove("ok", "bad", "unknown");
    el.classList.add(ok == null ? "unknown" : ok ? "ok" : "bad");
  }

  // D3-01: ONE color source for the chart/canvas overlays — read once from
  // the CSS custom properties (the same tokens the UI chrome uses for P&L),
  // cached, instead of scattering hex literals across every draw function.
  // Populated lazily on first use rather than at module-parse time: the
  // stylesheet is linked in <head> and this script runs at the end of
  // <body> (see base.html), so by the time any chart function runs the CSS
  // is already applied — but initChart() also primes the cache explicitly
  // as its first step, so nothing ever reads it before the chart exists.
  let _chartColors = null;
  function getChartColors() {
    if (_chartColors) return _chartColors;
    const cs = getComputedStyle(document.documentElement);
    const v = function (name, fallback) {
      const val = cs.getPropertyValue(name);
      return val && val.trim() ? val.trim() : fallback;
    };
    _chartColors = {
      // Named-color (not hex) fallbacks here on purpose: this branch should
      // never fire per the load-order guarantee above, and using a plain
      // CSS keyword rather than a hex literal keeps the old dual-palette
      // hexes (D3-01) from ever reappearing in this file, even as dead code.
      long: v("--long", "green"),
      short: v("--short", "red"),
      ema20: v("--chart-ema20", "#5d7690"),
      ema50: v("--chart-ema50", "#7d7690"),
      kiEntry: v("--chart-ki-entry", "#b79cff"),
      order: v("--chart-order", "#8b7ae6"),
      ticketEntry: v("--chart-ticket-entry", "#5aa6e6"),
      level: v("--chart-level", "#9d9ab6"),
      pool: v("--chart-pool", "#6b6788"),
      liq: v("--chart-liq", "#c0392b"),
      longSoft: v("--chart-long-soft", "#a0d8c0"),
      shortSoft: v("--chart-short-soft", "#e6a0a0"),
    };
    return _chartColors;
  }

  // Parse a cached "#rrggbb" (or shorthand "#rgb") token and return it as an
  // rgba() string at the given alpha — used for the translucent SL/TP fields
  // and dimmed trade-close markers, which need an alpha the CSS token itself
  // doesn't carry.
  function chartColorAlpha(hex, alpha) {
    let h = String(hex || "").trim().replace(/^#/, "");
    if (h.length === 3) {
      h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    }
    const n = parseInt(h, 16);
    if (!Number.isFinite(n) || h.length !== 6) return hex;
    const r = (n >> 16) & 255;
    const g = (n >> 8) & 255;
    const b = n & 255;
    return "rgba(" + r + ", " + g + ", " + b + ", " + alpha + ")";
  }

  function initChart() {
    const chartColors = getChartColors();
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
      // C3-11: local (de-DE) time on axis + crosshair instead of raw UTC —
      // format-only, see chartLocalTime() above.
      localization: {
        timeFormatter: function (time) {
          return chartLocalTime(time, true);
        },
      },
      timeScale: {
        borderColor: "#2b2740",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6, // breathing room next to the live candle
        barSpacing: 7,
        tickMarkFormatter: function (time) {
          return chartLocalTime(time, false);
        },
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
        upColor: chartColors.long,
        downColor: chartColors.short,
        borderUpColor: chartColors.long,
        borderDownColor: chartColors.short,
        wickUpColor: chartColors.long,
        wickDownColor: chartColors.short,
      });
      state.ema20Series = chart.addLineSeries({
        color: chartColors.ema20,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: "EMA20",
      });
      state.ema50Series = chart.addLineSeries({
        color: chartColors.ema50,
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
        upColor: chartColors.long,
        downColor: chartColors.short,
        borderUpColor: chartColors.long,
        borderDownColor: chartColors.short,
        wickUpColor: chartColors.long,
        wickDownColor: chartColors.short,
      });
      state.ema20Series = chart.addSeries(LightweightCharts.LineSeries, {
        color: chartColors.ema20,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: "EMA20",
      });
      state.ema50Series = chart.addSeries(LightweightCharts.LineSeries, {
        color: chartColors.ema50,
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

    // C3-04a: fill-marker hover tooltip (perf-critical handler — see the
    // block comment above handleMarkerCrosshairMove). try/catch only
    // guards against a hypothetical older LWC build without the API; the
    // vendored v4.2.0 bundle supports it.
    try {
      state.chart.subscribeCrosshairMove(handleMarkerCrosshairMove);
    } catch (_) {
      /* older LWC without hoveredObjectId support */
    }

    sizeTradeOverlay();

    // C3-04b: wire SL-line drag once. Hover hit-test lives on #chart-wrap so
    // it keeps firing whether the cursor is over the chart OR the (now
    // interactive) overlay — both bubble here. The pointer handlers live on
    // the overlay canvas, which is only reachable while armed (±4px hit zone),
    // so normal chart pan/zoom off the SL line is never intercepted.
    if (!state._slDragWired) {
      const overlay = tradeOverlayCanvas();
      const wrap = $("chart-wrap");
      if (overlay && wrap) {
        wrap.addEventListener("mousemove", onSlHoverMove);
        wrap.addEventListener("mouseleave", onSlWrapLeave);
        overlay.addEventListener("pointerdown", onSlDragStart);
        overlay.addEventListener("pointermove", onSlDragMove);
        overlay.addEventListener("pointerup", onSlDragEnd);
        overlay.addEventListener("pointercancel", onSlDragCancel);
        document.addEventListener("keydown", onSlDragKey);
        state._slDragWired = true;
      }
    }
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
    const chartColors = getChartColors();
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
        // T41b: one frontend source for SL/TP classification (field → label →
        // geometry), mirroring app/orders/protection.py.
        const c = classifyTriggers(s, short ? "short" : "long", entry);
        if (c.sl != null) sl = c.sl;
        if (c.tp != null) tp = c.tp;
      });
      // Fallback for MANUAL mode (no exchange trigger): the SL/TP the trader
      // set at entry, remembered on confirm. This is the ONE place the manual
      // trader still gets a visual SL/TP zone.
      const mk = state.tradeMarkers && state.tradeMarkers[markerKey(p.symbol)];
      if (mk) {
        if (sl == null && mk.sl) sl = mk.sl;
        if (tp == null && mk.tp) tp = mk.tp;
      }

      const yEntry = series.priceToCoordinate(entry);
      if (yEntry == null) return;

      // x-start: entry candle time. C3-07: neither in-memory
      // tradeEntryTimes (lost on reload) nor a fixed bucket (breaks on TF
      // switch — a 15m bucket is not a bar time on the 4H chart) survive.
      // Prefer the oldest known OPEN fill (HL only, self-healing, needs no
      // storage), else the persisted raw-ms entry time re-bucketed to the
      // CURRENT tf, else the legacy in-memory value as a last resort.
      let xStart = 0;
      let et = oldestOpenFillTime(p.symbol);
      if (et == null && mk && mk.entryMs) {
        et = barOpenTimeSec(mk.entryMs, state.tf || "15m");
      }
      if (et == null) {
        et = state.tradeEntryTimes && state.tradeEntryTimes[p.symbol];
      }
      if (et != null) {
        const xc = ts.timeToCoordinate(et);
        if (xc != null) xStart = Math.max(0, xc);
      }
      const xEnd = plotW;
      // Entry candle scrolled off to the right → avoid negative-width rects.
      if (xStart > xEnd) xStart = 0;

      const vol = Number(p.hold_vol) || 0;
      // F-10: this position's own contract_size drives the $ risk band; fall
      // back to the active chart symbol's contractSize only if it's missing.
      const cs = positionContractSize(p.contract_size, contractSize());
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
          const rm = rMultiple(price, entry, sl);
          if (rm != null) extra += " · " + fmt(rm, 1) + "R";
        }
        ctx.fillStyle = colorLine;
        ctx.font = "10px 'IBM Plex Mono', monospace";
        // C3-03: the entry label always sits at yEntry-4. When this band's
        // line lands within 14px of the entry line, that default y-4
        // baseline would overlap the entry text — nudge this label further
        // away from entry (to a fixed 14px clearance) instead of stacking
        // unreadable text on the left edge.
        let labelY = y - 4;
        if (Math.abs(y - yEntry) < 14) {
          labelY = y <= yEntry ? yEntry - 18 : yEntry + 10;
        }
        ctx.fillText(label + " " + fmt(price, 4) + extra, xStart + 6, labelY);
      }

      band(sl, chartColorAlpha(chartColors.short, 0.17), chartColors.short, "SL");
      band(tp, chartColorAlpha(chartColors.long, 0.17), chartColors.long, "TP");

      // entry line (neutral)
      ctx.strokeStyle = short ? chartColors.shortSoft : chartColors.longSoft;
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xStart, yEntry);
      ctx.lineTo(xEnd, yEntry);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = short ? chartColors.shortSoft : chartColors.longSoft;
      ctx.font = "10px 'IBM Plex Mono', monospace";
      ctx.fillText((short ? "Short " : "Long ") + fmt(entry, 4), xStart + 6, yEntry - 4);
    });

    // C3-04b: ghost SL line during an active drag — the WOULD-BE new stop,
    // following the cursor. Purely visual: nothing is sent until the trader
    // confirms on drop. Shows the new price + resulting $ risk and RRR (cheap:
    // entry/vol/cs/tp were snapshotted at drag-start).
    const drag = state._slDrag;
    if (drag && symMatch(drag.symbol, state.symbol) && Number.isFinite(drag.newSl)) {
      const gy = series.priceToCoordinate(drag.newSl);
      if (gy != null) {
        ctx.save();
        ctx.strokeStyle = chartColors.short;
        ctx.setLineDash([6, 4]);
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        ctx.moveTo(0, gy);
        ctx.lineTo(plotW, gy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
        const risk = Math.abs(drag.entry - drag.newSl) * (drag.vol || 0) * (drag.cs || 1);
        let lbl = "SL→ " + fmt(drag.newSl, 4) + " · −" + fmt(risk, 2);
        if (Number.isFinite(drag.tp) && drag.tp != null && risk > 0) {
          const rm = rMultiple(drag.tp, drag.entry, drag.newSl);
          if (rm != null) lbl += " · " + fmt(rm, 1) + "R";
        }
        // wrong-side hint so the trader sees the drop will be rejected
        const wrongSide = drag.side === "long" ? drag.newSl >= drag.entry : drag.newSl <= drag.entry;
        if (wrongSide) lbl += "  ⚠ falsche Seite";
        ctx.fillStyle = chartColors.short;
        ctx.font = "11px 'IBM Plex Mono', monospace";
        ctx.fillText(lbl, 8, gy - 5);
        ctx.restore();
      }
    }
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

  /* ── Chart line groups ──────────────────────────────────
     Three independent overlays on the candle series, plus the dashed ticket
     lines. Each is reconciled through applyLineGroup() (C3-12) — the group's
     spec set is diffed and only changed lines are touched (applyOptions /
     create / remove) instead of remove+recreate on every poll:
     - proposalLines: what the KI suggests (Entry/SL/TP + Levels)
     - positionLines: what is actually open (Entry/Liq)
     - orderLines:    resting orders + active SL/TP triggers
     - priceLines:    the ticket's own dashed Entry/Limit/SL/TP lines */

  // axisLabel (the price box on the right) OFF by default — it stacks and
  // clutters. The title stays as a small label on the line. Only a few key
  // lines (Liq, current price) keep the numeric axis label.
  //
  // C3-12: this no longer creates the line immediately — it appends a *spec* to
  // `specs`. applyLineGroup() then diffs the spec set against the last render
  // and only touches (applyOptions / create / remove) the lines that changed,
  // instead of tearing every line down and rebuilding it each poll (flicker).
  function addChartLine(specs, price, color, style, title, width, axisLabel) {
    const px = Number(price);
    if (!Number.isFinite(px) || px <= 0) return;
    specs.push({
      price: px,
      color: color,
      style: style, // 0 solid, 1 dotted, 2 dashed, 4 sparse dotted
      title: title || "",
      width: width || 1,
      axisLabel: axisLabel === true,
    });
  }

  function _lineOpts(s) {
    return {
      price: s.price,
      color: s.color,
      lineWidth: s.width || 1,
      lineStyle: s.style,
      axisLabelVisible: s.axisLabel === true,
      title: s.title || "",
    };
  }

  function _sameLineSpec(a, b) {
    return (
      !!a && !!b &&
      a.price === b.price &&
      a.color === b.color &&
      a.style === b.style &&
      (a.width || 1) === (b.width || 1) &&
      (a.axisLabel === true) === (b.axisLabel === true) &&
      (a.title || "") === (b.title || "")
    );
  }

  /** C3-12: reconcile one price-line group against a fresh spec list. Reuses
   *  the existing IPriceLine objects and only calls applyOptions() on the ones
   *  whose (price,title,color,style,width,axisLabel) actually changed; creates
   *  the extra, removes the surplus. When the whole set is identical it does
   *  NOTHING — no more remove+recreate flicker on every poll. `plArr` is
   *  mutated in place so external references (state.positionLines, …) stay
   *  valid. */
  function applyLineGroup(plArr, cacheKey, specs) {
    if (!state.candleSeries) return;
    specs = (specs || []).filter(function (s) {
      const p = Number(s.price);
      return Number.isFinite(p) && p > 0;
    });
    const prev = state._lineSpecs[cacheKey] || [];
    let same = prev.length === specs.length;
    if (same) {
      for (let i = 0; i < specs.length; i++) {
        if (!_sameLineSpec(prev[i], specs[i])) { same = false; break; }
      }
    }
    if (same) return;
    for (let i = 0; i < specs.length; i++) {
      if (i < plArr.length) {
        if (!_sameLineSpec(prev[i], specs[i])) {
          try {
            plArr[i].applyOptions(_lineOpts(specs[i]));
          } catch (_) {
            // LWC build without price-line applyOptions → remove + recreate
            // just this one line.
            try { state.candleSeries.removePriceLine(plArr[i]); } catch (__) {}
            try { plArr[i] = state.candleSeries.createPriceLine(_lineOpts(specs[i])); } catch (__) {}
          }
        }
      } else {
        try { plArr.push(state.candleSeries.createPriceLine(_lineOpts(specs[i]))); } catch (_) {}
      }
    }
    while (plArr.length > specs.length) {
      const pl = plArr.pop();
      try { state.candleSeries.removePriceLine(pl); } catch (_) {}
    }
    state._lineSpecs[cacheKey] = specs;
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
    let ex = state.health && state.health.exchange;
    if (!ex) {
      // /api/health not loaded yet: seed the exchange from the server-rendered
      // label so HL positions (bare base-coin symbols like "BTC") aren't
      // transiently hidden by the full-string fallback on first paint (audit F4).
      const lbl = $("exchange-label");
      ex = lbl ? lbl.textContent.trim().toLowerCase() : "";
    }
    const isHyperliquid = ex === "hyperliquid";
    if (isHyperliquid) {
      const ca = na.split("_")[0];
      const cb = nb.split("_")[0];
      return ca !== "" && ca === cb;
    }
    return na === nb;
  }

  /** Task 40 (N3-14): same exchange lookup as symMatch, exposed standalone —
   *  round-trip folding of fills is HL-only for now (MEXC stays Stufe 2/out
   *  of scope per the task brief), gated on this instead of hardcoding. */
  function isHlExchange() {
    let ex = state.health && state.health.exchange;
    if (!ex) {
      const lbl = $("exchange-label");
      ex = lbl ? lbl.textContent.trim().toLowerCase() : "";
    }
    return ex === "hyperliquid";
  }

  /** Canonical key for state.tradeMarkers (A3-01). MUST resolve to the same
   *  form the backend/exchange reports for positions & confirm summaries —
   *  otherwise an exact-key read (position zones, mini-tiles, the manual-SL
   *  alarm) can silently miss a marker written under a different form. Mirrors
   *  symMatch's own HL normalization (and the backend's normalize_symbol in
   *  security.py): MEXC always uses the full "COIN_QUOTE" pair, Hyperliquid
   *  canonicalizes to the bare coin. ONE place all tradeMarkers reads/writes
   *  route through, so there is exactly one key schema. */
  function markerKey(sym) {
    const s = String(sym || "").toUpperCase().trim();
    if (!s) return "";
    let ex = state.health && state.health.exchange;
    if (!ex) {
      // /api/health not loaded yet: same DOM fallback as symMatch (audit F4).
      const lbl = $("exchange-label");
      ex = lbl ? lbl.textContent.trim().toLowerCase() : "";
    }
    return ex === "hyperliquid" ? s.split("_")[0] : s;
  }

  /** Three-way fill classification (C3-02): Hyperliquid's free-text `dir`
   *  field says what actually happened. A naive `dir.indexOf("close")`
   *  check only recognizes ordinary closes — liquidations ("Liquidated
   *  Long") and position flips ("Long > Short") contain no "close"
   *  substring, so they fell through to the OPEN branch and the worst
   *  possible event (a liquidation) rendered as a normal, full-color,
   *  deliberate-looking entry marker. Detect both explicitly so they get
   *  their own (unmissable) treatment instead. */
  function classifyFillDir(dir) {
    const s = String(dir || "").toLowerCase();
    if (s.indexOf("liquidat") !== -1 || s.indexOf(">") !== -1) return "liq";
    if (s.indexOf("close") !== -1) return "close";
    return "open";
  }

  /** C3-07: the most durable source for "when did this position start" is
   *  the oldest still-known OPEN fill for the symbol — it needs no
   *  persistence at all and survives a full localStorage wipe. Preferred
   *  over the persisted tradeMarkers.entryMs when fills are loaded (HL
   *  only; MEXC has no fill history so this always falls through). Returns
   *  the entry bucketed to the CURRENT tf, or null if no open fill is known. */
  function oldestOpenFillTime(symbol) {
    const fills = state.fills || [];
    let minMs = null;
    fills.forEach(function (f) {
      if (!symMatch(f.symbol, symbol)) return;
      if (classifyFillDir(f.dir) !== "open") return;
      const t = Number(f.time);
      if (!(t > 0)) return;
      if (minMs == null || t < minMs) minMs = t;
    });
    return minMs == null ? null : barOpenTimeSec(minMs, state.tf || "15m");
  }

  function drawProposalLines() {
    const chartColors = getChartColors();
    const specs = [];
    const p = state.proposal;
    // With a live trade open, the chart focuses on the trade (position + zones);
    // the KI planning overlay would just clutter it.
    const show =
      state.candleSeries && state.showAiLines && !hasActivePosition() &&
      p && p.action !== "STAY_OUT" &&
      !(state.proposalSymbol && !symMatch(state.proposalSymbol, state.symbol));
    if (show) {
      // R-multiple labels: how much reward per unit of risk each TP pays
      const tpTitle = function (name, tp) {
        const r = rMultiple(tp, p.entry_price, p.stop_loss);
        return r == null ? name : name + " +" + r.toFixed(1) + "R";
      };
      // Core trade — hidden once applied to the ticket (ticket lines take over)
      if (!state.proposalApplied) {
        addChartLine(specs, p.entry_price, chartColors.kiEntry, 2, "KI Entry", 2);
        addChartLine(specs, p.stop_loss, chartColors.short, 1, "KI SL -1R");
        addChartLine(specs, p.tp1, chartColors.long, 1, tpTitle("KI TP1", p.tp1));
      }
      addChartLine(specs, p.tp2, chartColors.long, 4, tpTitle("KI TP2", p.tp2));
      addChartLine(specs, p.tp3, chartColors.long, 4, tpTitle("KI TP3", p.tp3));

      // Analysis levels
      const kl = p.key_levels || {};
      addChartLine(specs, kl.immediate_support, chartColors.level, 4, "Support");
      addChartLine(specs, kl.immediate_resistance, chartColors.level, 4, "Resist");
      const pools = Array.isArray(kl.major_liquidity_pools)
        ? kl.major_liquidity_pools
        : [];
      pools.slice(0, 3).forEach(function (v) {
        addChartLine(specs, Number(v), chartColors.pool, 4, "Pool");
      });
    }
    applyLineGroup(state.proposalLines, "proposal", specs);
  }

  function drawPositionLines() {
    const chartColors = getChartColors();
    const specs = [];
    const positions = (state.account && state.account.positions) || [];
    positions.forEach(function (p) {
      if (!symMatch(p.symbol, state.symbol)) return;
      const short = String(p.side || "").toLowerCase() === "short";
      const entry = Number(p.entry_price);
      addChartLine(
        specs,
        entry,
        short ? chartColors.short : chartColors.long,
        0,
        (short ? "Short" : "Long") + " " + fmt(p.hold_vol, 4),
        2
      );
      // Break-even incl. ~round-trip taker fees (0.06% total) so "SL to BE"
      // actually covers costs, not just the raw entry.
      const be = breakEvenPrice(entry, short);
      if (be != null) {
        addChartLine(specs, be, chartColors.level, 1, "BE≈");
      }
      // Liquidation — the survival line; keeps its numeric axis label.
      addChartLine(specs, p.liquidate_price, chartColors.liq, 3, "⚠ LIQ", undefined, true);
    });
    applyLineGroup(state.positionLines, "position", specs);
  }

  function drawOrderLines() {
    const chartColors = getChartColors();
    const specs = [];
    const d = state.openOrders || {};
    (d.orders || []).forEach(function (o) {
      if (o.symbol && !symMatch(o.symbol, state.symbol)) return;
      addChartLine(specs, o.price, chartColors.order, 2, "Order");
    });
    (d.stop_orders || []).forEach(function (s) {
      if (s.symbol && !symMatch(s.symbol, state.symbol)) return;
      let drew = false;
      const slPx = Number(s.stopLossPrice);
      const tpPx = Number(s.takeProfitPrice);
      if (Number.isFinite(slPx) && slPx > 0) {
        addChartLine(specs, slPx, chartColors.short, 2, "SL aktiv");
        drew = true;
      }
      if (Number.isFinite(tpPx) && tpPx > 0) {
        addChartLine(specs, tpPx, chartColors.long, 2, "TP aktiv");
        drew = true;
      }
      if (!drew) {
        const px = Number(s.triggerPrice != null ? s.triggerPrice : s.price);
        // T41b: shared label classifier (mirrors app/orders/protection.py) —
        // the combined "tpsl" order carries a STOP and now correctly draws as
        // "SL aktiv", not "TP aktiv" (the old `indexOf("tp") === 0` prefix
        // rule mislabeled it). No side/entry here, so an unlabeled trigger
        // stays SL for visibility (never fabricated as a favourable TP line).
        const isTp = classifyTriggerLabel(s.orderType) === "tp";
        addChartLine(
          specs,
          px,
          isTp ? chartColors.long : chartColors.short,
          2,
          isTp ? "TP aktiv" : "SL aktiv"
        );
      }
    });
    applyLineGroup(state.orderLines, "order", specs);
  }

  /** All currently active order/position price levels for the active symbol —
   *  the SAME source drawPositionLines/drawOrderLines draw from, read
   *  directly (not the priceLine objects) so this works regardless of draw
   *  order. Used to dedupe the ticket's own dashed lines against them
   *  (C3-03/T3-08): once a "real" order/position line already marks a price,
   *  the ticket-dashed line at that same price is pure redundant clutter —
   *  and on the left edge, unreadable overlap. */
  function activeLinePrices() {
    const out = [];
    const positions = (state.account && state.account.positions) || [];
    positions.forEach(function (p) {
      if (!symMatch(p.symbol, state.symbol)) return;
      const entry = Number(p.entry_price);
      if (Number.isFinite(entry) && entry > 0) {
        out.push(entry);
        const short = String(p.side || "").toLowerCase() === "short";
        out.push(breakEvenPrice(entry, short)); // BE≈
      }
      const liq = Number(p.liquidate_price);
      if (Number.isFinite(liq) && liq > 0) out.push(liq);
    });
    const d = state.openOrders || {};
    (d.orders || []).forEach(function (o) {
      if (o.symbol && !symMatch(o.symbol, state.symbol)) return;
      const px = Number(o.price);
      if (Number.isFinite(px) && px > 0) out.push(px);
    });
    (d.stop_orders || []).forEach(function (s) {
      if (s.symbol && !symMatch(s.symbol, state.symbol)) return;
      const slPx = Number(s.stopLossPrice);
      const tpPx = Number(s.takeProfitPrice);
      let drew = false;
      if (Number.isFinite(slPx) && slPx > 0) {
        out.push(slPx);
        drew = true;
      }
      if (Number.isFinite(tpPx) && tpPx > 0) {
        out.push(tpPx);
        drew = true;
      }
      if (!drew) {
        const px = Number(s.triggerPrice != null ? s.triggerPrice : s.price);
        if (Number.isFinite(px) && px > 0) out.push(px);
      }
    });
    return out;
  }

  function drawTicketLines() {
    if (!state.candleSeries) return;
    const chartColors = getChartColors();

    // Same tick/price_unit source the rest of the app reads (contract meta
    // from /api/market); falls back to a small relative epsilon so float
    // dust never blocks the dedupe when it's unavailable (e.g. before the
    // first market load).
    const priceUnit = Number(
      state.market && state.market.contract && state.market.contract.priceUnit
    );
    const tickTol = priceUnit > 0 ? priceUnit : null;
    const activePx = activeLinePrices();
    function dupesActiveLine(px) {
      return activePx.some(function (ap) {
        const tol = tickTol != null ? tickTol : Math.max(Math.abs(px), Math.abs(ap)) * 1e-6;
        return Math.abs(px - ap) <= tol;
      });
    }

    // SL/TP resolve through the price/% mode; entry & limit are always prices
    const ticketSpecs = [
      { price: numOrNull($("ticket-entry")), color: chartColors.ticketEntry, title: "Entry" },
      { price: numOrNull($("ticket-price")), color: chartColors.order, title: "Limit" },
      { price: resolveStop(), color: chartColors.short, title: "SL" },
      { price: resolveTp(), color: chartColors.long, title: "TP1" },
    ];

    const specs = [];
    for (const s of ticketSpecs) {
      const val = s.price;
      if (!Number.isFinite(val) || val <= 0) continue;
      // T3-08: an active order/position line already marks this price (±1
      // tick) — keep THAT one (it's the real thing) and drop this redundant
      // ticket-dashed duplicate instead of stacking two lines at one price.
      if (dupesActiveLine(val)) continue;
      // C3-12: diffed against last render — dashed ticket lines only get
      // touched when a value actually moved, no per-poll remove+recreate.
      addChartLine(specs, val, s.color, 2, s.title, 1, true);
    }
    applyLineGroup(state.priceLines, "ticket", specs);
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
    if (state.fundingNextSettle == null) {
      // U-07: exchange (or symbol) doesn't report a settle time anymore
      // (e.g. switched to Hyperliquid) — stop the 1s tick, it would just
      // spin forever writing "" into a hidden countdown span.
      if (state._fundingCdTimer) {
        clearInterval(state._fundingCdTimer);
        state._fundingCdTimer = null;
      }
    } else if (cd && !state._fundingCdTimer) {
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

  const STALE_PRICE_MS = 30000; // E3-01: no price tick for this long => feed considered stale, PERIOD
  const APP_PING_INTERVAL_MS = 10000; // Task 4: client app-ping cadence, piggybacked on the 5s poll tick
  const APP_PONG_TIMEOUT_MS = 25000; // Task 4: no pong for this long => socket is dead, force reconnect
  const MARK_OFFSET_TTL_MS = 90000; // E3-06: a Mark−Last offset older than ~3 account polls is stale => fall back to the raw tick

  /** E3-06: the exchange Mark−Last offset stored for `sym` on the last account
   *  poll, or 0 when unknown/stale. The exchange computes uPnL/ROE from its MARK
   *  price; the live WS feed is the LAST-traded tick — so without this the card
   *  jumps every 30s between the tick-derived value and the poll's mark-derived
   *  one. Adding this offset to the live tick makes the streamed value track the
   *  mark. Fails safe: a missing or stale (> TTL) offset returns 0 (raw tick),
   *  never a wrong correction. */
  function _markOffsetFor(sym) {
    const rec = state._markOffset && state._markOffset[String(sym || "").toUpperCase()];
    if (!rec) return 0;
    if (Date.now() - Number(rec.ts || 0) > MARK_OFFSET_TTL_MS) return 0; // stale → raw tick
    const off = Number(rec.offset);
    return Number.isFinite(off) ? off : 0;
  }

  /** U-02/E3-01: manual-SL protection (checkManualSlAlarm) and uPnL only ever
   *  fire from a live price tick — so if the feed goes dark, nothing
   *  re-evaluates and the trader is never told their SL is now unmonitored.
   *  Staleness is judged PURELY by tick age, regardless of wsStatus: a
   *  zombie-live WS (laptop sleep, silent HL subscription loss, network
   *  change without a socket close) still reports wsStatus "live" while
   *  serving nothing but frozen prices — the previous `wsDown && age>15s`
   *  gate never caught that case (E3-01, HOCH). wsStatus is only used to
   *  pick the banner's wording. Called from setLivePrice (clears it the
   *  instant a fresh tick lands), setChartMeta and the 5s background poll
   *  (so it also FIRES when no tick arrives at all, not only on the next one). */
  function updateStaleBanner() {
    const el = $("stale-banner");
    const priceEl = $("ctx-price");
    const wsDown = state.wsStatus === "error" || state.wsStatus === "off";
    const age = state._lastTickTs != null ? Date.now() - state._lastTickTs : null;
    const stale = age != null && age > STALE_PRICE_MS;
    if (el) {
      el.classList.toggle("hidden", !stale);
      if (stale) {
        el.textContent = wsDown
          ? "WS getrennt — Preis veraltet, Manual-SL wird NICHT überwacht"
          : "Feed eingefroren — Preis veraltet, Manual-SL wird NICHT überwacht";
      }
    }
    if (priceEl) priceEl.classList.toggle("price-stale", stale);
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
    updateStaleBanner(); // U-02: WS status changes here too, so re-check
  }

  // A3-01: tfSeconds, barOpenTimeSec moved to utils.js (pure helpers, loaded
  // as globals before app.js). Call sites unchanged.

  /** Manual positions carry NO exchange stop. When the live price reaches the
   *  SL zone, warn LOUDLY — but only while the browser is open. Dedup per
   *  symbol so it fires once per breach, not on every tick. */
  function checkManualSlAlarm(px) {
    updateStaleBanner(); // U-02: re-check on every tick so the banner clears immediately
    if (px == null || !Number.isFinite(Number(px))) return;
    px = Number(px);
    const positions = (state.account && state.account.positions) || [];
    positions.forEach(function (p) {
      const key = markerKey(p.symbol); // A3-01: same schema the marker was written under
      if (!symMatch(p.symbol, state.symbol)) return; // only compare live price against the active symbol's SL
      const mk = state.tradeMarkers && state.tradeMarkers[key];
      if (!mk || !mk.manual || !mk.sl) { state.slAlarm[key] = false; return; }
      const sl = Number(mk.sl);
      if (!Number.isFinite(sl) || sl <= 0) return;
      const short = String(p.side || "").toLowerCase() === "short";
      // E3-06 SAFETY: the alarm must fail TOWARD alerting. We test BOTH the raw
      // last-traded tick AND the mark-corrected price and fire on the UNION — so
      // the Mark/Last offset can only make the alarm trigger EARLIER, never
      // suppress it. A missing/stale offset makes markPx == px (raw-only), so a
      // bad offset can never silence a real touch.
      const rawTouched = short ? px >= sl : px <= sl;
      const markPx = px + _markOffsetFor(p.symbol);
      const markTouched = short ? markPx >= sl : markPx <= sl;
      const touched = rawTouched || markTouched;
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
        // E3-06 reconciliation: a touch may mean the exchange SL (or the trader
        // on another device) just closed the position. Pull a fresh account
        // snapshot NOW so a position closed externally stops re-firing this
        // alarm within seconds instead of lingering "open" for up to 30s.
        // Throttled so a price flapping across the SL can't hammer /api/account.
        const _now = Date.now();
        if (_now - (state._lastSlRecon || 0) > 5000) {
          state._lastSlRecon = _now;
          try { loadAccount(); } catch (_) { /* fire-and-forget reconciliation */ }
        }
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
    state._lastTickTs = Date.now(); // U-02: feed is alive — feeds the stale-banner check
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
    // T3-11: in %-mode, resolveStop()/resolveTp() track the live price via
    // refEntryPrice()'s market-order fallback — without a redraw here the
    // ticket's dashed SL/TP chart lines freeze at whatever price they last
    // resolved to, instead of following the tape like the %-distance implies.
    // Throttled to ~1/s (not every tick) since a full line redraw per tick
    // would be wasted work on a fast feed.
    if (sltpMode() === "pct") {
      const now = Date.now();
      if (!state._ticketLinesLastDraw || now - state._ticketLinesLastDraw >= 1000) {
        state._ticketLinesLastDraw = now;
        try { drawTicketLines(); } catch (e) { console.error("drawTicketLines", e); }
      }
    }
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
      // E3-06: correct the live last-traded tick toward the exchange MARK so
      // uPnL/ROE stops jumping every 30s between this tick-derived value and the
      // account poll's mark-derived one. Stale/missing offset → +0 (raw tick).
      const mpx = px + _markOffsetFor(sym);
      const pnl = computePnl(mpx, entry, vol, cs, short);
      const roe = computeRoe(pnl, im);
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

  function updateWsBadge() {
    const badge = $("rt-badge");
    if (badge) {
      badge.classList.toggle("ok-live", state.wsStatus === "live");
      badge.title =
        state.wsStatus === "live"
          ? "Realtime verbunden"
          : "Realtime: " + state.wsStatus;
    }
  }

  /** E3-07: shared by onclose AND the `new WebSocket()` constructor-throw
   *  catch, so a synchronous construction failure (rare, but observed on
   *  some browsers/extensions) backs off and retries exactly like a normal
   *  disconnect instead of leaving realtime dead forever. Reuses the single
   *  state._wsReconnect timer — never schedules a second one. */
  function scheduleWsReconnect() {
    state.wsRetry = Math.min((state.wsRetry || 0) + 1, 5);
    const delay = Math.min(2000 * Math.pow(2, state.wsRetry - 1), 30000);
    clearTimeout(state._wsReconnect);
    state._wsReconnect = setTimeout(function () {
      if (!state.ws && state.symbol) {
        startRealtime(state.symbol, state.tf);
      }
    }, delay);
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
      updateWsBadge();
      scheduleWsReconnect(); // E3-07: a constructor throw must not kill realtime for good
      return;
    }
    state.ws = ws;

    ws.onopen = function () {
      state.wsStatus = "connecting";
      // Task 4: fresh grace period for the app-ping watchdog on every new
      // socket — otherwise a pong timestamp from the PREVIOUS connection
      // could immediately look "expired" and force-close the brand new one.
      state._lastPongTs = Date.now();
      state._lastAppPingTs = 0;
    };

    ws.onmessage = function (ev) {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      if (!msg || !msg.type) return;

      if (msg.type === "pong") {
        // Task 4/E3-01: proves the SOCKET is alive — deliberately NOT fed
        // into _lastTickTs, which must only ever reflect real price data.
        state._lastPongTs = Date.now();
        return;
      }

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
        updateWsBadge();
        return;
      }

      if (msg.type === "trade") {
        // Bind ticks to the ACTIVE symbol: a stale tick from the previous coin
        // must never be checked against the new position's SL (false alarm).
        if (msg.coin && !symMatch(msg.coin, state.symbol)) return;
        // E3-04: any actual price frame proves the feed is live — a transient
        // MEXC poll error (one "status":"error" frame) must not leave the
        // badge stuck once mids resume, without waiting for another "status".
        if (state.wsStatus !== "live") {
          state.wsStatus = "live";
          updateWsBadge();
        }
        applyLiveTrade(msg.px, msg.time);
        return;
      }
      if (msg.type === "mid") {
        if (msg.coin && !symMatch(msg.coin, state.symbol)) return;
        if (state.wsStatus !== "live") {
          state.wsStatus = "live";
          updateWsBadge();
        }
        applyLiveTrade(msg.px, msg.time || Date.now());
        return;
      }
      if (msg.type === "candle" && msg.bar) {
        // Same coin guard as trade/mid: a straggler candle from the previous
        // symbol during a switch must not feed the new coin's chart / SL-alarm /
        // uPnL with the wrong price (audit exchange H-1).
        if (msg.coin && !symMatch(msg.coin, state.symbol)) return;
        if (state.wsStatus !== "live") {
          state.wsStatus = "live";
          updateWsBadge();
        }
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
        scheduleWsReconnect();
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
      // A3-08: a real coin switch aborts an in-flight /api/analyze of the OLD
      // coin so a discarded LLM call stops wasting tokens (the market read is
      // superseded by this call's own apiFetchAbortable("market", …) below).
      // READ resource only — order/modify-sl/cancel never route through here.
      try { abortResource("analyze"); } catch (_) {}
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
        // U2-05: a stale proposal must not leave the OLD panel/Apply button
        // standing — renderProposal(null) tears down the rendered panel html,
        // the drift timer and the TP2/TP3 reminder in one place and disables
        // Apply, instead of only clearing the two state fields here (which
        // left the previous coin's HTML on screen with a live-looking button
        // that silently did nothing).
        try { renderProposal(null); } catch (_) { setApplyEnabled(false); }
      } else {
        try { drawProposalLines(); } catch (_) {}
      }
      try { drawTradeZones(); } catch (_) {}
      // Also drop the old coin's position/order price lines now — they re-filter
      // by symMatch but linger until the next fetch resolves (audit F3).
      try { drawPositionLines(); } catch (_) {}
      try { drawOrderLines(); } catch (_) {}
      // Clear the ticket's ABSOLUTE price fields: prices from the old coin are
      // meaningless for the new one and would corrupt the %-mode reference price
      // and the risk readout until overwritten (audit F2).
      ["ticket-entry", "ticket-price", "ticket-sl", "ticket-tp1", "ticket-tp2", "ticket-tp3"]
        .forEach(function (id) { const el = $(id); if (el) el.value = ""; });
      _clearTpLadderReminder();
      try { updateRiskReadout(); } catch (_) {}
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
      // A3-08: abortable READ — a newer market load (fast tf/coin switch)
      // aborts this one so a stale snapshot never lands.
      res = await apiFetchAbortable("market", url);
    } catch (err) {
      // Superseded by a newer load (abort) — silent, not a real error.
      if (err && err.name === "AbortError") return null;
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

    // A3-08: guard the parse — a truncated/aborted body must not throw an
    // unhandled rejection out of loadMarket.
    let data;
    try {
      data = await res.json();
    } catch (err) {
      if (err && err.name === "AbortError") return null;
      console.error("loadMarket parse error", err);
      if (!silent) setChartMeta(symbol, tf, htf, "parse error");
      return null;
    }
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
    updatePriceFieldSteps(); // T3-10: contract tick may have changed with the coin
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
      const prevLive = state.liveBar;
      if (
        silent && !keyChanged && prevLive &&
        Number.isFinite(prevLive.time) && Number.isFinite(t) && prevLive.time > t
      ) {
        // C3-12: on a silent poll the WS live bar can be NEWER than the last
        // REST candle (REST lags the tape by a few seconds). setData() just
        // snapped the chart back to that older REST close — re-apply the
        // in-progress live bar so the current candle doesn't flicker/vanish.
        try {
          if (state.candleSeries) state.candleSeries.update(prevLive);
        } catch (_) {
          /* chart empty — ignore */
        }
        if (data.last_price != null) setLivePrice(data.last_price);
      } else {
        state.liveBar = {
          time: t,
          open: last.open,
          high: last.high,
          low: last.low,
          close: last.close,
        };
        if (data.last_price != null) setLivePrice(data.last_price);
      }
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

  async function loadHealth() {
    try {
      const res = await apiFetch("/api/health");
      if (!res.ok) throw new Error("health " + res.status);
      const h = await res.json();
      state.health = h;

      const arm = $("arm-status");
      if (arm) {
        arm.classList.toggle("armed", h.trading_enabled === true);
        const t = arm.querySelector(".arm-text");
        if (t) t.textContent = h.trading_enabled ? "LIVE" : "DISARMED";
      }
      // W3-01: red MAINNET · ECHTGELD chip — visible only when armed AND on
      // mainnet (live_trading = trading_enabled && not testnet, from health).
      const mainnetChip = $("mainnet-chip");
      if (mainnetChip) mainnetChip.hidden = h.live_trading !== true;
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

      // Keep the sizing-suggestion button's label/tooltip in lockstep with
      // the backend's actual risk cap (U-01): never let the button promise
      // a % it doesn't deliver.
      const sugBtn = $("btn-suggest-vol");
      if (sugBtn) {
        const rp = maxRiskPct();
        sugBtn.textContent = fmt(rp, 2) + " % Risiko";
        sugBtn.setAttribute(
          "data-tip",
          "Berechnet die Größe so, dass der Stop-Loss genau " + fmt(rp, 2) +
            " % deines Equity riskiert (Stop-Loss vorher eintragen)."
        );
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
  /** Shared equity/uPnL/margin aggregation over open positions — the SINGLE
   *  source of truth for both the Konto side-panel (renderAccounts) and the
   *  overview Account-Puls bar (V3-01, renderAcctPulse). A second, divergent
   *  calc in the pulse would drift from the panel, so both read this. */
  function acctAggregate(acct) {
    const a = acct || {};
    const positions = (a.positions || []).filter(function (p) {
      return Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    let upnl = 0, haveUpnl = false, used = 0, haveUsed = false;
    positions.forEach(function (p) {
      const u = Number(p.unrealized_pnl);
      if (Number.isFinite(u)) { upnl += u; haveUpnl = true; }
      const im = Number(p.im != null ? p.im : p.margin);
      if (Number.isFinite(im)) { used += im; haveUsed = true; }
    });
    const eq = Number(a.equity_usdt);
    const free = Number(a.available_usdt);
    return {
      positions: positions,
      equity: Number.isFinite(eq) ? eq : null,
      free: Number.isFinite(free) ? free : null,
      upnl: haveUpnl ? upnl : null,
      used: haveUsed ? used : null,
    };
  }

  function renderAccounts(data) {
    const acct = data || state.account || {};
    const eqEl = $("acct-equity");
    if (!eqEl) return;
    const c = ccy();
    const agg = acctAggregate(acct);
    eqEl.textContent = agg.equity != null ? fmt(agg.equity, 2) + " " + c : "—";

    const upnlEl = $("acct-upnl");
    if (upnlEl) {
      if (agg.upnl != null) {
        upnlEl.textContent = (agg.upnl >= 0 ? "+" : "") + fmt(agg.upnl, 2) + " " + c;
        upnlEl.className =
          "acct-val " + (agg.upnl > 0 ? "pnl-pos" : agg.upnl < 0 ? "pnl-neg" : "");
      } else {
        upnlEl.textContent = "—";
        upnlEl.className = "acct-val";
      }
    }
    const freeEl = $("acct-free");
    if (freeEl) freeEl.textContent = agg.free != null ? fmt(agg.free, 2) + " " + c : "—";
    const usedEl = $("acct-used");
    if (usedEl) usedEl.textContent = agg.used != null ? fmt(agg.used, 2) + " " + c : "—";

    renderAcctPulse(acct, agg);
  }

  var DAY_EQUITY_KEY = PERSIST.DAY_EQUITY; // A3-02: routed via store.js PERSIST table

  /** Client-local "since first equity read today" baseline for the pulse's
   *  Tages-PnL. NOT the exchange's true realized daily PnL (a deposit or
   *  withdrawal would distort it) — there is no backend endpoint tracking
   *  historical equity, and adding one is out of scope for this display task
   *  (V3-01). Returns null (→ "—" in the UI) rather than fabricate a number
   *  when equity itself isn't known. */
  function dayPnl(equity) {
    if (!Number.isFinite(equity)) return null;
    // LOKALES Kalenderdatum (nicht UTC): der Tages-Baseline soll um die
    // Mitternacht des Nutzers zuruecksetzen, nicht um UTC-Mitternacht.
    const d = new Date();
    const today =
      d.getFullYear() +
      "-" +
      String(d.getMonth() + 1).padStart(2, "0") +
      "-" +
      String(d.getDate()).padStart(2, "0");
    try {
      const raw = localStorage.getItem(DAY_EQUITY_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (parsed && parsed.date === today && Number.isFinite(parsed.equity)) {
        return equity - parsed.equity;
      }
      localStorage.setItem(DAY_EQUITY_KEY, JSON.stringify({ date: today, equity: equity }));
      return 0;
    } catch (_) {
      return null;
    }
  }

  /** Best-effort per-position risk in USDT: |entry − known SL| × volume ×
   *  contract size. Requires a known SL (synced open-order stop or a manual
   *  tradeMarkers entry) — returns null rather than guess when it isn't
   *  known yet. Shared by the Account-Puls Σ-Risiko tile (V3-01) and the
   *  overview grid's risk-first position sort (V3-06) so both read the same
   *  number instead of two calcs quietly drifting apart. */
  function positionRiskUsdt(p) {
    if (!p) return null;
    const mk = state.tradeMarkers && state.tradeMarkers[markerKey(p.symbol)];
    const sl = mk && Number(mk.sl);
    const entry = Number(p.entry_price);
    const vol = Number(p.hold_vol);
    if (!Number.isFinite(sl) || !sl || !Number.isFinite(entry) || !Number.isFinite(vol) || !vol) {
      return null;
    }
    const pcs = positionContractSize(p.contract_size, contractSize());
    return Math.abs(entry - sl) * Math.abs(vol) * pcs;
  }

  /** V3-01: Account-Puls — Equity · Tages-PnL · Σ uPnL · Σ Risiko · Margin-%,
   *  the FIRST thing the overview shows (money before news). Degrades to "—"
   *  per field when the underlying data isn't available, never NaN. */
  function renderAcctPulse(acct, agg) {
    const a = agg || acctAggregate(acct);
    const c = ccy();

    const eqEl = $("pulse-equity");
    if (eqEl) eqEl.textContent = a.equity != null ? fmt(a.equity, 2) + " " + c : "—";

    const dpEl = $("pulse-day-pnl");
    if (dpEl) {
      const dp = a.equity != null ? dayPnl(a.equity) : null;
      // Ehrlichkeit (V3-01): das ist KEIN echtes realisiertes Tages-PnL, sondern
      // eine lokale Naeherung (Equity jetzt minus erster Kontostand-Abruf heute).
      // Ein-/Auszahlungen verzerren sie. Daher "≈"-Praefix + Tooltip, damit die
      // Zahl nicht wie eine belastbare Boersen-Groesse gelesen wird.
      dpEl.title =
        "Naeherung: Equity jetzt minus erstem Kontostand-Abruf heute. " +
        "KEIN echtes realisiertes PnL — Ein-/Auszahlungen verzerren den Wert.";
      if (dp != null) {
        dpEl.textContent = "≈ " + (dp >= 0 ? "+" : "") + fmt(dp, 2) + " " + c;
        dpEl.className = "pulse-val " + (dp > 0 ? "pnl-pos" : dp < 0 ? "pnl-neg" : "");
      } else {
        dpEl.textContent = "—";
        dpEl.className = "pulse-val";
      }
    }

    const upEl = $("pulse-upnl");
    if (upEl) {
      if (a.upnl != null) {
        upEl.textContent = (a.upnl >= 0 ? "+" : "") + fmt(a.upnl, 2) + " " + c;
        upEl.className = "pulse-val " + (a.upnl > 0 ? "pnl-pos" : a.upnl < 0 ? "pnl-neg" : "");
      } else {
        upEl.textContent = "—";
        upEl.className = "pulse-val";
      }
    }

    const riskEl = $("pulse-risk");
    if (riskEl) {
      let risk = 0, haveRisk = false;
      (a.positions || []).forEach(function (p) {
        const r = positionRiskUsdt(p);
        if (r != null) { risk += r; haveRisk = true; }
      });
      riskEl.textContent = haveRisk ? fmt(risk, 2) + " " + c : "—";
    }

    const mgEl = $("pulse-margin");
    if (mgEl) {
      mgEl.textContent =
        a.used != null && a.equity != null && a.equity > 0
          ? fmt((a.used / a.equity) * 100, 1) + "%"
          : "—";
    }
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
    // T41b: same side+entry geometry the shared classifier uses (loss side of
    // entry = SL). A reduce-only price is a bare limit (no field/label), so
    // classifyTriggers falls straight through to geometry.
    return classifyTriggers({ price: price }, pos.side, entry).tp != null ? "TP" : "SL";
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
    // Escape both — latent XSS sink hardening (F5). All current callers pass
    // static labels + fmt() numbers, so escaping is a no-op today but closes
    // the hole if this is ever fed exchange/feed data.
    return (
      '<span class="pos-kv"><b>' +
      escapeHtml(String(label)) +
      "</b><span" +
      (cls ? ' class="' + cls + '"' : "") +
      ">" +
      escapeHtml(String(value)) +
      "</span></span>"
    );
  }

  function _posDataAttrs(p, sideVal, posCs) {
    return (
      ' data-sym="' + escapeHtml(String(p.symbol || "")) + '"' +
      ' data-entry="' + escapeHtml(String(p.entry_price != null ? p.entry_price : "")) + '"' +
      ' data-vol="' + escapeHtml(String(p.hold_vol != null ? p.hold_vol : "")) + '"' +
      ' data-cs="' + escapeHtml(String(posCs)) + '"' +
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
    const short = String(p.side || "").toLowerCase() === "short";
    // T41b: SL/TP classification is now the ONE frontend source classifyTriggers
    // (trade-math.js), mirroring app/orders/protection.py — explicit field
    // first, then orderType label, then side+entry geometry. It keeps this
    // module's break-even nuance (a trigger within ~0.1% of entry is a
    // break-even STOP, F-12b — a real BE stop must not read as unprotected)
    // and, like the backend, never fabricates an SL for an unresolvable
    // trigger. The prior inline label rule (`indexOf("tp") === 0`) mis-read
    // MEXC's combined "tpsl" (a stop) as a take-profit; the shared classifier
    // fixes that, so a tpsl-protected position no longer reads "no stop-loss".
    stops.forEach(function (s) {
      if (s.symbol && !symMatch(s.symbol, p.symbol)) return;
      const c = classifyTriggers(s, short ? "short" : "long", entry);
      if (c.sl != null) sl = c.sl;
      if (c.tp != null) tp = c.tp;
    });
    const mk = state.tradeMarkers && state.tradeMarkers[markerKey(p.symbol)];
    let manual = false;
    if (mk) {
      if (sl == null && mk.sl) { sl = mk.sl; manual = !!mk.manual; }
      if (tp == null && mk.tp) tp = mk.tp;
    }
    return { sl: sl, tp: tp, ordersKnown: ordersKnown, manual: manual };
  }

  /** N3-02: TP suffix for the SL banner — "· TP 0.026 (+9.8%)" when a
   *  take-profit is known, a neutral (non-alarming) "kein TP" when orders ARE
   *  loaded and none is set, or "TP-Status unbekannt" while orders haven't
   *  loaded yet — mirrors the SL banner's ordersKnown gate so an unloaded
   *  order list never reads as a confirmed "no TP" (same honesty rule as the
   *  SL "wird geladen" state). */
  function _tpSuffix(prot, entry) {
    if (prot.tp != null && Number.isFinite(entry) && entry > 0) {
      const pct = ((prot.tp - entry) / entry) * 100;
      return (
        ' · <span class="cp-tp-status cp-tp-set">TP ' + fmt(prot.tp, 4) +
        " (" + (pct >= 0 ? "+" : "") + fmt(pct, 2) + "%)</span>"
      );
    }
    if (!prot.ordersKnown) {
      return ' · <span class="cp-tp-status cp-tp-unknown">TP-Status unbekannt</span>';
    }
    return ' · <span class="cp-tp-status cp-tp-none">kein TP</span>';
  }

  /** SL-status banner HTML for a position — green when protected, loud red when
   *  genuinely unprotected. This is the single most important safety nudge.
   *  N3-02: extended with the TP suffix so the same banner also answers "do I
   *  have a target?", not just "am I protected downside?". */
  function slStatusBanner(p) {
    const prot = findPositionProtection(p);
    const entry = Number(p.entry_price);
    const tp = _tpSuffix(prot, entry);
    if (prot.manual && prot.sl != null && Number.isFinite(entry) && entry > 0) {
      const pct = ((prot.sl - entry) / entry) * 100;
      return (
        '<div class="cp-sl-status cp-sl-manual">SL: MANUELL ' + fmt(prot.sl, 4) +
        " (" + (pct >= 0 ? "+" : "") + fmt(pct, 2) + "%) — nur bei offenem Browser" + tp + "</div>"
      );
    }
    if (prot.sl != null && Number.isFinite(entry) && entry > 0) {
      const pct = ((prot.sl - entry) / entry) * 100;
      return (
        '<div class="cp-sl-status cp-sl-ok">🛡 Stop-Loss ' + fmt(prot.sl, 4) +
        " (" + (pct >= 0 ? "+" : "") + fmt(pct, 2) + "%)" + tp + "</div>"
      );
    }
    if (!prot.ordersKnown) {
      return '<div class="cp-sl-status cp-sl-unknown">Stop-Loss-Status wird geladen…' + tp + "</div>";
    }
    return (
      '<div class="cp-sl-status cp-sl-missing">⚠ KEIN STOP-LOSS AKTIV — Position ungeschützt' +
      tp + "</div>"
    );
  }

  /* ── Instrument rail (signature) ─────────────────────────────────────
     Keeps the critical read — armed state, exchange, equity, and the active
     position's live P&L / protection / liq distance — always visible above
     the workspace, instead of scattered across header + panels. */
  function renderInstrumentRail() {
    const rail = $("instrument-rail");
    if (!rail) return;
    // Compact the layout while a position is open in the active symbol — the
    // panels tighten up so the trade + chart need less scrolling (user request).
    try { document.body.classList.toggle("has-position", hasActivePosition()); } catch (_) {}
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
    // F-10: use this position's own contract_size when present.
    const cs = positionContractSize(active.contract_size, contractSize());
    const im = Number(active.margin != null ? active.margin : active.im);
    const px = Number(state.lastPx);
    let pnl = active.unrealized_pnl != null ? Number(active.unrealized_pnl) : null;
    if (Number.isFinite(px) && Number.isFinite(entry) && Number.isFinite(vol)) {
      pnl = computePnl(px, entry, vol, cs, short);
    }
    const roe = computeRoe(pnl, im);
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
    const cs = positionContractSize(active.contract_size, contractSize());
    const pnl = computePnl(px, entry, vol, cs, short);
    const roe = computeRoe(pnl, im);
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

  /** Tiny djb2 string hash → short base36 token. Used to fold a position
   *  card's structural signature (which may contain HTML/quotes) into a safe
   *  `data-fp` attribute for the keyed-patch diff. */
  function _hashStr(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }

  /** N3-07/A3-03: ONE delegated click listener on #positions-body. This is the
   *  REAL event delegation the old per-card rebind only pretended to be — it
   *  survives keyed re-renders because it lives on the panel, not the cards, so
   *  a Close/BE click can never be lost to a mid-render DOM swap of its button.
   *  Every action the old per-card listeners fired is dispatched here by
   *  data-action (money actions) or class (KI-switch), with the bare card click
   *  as the click-to-chart fallback. Dropping any branch = a dead button. */
  function onPositionsBodyClick(e) {
    const t = e.target;
    const actionEl = t.closest && t.closest("[data-action]");
    if (actionEl) {
      const a = actionEl.getAttribute("data-action");
      if (a === "close-frac") {
        // Partial-close: closes data-frac of the CURRENT hold (server rounds to lot).
        e.stopPropagation();
        const box = actionEl.closest(".cp-close");
        if (box) {
          closePositionFrac(
            box.getAttribute("data-sym"),
            box.getAttribute("data-side"),
            Number(actionEl.getAttribute("data-frac"))
          );
        }
        return;
      }
      if (a === "be") {
        // SL → Break-Even: real money action, moves/places the stop at BE.
        e.stopPropagation();
        const box = actionEl.closest(".cp-actions");
        if (box) {
          moveStopToBreakEven(
            box.getAttribute("data-sym"),
            box.getAttribute("data-side"),
            Number(box.getAttribute("data-be"))
          );
        }
        return;
      }
      if (a === "sl-edit") {
        // N3-09: reveal/hide the inline SL-price editor. Pure UI toggle, no
        // network — the actual move is gated behind data-action="sl-set".
        e.stopPropagation();
        const box = actionEl.closest(".cp-actions");
        const ed = box && box.querySelector(".cp-sl-edit");
        if (ed) {
          const nowHidden = ed.classList.toggle("hidden");
          const input = box.querySelector(".cp-sl-input");
          if (!nowHidden && input) input.focus();
        }
        return;
      }
      if (a === "sl-edit-cancel") {
        // Just closes the editor — never touches an order.
        e.stopPropagation();
        const box = actionEl.closest(".cp-actions");
        const ed = box && box.querySelector(".cp-sl-edit");
        if (ed) ed.classList.add("hidden");
        return;
      }
      if (a === "sl-set") {
        // N3-09: send the typed SL through the SAME cancel+replace path as BE.
        // moveStopTo() runs its own window.confirm, so this is NEVER a silent
        // send. A client-side wrong-side guard rejects an obviously invalid
        // stop (long above / short below entry) BEFORE any network call — the
        // server still does the authoritative geometry validation.
        e.stopPropagation();
        const box = actionEl.closest(".cp-actions");
        if (!box) return;
        const input = box.querySelector(".cp-sl-input");
        const sym = box.getAttribute("data-sym");
        const side = box.getAttribute("data-side");
        const entry = Number(box.getAttribute("data-entry"));
        const px = Number(input && input.value);
        if (!Number.isFinite(px) || px <= 0) {
          showToast("Bitte einen gültigen SL-Preis eingeben.", "err");
          return;
        }
        if (Number.isFinite(entry) && entry > 0) {
          const isLong = String(side).toLowerCase() !== "short";
          if (isLong && px >= entry) {
            showToast(
              "SL für LONG muss unter dem Entry (" + fmt(entry, 6) + ") liegen.",
              "err"
            );
            return;
          }
          if (!isLong && px <= entry) {
            showToast(
              "SL für SHORT muss über dem Entry (" + fmt(entry, 6) + ") liegen.",
              "err"
            );
            return;
          }
        }
        moveStopTo(sym, side, px);
        return;
      }
      if (a === "reeval") {
        // "KI: Position bewerten" — advisory only, never trades.
        e.stopPropagation();
        runReevaluate(actionEl.getAttribute("data-sym"));
        return;
      }
    }
    // A cached reevaluate result may be a categorized "⚠" provider error whose
    // "KI wechseln" button focuses the provider dropdown (shared warn-banner
    // markup, so matched by class, not data-action).
    if (t.closest && t.closest(".btn-ki-switch")) {
      focusLlmSelect();
      return;
    }
    // K4: bare card click opens that coin's chart; ignore clicks that landed on
    // an interactive element (buttons above already returned).
    if (t.closest && t.closest("button, a, input, select, textarea")) return;
    const card = t.closest && t.closest(".pos-cockpit");
    if (card) {
      const sym = card.getAttribute("data-sym");
      if (sym) goToSymbol(sym);
    }
  }

  /** N3-09: Enter inside the inline SL field submits it — dispatched through
   *  the SAME data-action="sl-set" path (and thus the same window.confirm) as
   *  the "Setzen" button, so there is no confirm-free shortcut. */
  function onPositionsBodyKeydown(e) {
    if (e.key !== "Enter") return;
    const input = e.target;
    if (!input || !input.classList || !input.classList.contains("cp-sl-input")) return;
    e.preventDefault();
    const box = input.closest(".cp-actions");
    const setBtn = box && box.querySelector(".cp-sl-set-btn");
    if (setBtn) setBtn.click();
  }

  /** N3-01: best-effort MARK price for a position — honest degrade, never
   *  fabricated. The active chart symbol has a live streamed price
   *  (state.lastPx); any other open position only has a mark once the
   *  overview mini-tiles have fetched it (state.overviewData, populated only
   *  while the overview view is active) — otherwise this returns null and the
   *  card shows "—" rather than guessing. */
  function _positionMarkPrice(p) {
    const active = symMatch(p.symbol, state.symbol);
    const live = Number(state.lastPx);
    // E3-06: for the active symbol the live tick is corrected toward the
    // exchange MARK (same offset _updateLivePnl applies) so the Mark cell,
    // price-%, liq-% and the streamed uPnL all agree — no 30s jump.
    if (active && Number.isFinite(live) && live > 0) return live + _markOffsetFor(p.symbol);
    const d = state.overviewData && state.overviewData[String(p.symbol || "").toUpperCase()];
    const last = d && Number(d.last_price);
    return Number.isFinite(last) && last > 0 ? last : null;
  }

  /** N3-01: combined ROE + raw price-% sub-line under the big uPnL number.
   *  ROE is leverage-scaled (misleading for SL decisions at high leverage);
   *  the price-% is the actual, unleveraged distance entry→mark in the
   *  position's favor, so the two together show both "how much" and "how far
   *  price actually moved". Either half degrades to omitted (not "—") when
   *  unknown, so a card with no mark yet just shows plain ROE. */
  function _pnlSubText(roe, pricePct) {
    const roeTxt = roe != null ? (roe >= 0 ? "+" : "") + fmt(roe, 1) + "% ROE" : "";
    const pxTxt = pricePct != null ? (pricePct >= 0 ? "+" : "") + fmt(pricePct, 1) + "% Px" : "";
    if (roeTxt && pxTxt) return roeTxt + " · " + pxTxt;
    return roeTxt || pxTxt;
  }

  /** Build the render model for one position: the full card HTML plus the
   *  structural fingerprint (everything the card shows EXCEPT the live
   *  uPnL/ROE/mark/liq-distance numbers, which are text-patched in place so
   *  the card never freezes and never needs a full rebuild just because price
   *  drifted). */
  function _positionModel(p, cs) {
    const pnl = Number(p.unrealized_pnl);
    const pnlCls = Number.isFinite(pnl) && pnl !== 0 ? (pnl > 0 ? "pnl-pos" : "pnl-neg") : "";
    const im = Number(p.im);
    const roe = computeRoe(pnl, im);
    const sideVal = String(p.side || "").toLowerCase() === "short" ? "short" : "long";
    const isActive = symMatch(p.symbol, state.symbol);
    // `cs` is the ACTIVE chart symbol's contractSize — correct only for it.
    // MEXC coins can have different contract sizes, so reusing it for every
    // other open position's notional would be wrong (F-10). For any other
    // symbol, derive notional from exchange-reported margin × leverage instead.
    const posLev = Number(p.leverage);
    const posIm = Number(p.im);
    // F-10: prefer the position's OWN contract_size; fall back to the old
    // proxies only when it's absent (older /api/account payload).
    const posCsRaw = Number(p.contract_size);
    const hasPosCs = Number.isFinite(posCsRaw) && posCsRaw > 0;
    const entryPx = Number(p.entry_price || 0);
    const notional =
      hasPosCs && Number.isFinite(entryPx) && entryPx > 0
        ? Number(p.hold_vol) * posCsRaw * entryPx
        : isActive
          ? Number(p.hold_vol) * cs * entryPx
          : Number.isFinite(posIm) && posIm > 0 && Number.isFinite(posLev) && posLev > 0
            ? posIm * posLev
            : null;
    const posCs = positionContractSize(p.contract_size, cs);
    // N3-01: mark price + liq-distance-% + raw price-%, all volatile-per-tick
    // (patched in place, see globalFp below — never gate the structural fp on
    // these or the card would skip re-render on a pure price move).
    const mark = _positionMarkPrice(p);
    const liq = Number(p.liquidate_price);
    const hasLiq = Number.isFinite(liq) && liq > 0;
    // Signed distance (liq vs mark): negative when liq sits below mark (the
    // common LONG case), positive when above (the common SHORT case) — same
    // "+/- from reference" convention as the SL/TP % elsewhere on this card,
    // not a side-normalized magnitude.
    const liqPct = hasLiq && mark != null && mark > 0 ? ((liq - mark) / mark) * 100 : null;
    const liqAbsPct = liqPct != null ? Math.abs(liqPct) : null;
    const liqCls = liqAbsPct == null ? "" : liqAbsPct < 5 ? "cp-liq-danger" : liqAbsPct < 10 ? "cp-liq-warn" : "";
    const liqCellText =
      fmt(p.liquidate_price, 4) +
      (liqPct != null ? " (" + (liqPct >= 0 ? "+" : "") + fmt(liqPct, 1) + "%)" : "");
    // Raw, UNLEVERAGED price move entry→mark, signed so it's positive when
    // favorable (same sign convention as ROE/uPnL) — this is the number that
    // matters for an SL decision, unlike ROE which the leverage inflates.
    // Reuses `entryPx` (already computed above for the notional calc).
    const pricePct =
      mark != null && entryPx > 0
        ? ((mark - entryPx) / entryPx) * (sideVal === "short" ? -1 : 1) * 100
        : null;
    // N3-04: "no SL" sort key. A second findPositionProtection() call (the
    // first lives inside slStatusBanner below) — cheap (loops the already-
    // fetched open-orders array), and keeping the sort key independent of the
    // banner HTML avoids coupling the two concerns.
    const hasSl = findPositionProtection(p).sl != null;
    // Break-even stop incl. ~round-trip taker fees (0.06% total).
    const bePrice = breakEvenPrice(p.entry_price, sideVal === "short");
    // Computed once, reused for BOTH the HTML and the fingerprint so the SL
    // banner / reeval block can't drift between what's shown and what's hashed.
    const slHtml = slStatusBanner(p);
    const reevalHtml = reevalResultHtml(p.symbol);
    // N3-09: the actions row always carries the inline SL-editor (✎ SL) so a
    // stop can be dragged to ANY price from the card; the BE button rides
    // along only when a break-even price is computable. data-be is included
    // only when present (the fp above still folds bePrice in either way).
    const beBtn =
      bePrice != null
        ? '<button type="button" class="cp-be-btn" data-action="be" title="Stop-Loss auf Break-Even (inkl. Gebühren) setzen — ersetzt einen bestehenden Stop">SL → Break-Even</button>'
        : "";
    const beRow =
      '<div class="cp-actions"' + _posDataAttrs(p, sideVal, posCs) +
      (bePrice != null ? ' data-be="' + escapeHtml(String(bePrice)) + '"' : "") + ">" +
      '<span class="cp-actions-label">Stop</span>' +
      beBtn +
      '<button type="button" class="cp-sl-edit-btn" data-action="sl-edit" title="Stop-Loss auf einen beliebigen Preis nachziehen">✎ SL</button>' +
      '<span class="cp-sl-edit hidden">' +
      '<input type="number" class="cp-sl-input" step="any" inputmode="decimal" placeholder="SL-Preis" aria-label="Neuer Stop-Loss-Preis" />' +
      '<button type="button" class="cp-sl-set-btn" data-action="sl-set">Setzen</button>' +
      '<button type="button" class="cp-sl-cancel-btn" data-action="sl-edit-cancel" title="Abbrechen" aria-label="Abbrechen">✕</button>' +
      "</span>" +
      "</div>";
    // Structural fingerprint: everything the user needs EXCEPT the live pnl/roe
    // text (patched in place). SL status + reeval block are included so a
    // protection change or a fresh KI verdict DOES rebuild the card.
    const fp = _hashStr(
      [
        String(p.symbol || ""), sideVal, String(p.leverage),
        fmt(p.entry_price, 6), fmt(p.hold_vol, 6),
        p.im != null ? fmt(p.im, 4) : "-",
        fmt(p.liquidate_price, 6),
        String(posCs), notional != null ? fmt(notional, 0) : "-",
        bePrice != null ? String(bePrice) : "-",
        slHtml, reevalHtml, isActive ? "A" : "-",
      ].join("")
    );
    const html =
      '<div class="pos-cockpit ' + (sideVal === "short" ? "cp-short" : "cp-long") +
      (isActive ? " cp-active" : "") + '" data-fp="' + fp + '"' +
      _posDataAttrs(p, sideVal, posCs) + ">" +
      '<div class="cp-head">' +
      sideTag(p.side) +
      '<span class="cp-sym">' + escapeHtml(p.symbol || "—") + "</span>" +
      '<span class="cp-lev">' + escapeHtml(String(p.leverage != null ? p.leverage : "—")) + "×</span>" +
      // N3-06: the active card streams live via the WS; every other card is only
      // as fresh as the 30s account poll — a subtle "·30s" so a foreign coin that
      // is actually up to 30s old never masquerades as live. Static literal (no
      // per-tick timestamp) so it stays in the STRUCTURAL fp — which already
      // folds isActive — and can't churn the volatile fp or flicker the patcher.
      (isActive
        ? ""
        : '<span class="cp-fresh" title="Nicht live — Momentaufnahme, bis zu 30 s alt (Konto-Poll)">·30s</span>') +
      "</div>" +
      slHtml +
      '<div class="cp-pnl js-upnl-big ' + pnlCls + '">' +
      (pnl >= 0 ? "+" : "") + fmt(p.unrealized_pnl, 2) + " " + ccy() +
      '<span class="cp-pnl-sub js-roe-big ' + pnlCls + '">' +
      _pnlSubText(roe, pricePct) + "</span>" +
      "</div>" +
      '<div class="cp-grid">' +
      _cpCell("Entry", fmt(p.entry_price, 4)) +
      _cpCell("Mark", mark != null ? fmt(mark, 4) : "—", "", "js-mark-val") +
      _cpCell(
        "Größe",
        fmt(p.hold_vol, 4) + (notional != null ? " · " + fmt(notional, 0) + " " + ccy() : "")
      ) +
      _cpCell("Liq", liqCellText, "cp-liq" + (liqCls ? " " + liqCls : ""), "js-liq-val") +
      _cpCell("Margin", p.im != null ? fmt(p.im, 2) + " " + ccy() : "—") +
      "</div>" +
      beRow +
      '<div class="cp-close" ' + _posDataAttrs(p, sideVal, posCs) + ">" +
      '<span class="cp-close-label">Schließen</span>' +
      '<button type="button" class="cp-close-btn" data-action="close-frac" data-frac="0.25">25%</button>' +
      '<button type="button" class="cp-close-btn" data-action="close-frac" data-frac="0.5">50%</button>' +
      '<button type="button" class="cp-close-btn" data-action="close-frac" data-frac="0.75">75%</button>' +
      '<button type="button" class="cp-close-btn cp-close-full" data-action="close-frac" data-frac="1">100%</button>' +
      "</div>" +
      '<div class="cp-reeval">' +
      '<button type="button" class="cp-reeval-btn" data-action="reeval" data-sym="' +
      escapeHtml(String(p.symbol || "")) + '">KI: Position bewerten</button>' +
      '<div class="cp-reeval-result" data-sym-result="' +
      escapeHtml(String(p.symbol || "").toUpperCase()) + '">' +
      reevalHtml +
      "</div>" +
      "</div>" +
      "</div>";
    return {
      key: String(p.symbol || "") + "|" + sideVal,
      sym: String(p.symbol || ""),
      html: html,
      fp: fp,
      rawPnl: p.unrealized_pnl,
      pnl: pnl,
      pnlCls: pnlCls,
      roe: roe,
      // N3-01 volatile fields — patched in place, also folded into globalFp.
      mark: mark,
      pricePct: pricePct,
      liqPct: liqPct,
      liqCellText: liqCellText,
      liqCls: liqCls,
      // N3-03 (Σ header) + N3-04 (sort) inputs.
      notional: notional,
      hasSl: hasSl,
      liqAbsPct: liqAbsPct,
      isActive: isActive,
    };
  }

  /** In-place patch of a card's live uPnL/ROE/mark/liq-distance (no DOM
   *  rebuild → buttons stay clickable). Mirrors the per-tick _updateLivePnl
   *  writer so the poll value and the streaming value use the same text/class
   *  shape. N3-01: also patches the Mark cell and the Liq cell's distance-%
   *  text + risk class, since both move every tick the same as uPnL/ROE. */
  function _patchCardLive(card, m) {
    const big = card.querySelector(".js-upnl-big");
    if (big) {
      big.className = "cp-pnl js-upnl-big " + m.pnlCls;
      if (big.firstChild) {
        big.firstChild.nodeValue = (m.pnl >= 0 ? "+" : "") + fmt(m.rawPnl, 2) + " " + ccy();
      }
    }
    const sub = card.querySelector(".js-roe-big");
    if (sub) {
      sub.className = "cp-pnl-sub js-roe-big " + m.pnlCls;
      sub.textContent = _pnlSubText(m.roe, m.pricePct);
    }
    const markEl = card.querySelector(".js-mark-val");
    if (markEl) markEl.textContent = m.mark != null ? fmt(m.mark, 4) : "—";
    const liqEl = card.querySelector(".js-liq-val");
    if (liqEl) liqEl.textContent = m.liqCellText;
    const liqCell = card.querySelector(".cp-liq");
    if (liqCell) liqCell.className = "cp-cell cp-liq" + (m.liqCls ? " " + m.liqCls : "");
  }

  function _buildCardEl(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return tmp.firstElementChild;
  }

  /** N3-03: Σ-header above the position cards — Σ uPnL, Σ Notional (+ ×Equity),
   *  Σ Margin/Equity. Reads acctAggregate (T33, the SAME aggregation the
   *  Account-Puls/Konto-panel use) for uPnL/used/equity so this can't drift
   *  from those; Σ Notional sums each model's own per-position `notional`
   *  (already F-10-correct per-symbol contract size). Hidden entirely when
   *  there are no open positions. */
  function _renderPositionsSummary(models, data) {
    const el = $("positions-summary");
    if (!el) return;
    if (!models || !models.length) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    const agg = acctAggregate(data || state.account);
    const c = ccy();
    let notional = 0, haveNotional = false;
    models.forEach(function (m) {
      if (m.notional != null) { notional += m.notional; haveNotional = true; }
    });
    const notionalTxt = haveNotional
      ? fmt(notional, 0) + " " + c +
        (agg.equity != null && agg.equity > 0 ? " (" + fmt(notional / agg.equity, 1) + "×)" : "")
      : "—";
    const marginTxt =
      agg.used != null && agg.equity != null && agg.equity > 0
        ? fmt((agg.used / agg.equity) * 100, 1) + "%"
        : "—";
    const upnlCls = agg.upnl == null ? "" : agg.upnl > 0 ? "pnl-pos" : agg.upnl < 0 ? "pnl-neg" : "";
    const upnlTxt = agg.upnl != null ? (agg.upnl >= 0 ? "+" : "") + fmt(agg.upnl, 2) + " " + c : "—";
    el.classList.remove("hidden");
    el.innerHTML =
      posKv("Σ uPnL", upnlTxt, "pos-sum-val " + upnlCls) +
      posKv("Σ Notional", notionalTxt, "pos-sum-val") +
      posKv("Σ Margin/Equity", marginTxt, "pos-sum-val");
  }

  function renderPositions(data) {
    const el = $("positions-body");
    if (!el) return;
    // Wire the ONE delegated listener a single time; it lives on the panel and
    // survives every keyed re-render below.
    if (!state._positionsWired) {
      el.addEventListener("click", onPositionsBodyClick);
      el.addEventListener("keydown", onPositionsBodyKeydown);
      state._positionsWired = true;
    }
    const positions = (data && data.positions) || [];
    const open = positions.filter(function (p) {
      return Math.abs(Number(p.hold_vol) || 0) > 0;
    });
    if (!open.length) {
      _renderPositionsSummary([]);
      const msg = data && data.error ? String(data.error) : "Keine offenen Positionen.";
      const emptyFp = "EMPTY" + msg;
      if (state._positionsFp === emptyFp) return;
      state._positionsFp = emptyFp;
      el.className = "positions-body muted";
      el.textContent = msg;
      return;
    }

    const cs =
      (state.market && state.market.contract && state.market.contract.contractSize) || 1;

    // ALL open positions render as full cockpit cards, account-wide — the
    // active symbol's card is highlighted (cp-active) but every coin is
    // equally full/clickable. N3-04: only the ACTIVE group's relative order
    // is left as-is (API order); the rest is sorted deterministically —
    // unprotected (no SL) first, then closest-to-liquidation first, with
    // symbol as a stable tiebreaker — so cards don't reshuffle between polls
    // on ties.
    const allModels = open.map(function (p) {
      return _positionModel(p, cs);
    });
    const activeModels = allModels.filter(function (m) {
      return m.isActive;
    });
    const otherModels = allModels
      .filter(function (m) {
        return !m.isActive;
      })
      .sort(function (a, b) {
        const aNoSl = a.hasSl ? 1 : 0;
        const bNoSl = b.hasSl ? 1 : 0;
        if (aNoSl !== bNoSl) return aNoSl - bNoSl; // no-SL (0) before protected (1)
        const aDist = a.liqAbsPct != null ? a.liqAbsPct : Infinity;
        const bDist = b.liqAbsPct != null ? b.liqAbsPct : Infinity;
        if (aDist !== bDist) return aDist - bDist; // closest to liq first
        return a.sym.localeCompare(b.sym); // stable tiebreaker
      });
    const models = activeModels.concat(otherModels);

    // N3-03: Σ header (uPnL / Notional / Margin-Auslastung) — reuses
    // acctAggregate (T33) so it can never drift from the Account-Puls, and is
    // NOT gated on the globalFp skip below: equity can change (funding,
    // another symbol's fill) without any card's structure/live fields
    // changing, and the header must still stay current.
    _renderPositionsSummary(models, data);

    // Render-fingerprint guard: fold every card's structure + live pnl/roe into
    // one string. If it matches the last render AND the panel already shows
    // cards, there is literally nothing to do — skip ALL DOM work (this is what
    // keeps the money-buttons rock-stable between polls).
    // N3-01: mark/liq-distance/price-% are volatile-per-tick, same as
    // rawPnl/roe — folded into globalFp so a pure price move (no structural
    // change) is never skipped by the "nothing to do" guard below.
    const globalFp = models
      .map(function (m) {
        return (
          m.key + "#" + m.fp + "#" + fmt(m.rawPnl, 2) +
          "#" + (m.roe != null ? fmt(m.roe, 1) : "-") +
          "#" + (m.mark != null ? fmt(m.mark, 6) : "-") +
          "#" + (m.liqPct != null ? fmt(m.liqPct, 2) : "-") +
          "#" + (m.pricePct != null ? fmt(m.pricePct, 2) : "-")
        );
      })
      .join("|");
    const populated = !!el.querySelector(".pos-cockpit");
    if (populated && state._positionsFp === globalFp) return;
    state._positionsFp = globalFp;

    el.className = "positions-body";
    // Coming from the muted/empty state → no cards to reuse; clear the text.
    if (!populated) el.innerHTML = "";

    // Keyed patch: index the cards currently in the DOM by symbol|side.
    const existing = {};
    Array.prototype.forEach.call(el.querySelectorAll(".pos-cockpit"), function (c) {
      existing[c.getAttribute("data-sym") + "|" + (c.getAttribute("data-side") || "")] = c;
    });

    const cardFor = {};
    models.forEach(function (m) {
      let card = existing[m.key];
      if (card && card.getAttribute("data-fp") === m.fp) {
        // Structure unchanged → keep the node (and its buttons); only the live
        // uPnL/ROE text may have moved.
        _patchCardLive(card, m);
      } else if (card) {
        // Structure changed (SL status, size after a partial close, fresh KI
        // verdict, active-symbol switch …) → rebuild just this card in place.
        const fresh = _buildCardEl(m.html);
        card.parentNode.replaceChild(fresh, card);
        card = fresh;
      } else {
        // New position → create; temp-append, reordered below.
        card = _buildCardEl(m.html);
        el.appendChild(card);
      }
      cardFor[m.key] = card;
    });

    // Remove cards whose position closed (no longer in the model set) so a
    // stale card never lingers.
    Object.keys(existing).forEach(function (k) {
      if (!cardFor[k]) existing[k].remove();
    });

    // Enforce the desired order (active first) without destroying nodes.
    models.forEach(function (m, i) {
      const card = cardFor[m.key];
      const at = el.children[i];
      if (at !== card) el.insertBefore(card, at || null);
    });
  }

  function _cpCell(label, value, cls, valCls) {
    return (
      '<div class="cp-cell ' + (cls || "") + '">' +
      '<span class="cp-cell-label">' + escapeHtml(label) + "</span>" +
      '<span class="cp-cell-val' + (valCls ? " " + valCls : "") + '">' +
      escapeHtml(String(value)) + "</span></div>"
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
      const text = String(entry.error);
      if (isLlmWarnMessage(text)) {
        // Categorized provider error (app/llm/client.py) — same clean
        // warn-banner + "KI wechseln" affordance as showProposalError()
        // instead of a plain error line. The caller is responsible for
        // calling wireKiSwitchButtons() once this is attached to the DOM.
        return '<div class="cp-reeval-out cp-reeval-error-warn">' + llmWarnBannerHtml(text) + "</div>";
      }
      return '<div class="cp-reeval-out cp-reeval-error">' + escapeHtml(text) + "</div>";
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
      if (out2) {
        // The card's "KI wechseln" button (if this result is a provider error)
        // is handled by the #positions-body delegated listener — no per-element
        // wiring needed here anymore (T34).
        out2.innerHTML = reevalResultHtml(key);
      }
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
      // A3-11: bind to the ACTIVE symbol (state) — never the raw input text.
      const sym = state.symbol || "";
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
      const ordersHtml = orders
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
      const stopsHtml = stops
        .map(function (s) {
          const slPx = Number(s.stopLossPrice);
          const tpPx = Number(s.takeProfitPrice);
          const trgPx = Number(s.triggerPrice != null ? s.triggerPrice : s.price);
          // T41b: shared classifier (field → label, mirrors protection.py). No
          // position side/entry in this list, so geometry is skipped; an
          // unlabeled trigger reads as SL (never a fabricated TP).
          const isTp = classifyTriggers(s).tp != null;
          const px = Number.isFinite(slPx) && slPx > 0 ? slPx
            : Number.isFinite(tpPx) && tpPx > 0 ? tpPx
            : trgPx;
          // N3-11: volume + signed distance to the live market so a resting
          // trigger is legible at a glance. Volume field name varies by
          // exchange (MEXC `vol`, others `sz`/`quantity`); show it only when a
          // finite value exists rather than a misleading "—". These trigger
          // rows are pre-filtered to the ACTIVE symbol, so state.lastPx is the
          // right reference for the distance.
          const svolRaw = Number(
            s.vol != null ? s.vol : s.sz != null ? s.sz : s.quantity
          );
          const volKv =
            Number.isFinite(svolRaw) && svolRaw > 0 ? posKv("Vol", fmt(svolRaw, 4)) : "";
          const mkt = Number(state.lastPx);
          const distPct =
            Number.isFinite(mkt) && mkt > 0 && Number.isFinite(px) && px > 0
              ? ((px - mkt) / mkt) * 100
              : null;
          const distKv =
            distPct != null
              ? posKv("Distanz", (distPct >= 0 ? "+" : "") + fmt(distPct, 2) + "%")
              : "";
          // N3-11: cancel a resting trigger. Wired to the EXISTING
          // /api/orders/cancel path via cancelTriggerOrder(), which prepends a
          // reinforced confirm for an SL (cancelling it leaves the position
          // unprotected). Shown only when the order carries a cancellable id.
          const soid =
            s.orderId != null ? s.orderId : s.oid != null ? s.oid : s.order_id;
          const cancelBtn =
            soid != null
              ? '<button type="button" class="btn-cancel-order btn-cancel-trigger" data-oid="' +
                escapeHtml(String(soid)) +
                '" data-sl="' + (isTp ? "0" : "1") + '">Cancel</button>'
              : "";
          return (
            '<div class="order-row order-row-trigger">' +
            '<span class="pos-sym">' + escapeHtml(s.symbol || "—") + "</span>" +
            '<span class="side-tag ' + (isTp ? "tag-long" : "tag-short") + '">' +
            (isTp ? "TP AKTIV" : "SL AKTIV") + "</span>" +
            posKv("Trigger", fmt(px, 4)) +
            volKv +
            distKv +
            cancelBtn +
            "</div>"
          );
        })
        .join("");
      // Cross-coin overview: resting orders / stops on OTHER coins.
      // U-04: build the full fragment in memory and assign innerHTML ONCE —
      // three separate `+=` assignments each re-parse and re-render the
      // entire (already-inserted) HTML, which thrashes the DOM for no reason.
      el.innerHTML = ordersHtml + stopsHtml + otherHtml;
      el.querySelectorAll(".btn-cancel-order:not(.btn-cancel-trigger)").forEach(function (btn) {
        btn.addEventListener("click", function () {
          cancelOrder(btn.getAttribute("data-oid"));
        });
      });
      // N3-11: trigger (SL/TP) cancels go through a confirm-gated wrapper — an
      // SL cancel gets a REINFORCED confirm (position ends up unprotected).
      el.querySelectorAll(".btn-cancel-trigger").forEach(function (btn) {
        btn.addEventListener("click", function () {
          cancelTriggerOrder(
            btn.getAttribute("data-oid"),
            btn.getAttribute("data-sl") === "1"
          );
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
    // Per-order double-submit guard: a double-click must not fire two cancels
    // for the same order (F6). Keyed by id so distinct orders still cancel.
    if (!state._cancelBusy) state._cancelBusy = {};
    if (state._cancelBusy[orderId]) return;
    state._cancelBusy[orderId] = true;
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
    } finally {
      delete state._cancelBusy[orderId];
    }
  }

  /** N3-11: Cancel a resting SL/TP trigger from the panel. Reuses the EXISTING
   *  /api/orders/cancel path (via cancelOrder) but prepends a confirm — a
   *  REINFORCED one for a stop-loss, because cancelling it leaves the position
   *  UNPROTECTED. There is no send without this confirm. */
  function cancelTriggerOrder(orderId, isSl) {
    if (!orderId) return;
    const text = isSl
      ? "Stop-Loss #" + orderId + " stornieren?\n\n" +
        "Die Position wird dann UNGESCHÜTZT — trotzdem stornieren?"
      : "Take-Profit-Trigger #" + orderId + " stornieren?";
    if (!window.confirm(text)) return;
    cancelOrder(orderId);
  }

  /** E3-06: derive and store the exchange Mark−Last offset per symbol from the
   *  account snapshot. The payload carries no mark price, but the exchange's
   *  `unrealized_pnl` IS computed from the mark, so we back the mark out:
   *
   *      pnl = (mark − entry) · vol · cs · (short ? −1 : 1)
   *   ⇒ mark = entry + pnl / (vol · cs · sign)
   *
   *  The reference "last" is the live WS tick for the active symbol (or the
   *  overview tile's last for others). offset = mark − last, later ADDED to the
   *  live tick. Guards: skips non-finite/zero inputs, and clamps out absurd
   *  offsets (> 5% of price — a real mark/last basis is tiny) so a data glitch
   *  can never inject a large, alarm-distorting correction. On skip the prior
   *  value simply ages out via the TTL and we fall back to the raw tick. */
  function updateMarkOffsets(data) {
    try {
      state._markOffset = state._markOffset || {};
      const positions = (data && data.positions) || [];
      positions.forEach(function (p) {
        const sym = String(p.symbol || "").toUpperCase();
        if (!sym) return;
        const vol = Number(p.hold_vol);
        const cs = positionContractSize(p.contract_size, 1);
        const entry = Number(p.entry_price);
        const pnl = Number(p.unrealized_pnl);
        if (
          !Number.isFinite(vol) || vol === 0 ||
          !Number.isFinite(cs) || cs === 0 ||
          !Number.isFinite(entry) || entry <= 0 ||
          !Number.isFinite(pnl)
        ) return;
        const sign = String(p.side || "").toLowerCase() === "short" ? -1 : 1;
        const impliedMark = entry + pnl / (vol * cs * sign);
        if (!Number.isFinite(impliedMark) || impliedMark <= 0) return;
        // Reference last-traded price: the active symbol has a live WS tick;
        // other symbols fall back to their overview tile's last (if fetched).
        let refLast = null;
        if (symMatch(p.symbol, state.symbol) && Number.isFinite(Number(state.lastPx)) && Number(state.lastPx) > 0) {
          refLast = Number(state.lastPx);
        } else {
          const d = state.overviewData && state.overviewData[sym];
          const l = d && Number(d.last_price);
          if (Number.isFinite(l) && l > 0) refLast = l;
        }
        if (refLast == null) return; // no reference → keep prior (ages out via TTL)
        const offset = impliedMark - refLast;
        if (!Number.isFinite(offset)) return;
        if (Math.abs(offset) > refLast * 0.05) return; // absurd basis → ignore (bad data)
        state._markOffset[sym] = { offset: offset, ts: Date.now() };
      });
    } catch (e) {
      console.error("updateMarkOffsets", e);
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
    updateMarkOffsets(data); // E3-06: refresh per-symbol Mark−Last basis before any render
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
      // C3-01: feature-detect via the endpoint's own `supported` flag
      // instead of hardcoding "only HL has fills" — the backend already
      // reports supported=false for any exchange client without a working
      // user_fills, so MEXC (and any future exchange) works through this
      // SAME code path with no frontend special-case.
      state.fills = data.supported && Array.isArray(data.fills) ? data.fills : [];
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
    // C3-06: loadFills() and loadMarket() resolve independently after a
    // coin/TF switch (goToSymbol fires both). If fills for the NEW
    // symbol/tf land before the new candles do, state.symbol/state.tf are
    // already the new values but state._chartKey (set only once loadMarket's
    // response lands) still reflects the OLD chart — drawing here would
    // bucket against the wrong series. Bail; loadMarket calls
    // applyTradeMarkers again right after it updates _chartKey.
    if (state._chartKey && state._chartKey !== state.symbol + "|" + state.tf) {
      return;
    }
    const chartColors = getChartColors();
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
    // C3-05: lastT is the last REST candle's open time. A WS-driven live bar
    // can already be newer (new bar opened, no poll yet) — without this, a
    // fill landing in that fresh live bar is outside the window and its
    // marker is dropped until the next ~15s poll pulls the candle in,
    // delaying the trader's own entry marker at the most exciting moment.
    const upperT =
      lastT != null && state.liveBar && Number.isFinite(state.liveBar.time)
        ? Math.max(lastT, state.liveBar.time)
        : lastT;

    (state.fills || []).forEach(function (f) {
      if (!symMatch(f.symbol, state.symbol) || !(Number(f.time) > 0)) return;
      const side = f.side === "buy" ? "buy" : "sell";
      const time = barOpenTimeSec(f.time, tf);
      if (firstT != null && (time < firstT || time > upperT)) return; // outside window
      const sz = Number(f.sz) || 0;
      const px = Number(f.px) || 0;
      const cls = classifyFillDir(f.dir); // C3-02: "open" | "close" | "liq"
      const key = time + "|" + side;
      let g = groups.get(key);
      if (!g) {
        g = {
          time: time,
          side: side,
          sz: 0,
          notional: 0,
          anyOpen: false,
          anyClose: false,
          anyLiq: false,
          closedPnl: 0,
        };
        groups.set(key, g);
      }
      g.sz += sz;
      g.notional += sz * px;
      g.closedPnl += Number(f.closed_pnl) || 0;
      if (cls === "liq") g.anyLiq = true;
      else if (cls === "close") g.anyClose = true;
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
    // Liquidation/flip groups are exempt (always labeled "LIQ" below) so
    // they never have to compete for one of the scarce text slots.
    let textKeys = null;
    const textCandidates = groupList.filter(function (g) {
      return !g.anyLiq;
    });
    if (textCandidates.length > TRADE_MARKER_TEXT_CAP) {
      textKeys = new Set(
        textCandidates
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

    // C3-04a: aggregate-map for the hover tooltip (id -> group detail). Same
    // key as the marker's own `id` below, so subscribeCrosshairMove's
    // hoveredObjectId resolves here in O(1) — no re-walking of state.fills
    // or groupList on every crosshair move.
    const aggById = new Map();

    const markers = groupList.map(function (g) {
      const buy = g.side === "buy";
      const avgPx = g.sz > 0 ? g.notional / g.sz : 0;
      const id = g.time + "|" + g.side; // matches the groups Map key above
      // A bucket with ANY open fill is treated as an entry (full color) even
      // if it also contains a close fill — the entry is what must stand out.
      const closeOnly = g.anyClose && !g.anyOpen;
      aggById.set(id, {
        time: g.time,
        sz: g.sz,
        avgPx: avgPx,
        side: g.side,
        closeOnly: closeOnly,
        anyLiq: g.anyLiq,
        closedPnl: g.closedPnl,
      });
      // C3-02: a liquidation or position flip is the worst-case event on the
      // chart — it must never be mistaken for a deliberate, well-colored
      // entry. Own shape/color/text, always labeled, independent of
      // open/close state (a liq bucket may well ALSO contain an open fill
      // from the resulting flip; the liq still dominates the marker).
      if (g.anyLiq) {
        return {
          time: g.time,
          id: id,
          position: buy ? "belowBar" : "aboveBar",
          color: chartColors.liq,
          shape: "square",
          text: "LIQ", // exempt from the text-count cap (see textCandidates above)
        };
      }
      const color = closeOnly
        ? buy
          ? chartColorAlpha(chartColors.long, 0.45) // dimmed: close of a long
          : chartColorAlpha(chartColors.short, 0.45) // dimmed: close of a short
        : buy
          ? chartColors.long
          : chartColors.short;
      const marker = {
        time: g.time,
        id: id,
        position: buy ? "belowBar" : "aboveBar",
        color: color,
        shape: buy ? "arrowUp" : "arrowDown",
      };
      const wantText = !textKeys || textKeys.has(g.time + "|" + g.side);
      if (wantText) {
        let txt = (buy ? "▲ " : "▼ ") + fmt(g.sz, 4) + " @ " + fmt(avgPx, 4);
        // C3-08: the most telling number on a close — what it actually made
        // or lost — was fetched from the backend but never shown anywhere.
        if (closeOnly) {
          txt += " · " + (g.closedPnl >= 0 ? "+" : "") + fmt(g.closedPnl, 2);
        }
        marker.text = txt;
      }
      return marker;
    });

    state._fillAggById = aggById;

    try {
      state.candleSeries.setMarkers(markers);
    } catch (e) {
      console.error("setMarkers", e);
    }
    // The hovered marker (if any) may no longer exist post-rebuild (fills
    // poll every 30s, a symbol switch replaces the set outright) — resolve
    // against the fresh map rather than blindly hiding, so a still-hovered
    // marker's tooltip doesn't flicker on every routine poll.
    syncMarkerTooltipAfterRebuild();
  }

  /* ── C3-04a: fill-marker hover tooltip ──────────────────────────────
     subscribeCrosshairMove fires on EVERY crosshair move (every mouse
     move over the chart), so the handler below is kept to the minimum:
     one Map.get() by id, and a DOM write ONLY when the hovered id
     actually changes (_hoverTooltipId guard) — no loop over fills/
     groupList, no getComputedStyle. Only the tooltip's on-screen
     position (a plain style.left/top write) updates on every move;
     content (innerHTML) is rebuilt solely on an id transition. */
  let _hoverTooltipId = null; // id of the marker whose tooltip is currently shown, or null
  let _hoverTooltipW = 0; // cached offsetWidth/Height of the tooltip at last content-build,
  let _hoverTooltipH = 0; // reused for clamping so the per-move path never re-reads layout

  function ensureMarkerTooltipEl() {
    if (state._markerTooltipEl) return state._markerTooltipEl;
    const wrap = $("chart-wrap");
    if (!wrap) return null;
    const el = document.createElement("div");
    el.className = "marker-tooltip hidden";
    wrap.appendChild(el);
    state._markerTooltipEl = el;
    return el;
  }

  function hideMarkerTooltip() {
    _hoverTooltipId = null;
    const el = state._markerTooltipEl;
    if (el) el.classList.add("hidden");
  }

  /** Rebuild the tooltip's content + cached size for `id`/`agg`. Only ever
   *  called on an id transition (crosshair) or a routine re-poll while the
   *  same marker is still hovered (applyTradeMarkers) — never per move. */
  function showMarkerTooltip(id, agg) {
    const el = ensureMarkerTooltipEl();
    if (!el) return;
    const buy = agg.side === "buy";
    const dirLabel = agg.anyLiq
      ? "LIQ"
      : buy
        ? agg.closeOnly
          ? "Close Short"
          : "Open/Add Long"
        : agg.closeOnly
          ? "Close Long"
          : "Open/Add Short";
    let html =
      '<div class="mt-row">' + escapeHtml(chartLocalTime(agg.time, true)) + "</div>" +
      '<div class="mt-row">' + escapeHtml(dirLabel) + "</div>" +
      '<div class="mt-row">Größe: ' + escapeHtml(fmt(agg.sz, 4)) + "</div>" +
      '<div class="mt-row">VWAP: ' + escapeHtml(fmt(agg.avgPx, 4)) + "</div>";
    // C3-08: realized PnL only means something on a close-only bucket —
    // an open/add has none yet (mirrors the marker-text rule above).
    if (agg.closeOnly) {
      const pnlCls = agg.closedPnl >= 0 ? "mt-pnl-pos" : "mt-pnl-neg";
      const pnlTxt = (agg.closedPnl >= 0 ? "+" : "") + fmt(agg.closedPnl, 2);
      html +=
        '<div class="mt-row">PnL: <span class="' + pnlCls + '">' +
        escapeHtml(pnlTxt) +
        "</span></div>";
    }
    el.innerHTML = html;
    el.classList.remove("hidden");
    _hoverTooltipId = id;
    _hoverTooltipW = el.offsetWidth;
    _hoverTooltipH = el.offsetHeight;
  }

  /** Called after every applyTradeMarkers rebuild (symbol switch or the
   *  30s fills poll) — the currently-hovered id may no longer exist (or
   *  its numbers may have moved slightly). Resolve against the fresh map:
   *  gone → hide; still there → refresh content so it doesn't go stale. */
  function syncMarkerTooltipAfterRebuild() {
    if (_hoverTooltipId === null) return;
    const agg = state._fillAggById && state._fillAggById.get(_hoverTooltipId);
    if (!agg) {
      hideMarkerTooltip();
      return;
    }
    showMarkerTooltip(_hoverTooltipId, agg);
  }

  /** Cheap per-move path: cursor-relative placement using the tooltip size
   *  cached at last content-build (no layout read here), clamped inside
   *  #chart-wrap so it can never render offscreen near the chart edges. */
  function positionMarkerTooltip(point) {
    const el = state._markerTooltipEl;
    if (!el || !point) return;
    const wrap = $("chart-wrap");
    const wrapW = wrap ? wrap.clientWidth : 0;
    const wrapH = wrap ? wrap.clientHeight : 0;
    const gap = 14; // small offset so the tooltip doesn't sit under the cursor
    let left = point.x + gap;
    let top = point.y + gap;
    if (wrapW && left + _hoverTooltipW > wrapW) left = point.x - _hoverTooltipW - gap;
    if (wrapH && top + _hoverTooltipH > wrapH) top = point.y - _hoverTooltipH - gap;
    if (left < 0) left = 0;
    if (top < 0) top = 0;
    el.style.left = left + "px";
    el.style.top = top + "px";
  }

  /** subscribeCrosshairMove handler — see perf note in the block comment
   *  above. `param.hoveredObjectId` is whatever `id` we gave the marker in
   *  applyTradeMarkers, so this is a single Map.get(), never a re-scan of
   *  state.fills/groupList. */
  function handleMarkerCrosshairMove(param) {
    const id =
      param && param.hoveredObjectId != null ? String(param.hoveredObjectId) : null;
    if (id == null) {
      if (_hoverTooltipId !== null) hideMarkerTooltip();
      return;
    }
    const agg = state._fillAggById && state._fillAggById.get(id);
    if (!agg) {
      if (_hoverTooltipId !== null) hideMarkerTooltip();
      return;
    }
    if (id !== _hoverTooltipId) {
      showMarkerTooltip(id, agg);
    }
    positionMarkerTooltip(param.point);
  }

  /** Reference entry price for size/risk math: limit price (LIMIT orders
   *  only — T3-01: a MARKET order must never size off a stale #ticket-price
   *  left over from a prior limit order), else entry ref, else the live
   *  last price — mirroring the backend gate, which sizes MARKET orders off
   *  live_price regardless of what #ticket-price contains. */
  function refEntryPrice() {
    const orderType = ($("ticket-type") && $("ticket-type").value) || "market";
    const limitPx = orderType === "limit" ? numOrNull($("ticket-price")) : null;
    return (
      limitPx ||
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

  /** T3-10: minimum price increment for the active contract (MEXC's
   *  priceUnit) — the same source drawTicketLines() already reads to dedupe
   *  against active order/position lines. Falls back to a cent before the
   *  market's loaded (never 0/NaN — an invalid step attribute is silently
   *  ignored by the browser, which reverts to the native default of 1). */
  function tickSize() {
    const t = Number(
      state.market && state.market.contract && state.market.contract.priceUnit
    );
    return t > 0 ? t : 0.01;
  }

  /** Arrow-key/spinner step on the price inputs must scale with the coin's
   *  tick size — a hardcoded step="1" (or the browser's default step of 1
   *  under step="any") means one ArrowUp on a $0.002 memecoin jumps the
   *  price by 500x its own value. %-mode SL/TP are NOT prices, so they keep
   *  a flat 0.1-percentage-point step regardless of tick size. Called
   *  whenever the contract meta changes (loadMarket) and whenever the
   *  %/Kurs toggle flips (setSltpMode). */
  function updatePriceFieldSteps() {
    const tick = String(tickSize());
    const entryEl = $("ticket-entry");
    const priceEl = $("ticket-price");
    if (entryEl) entryEl.step = tick;
    if (priceEl) priceEl.step = tick;
    const slEl = $("ticket-sl");
    const tpEl = $("ticket-tp1");
    const step = sltpMode() === "pct" ? "0.1" : tick;
    if (slEl) slEl.step = step;
    if (tpEl) tpEl.step = step;
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

  /** T3-02: converts existing SL/TP field values across a Kurs<->% toggle so
   *  a typed "61000" (price) never gets silently reinterpreted as "61000 %".
   *  Mirrors setSizeMode's unit-conversion pattern below. */
  function convertSltpFieldsOnModeSwitch(prevMode, nextMode) {
    if (prevMode === nextMode) return;
    const sl = $("ticket-sl");
    const tp = $("ticket-tp1");
    if (prevMode === "pct" && nextMode === "price") {
      // %→Kurs: reuse the existing resolvers — state.sltpMode is still "pct"
      // at this point, so they read the raw field values as % distances.
      const slPrice = resolveStop();
      const tpPrice = resolveTp();
      if (sl && slPrice != null) sl.value = String(Math.round(slPrice * 100) / 100);
      if (tp && tpPrice != null) tp.value = String(Math.round(tpPrice * 100) / 100);
      return;
    }
    if (prevMode === "price" && nextMode === "pct") {
      // Kurs→%: derive the % distance from the raw price + refEntryPrice().
      const entry = refEntryPrice();
      if (!entry) return;
      const long = currentSide() === "long";
      const slRaw = numOrNull(sl);
      if (sl && slRaw != null && slRaw > 0) {
        const pct = long
          ? ((entry - slRaw) / entry) * 100
          : ((slRaw - entry) / entry) * 100;
        sl.value = String(Math.round(pct * 100) / 100);
      }
      const tpRaw = numOrNull(tp);
      if (tp && tpRaw != null && tpRaw > 0) {
        const pct = long
          ? ((tpRaw - entry) / entry) * 100
          : ((entry - tpRaw) / entry) * 100;
        tp.value = String(Math.round(pct * 100) / 100);
      }
    }
  }

  /** Shows a "%" / currency suffix badge on the SL/TP inputs so the active
   *  unit is always visible next to the typed value (T3-02). */
  function updateSltpUnitSuffix() {
    const unit = state.sltpMode === "pct" ? "%" : ccy();
    [
      ["ticket-sl", "field-sl"],
      ["ticket-tp1", "field-tp"],
    ].forEach(function (pair) {
      const input = $(pair[0]);
      if (!input || !input.parentElement) return;
      let badge = input.parentElement.querySelector(".sltp-unit");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "sltp-unit";
        input.parentElement.appendChild(badge);
      }
      badge.textContent = unit;
    });
  }

  function setSltpMode(mode) {
    const prevMode = sltpMode();
    const nextMode = mode === "pct" ? "pct" : "price";
    convertSltpFieldsOnModeSwitch(prevMode, nextMode);
    state.sltpMode = nextMode;
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
    updateSltpUnitSuffix();
    updatePriceFieldSteps(); // T3-10: %-mode uses a flat step, price-mode uses the tick
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
    try { localStorage.setItem(PERSIST.SIZE_MODE, next); } catch (_) {}
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

  /** Effective sizing risk %: mirrors the backend's gate cap (main.py
   *  suggest_vol handler) so the "X % Risiko" button label, its request
   *  and the success toast can never disagree. Falls back to 1.0 (never
   *  above the backend cap) before /health has resolved. */
  function maxRiskPct() {
    return Number(state.health && state.health.max_risk_pct) || 1.0;
  }

  /** T3-03: single source of truth for the RRR threshold — mirrors the
   *  backend's risk_policy.min_rrr (app/config.py, enforced in
   *  app/risk/gates.py) so the readout-green color, the confirm-modal ack
   *  gate and the _humanGate() copy can never disagree with each other or
   *  with what the server actually enforces. Previously each of those three
   *  hardcoded its own constant (2, 1.5, "1:2" in the gate text) — a
   *  conservative-profile min_rrr of 2.0 would then contradict the UI's
   *  hardcoded 1.5 ack threshold. Falls back to 1.5 (the backend's Settings
   *  default) before /api/health has resolved. */
  function minRrr() {
    const v = Number(state.health && state.health.min_rrr);
    return Number.isFinite(v) && v > 0 ? v : 1.5;
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

  var TRADE_MARKERS_KEY = PERSIST.TRADE_MARKERS; // A3-02: routed via store.js PERSIST table
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

  /** Fold tradeMarkers persisted under a non-canonical key (e.g. a full pair
   *  typed on Hyperliquid, where positions/the manual-SL alarm canonically key
   *  by bare coin) into markerKey() form, so a marker saved under the old key
   *  isn't orphaned (A3-01). Idempotent — once every key is already canonical
   *  this is a no-op. On a collision (old + new key both present) the newer
   *  `ts` wins, same tie-break saveTradeMarkers() uses for cross-tab merges. */
  function migrateTradeMarkerKeys(raw) {
    const out = {};
    let changed = false;
    Object.keys(raw || {}).forEach(function (rawKey) {
      const canon = markerKey(rawKey);
      if (!canon) return;
      if (canon !== rawKey) changed = true;
      const incoming = raw[rawKey];
      const existing = out[canon];
      if (!existing || Number((incoming && incoming.ts) || 0) >= Number(existing.ts || 0)) {
        out[canon] = incoming;
      }
    });
    return { markers: out, changed: changed };
  }

  /** Merge our in-memory markers with whatever another tab persisted, keeping
   *  the newer entry per symbol (by ts), so concurrent tabs don't clobber each
   *  other's manual-SL markers (audit F3). A key we deleted locally (e.g. via
   *  pruneTradeMarkers) only stays deleted in storage if no other tab wrote a
   *  newer version of it since our last sync; otherwise their write wins. */
  function saveTradeMarkers() {
    try {
      const stored = migrateTradeMarkerKeys(_readMarkersRaw()).markers;
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
    const migrated = migrateTradeMarkerKeys(_readMarkersRaw());
    state.tradeMarkers = migrated.markers;
    _tradeMarkersSynced = Object.assign({}, state.tradeMarkers);
    if (migrated.changed) {
      try {
        localStorage.setItem(TRADE_MARKERS_KEY, JSON.stringify(migrated.markers));
      } catch (_) {}
    }
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
      // A3-01: keys are canonical markerKey() form now, so compare on that
      // (not symMatch) — a bare-key equality check is exact and cheaper.
      const hasPos = positions.some(function (p) { return markerKey(p.symbol) === key; });
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
          "risk-val" + (rrr != null && rrr >= minRrr() ? " rr-good" : rrr != null ? " rr-warn" : "");
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
    // Symbol at request time; compared against the active symbol when the
    // response lands so a sizing result computed off a different coin's SL
    // distance/price can never be written into the current ticket (M-1).
    const reqSymbol = ticket.symbol;
    if (ticket.stop_loss == null) {
      setTicketError("Erst Stop-Loss-Kurs eintragen — dann kann die Größe berechnet werden.");
      return;
    }
    // T3-04: margin mode needs a real leverage to turn notional into margin.
    // No "|| 1" fallback — that would silently write the FULL notional as
    // margin, causing up to Nx oversize once the leverage is filled in later.
    if (sizeMode() === "margin") {
      const levCheck = numOrNull($("ticket-leverage"));
      if (!levCheck || levCheck <= 0) {
        setTicketError("Hebel eintragen — Margin-Größe kann sonst nicht berechnet werden.");
        return;
      }
    }
    const rp = maxRiskPct();
    try {
      const res = await apiFetch("/api/sizing/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({}, ticket, { risk_pct: rp })),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      // A3-11: compare against the ACTIVE symbol (state), not the input text.
      const curSymbol = state.symbol || "";
      if (!symMatch(reqSymbol, curSymbol)) {
        // Symbol changed while this sizing request was in flight — discard
        // the stale result instead of writing it into the new ticket.
        return;
      }
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
          // T3-04: no "|| 1" fallback — writing full notional as margin when
          // leverage is missing/invalid would oversize up to Nx once the
          // user later fills in the real leverage.
          const lev = numOrNull($("ticket-leverage"));
          if (!lev || lev <= 0) {
            setTicketError("Hebel eintragen — Margin-Größe kann sonst nicht berechnet werden.");
            return;
          }
          val = data.notional_usdt / lev;
        }
        usdtEl.value = String(Math.round(val * 100) / 100);
      }
      updateRiskReadout();
      showToast(
        "Größe für " + fmt(rp, 2) + " % Risiko: " +
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

  /** Human label for a journal-filter field (used by the filter chip text). */
  function _journalFieldLabel(field) {
    switch (field) {
      case "setup_confidence": return "Confidence";
      case "action": return "Action";
      case "provider": return "Provider";
      default: return field || "";
    }
  }

  /** One {name -> {wins, losses, sample, win_rate, avg_realized_rrr, low_sample}}
   *  breakdown as a compact table; "" when there is nothing to show (empty DB).
   *  Task 40 (N3-17): each row is now clickable — `field` names the Entries-
   *  table column the row's key `k` maps to 1:1 (the group key IS the raw
   *  DB value, e.g. by_confidence's "low"/"medium"/"high" === entries[].
   *  setup_confidence verbatim), so clicking just sets that as the active
   *  filter chip. Delegated click handling lives on #journal-body
   *  (onJournalBodyClick); this fn only marks the row up. */
  function renderJournalBreakdown(title, groups, field) {
    const keys = Object.keys(groups || {});
    if (!keys.length) return "";
    let html = '<div class="journal-breakdown">';
    html += "<h4>" + escapeHtml(title) + "</h4>";
    html +=
      '<table class="journal-breakdown-table"><thead><tr>' +
      "<th></th><th>n</th><th>Win-Rate</th><th>Ø R</th>" +
      "</tr></thead><tbody>";
    for (const k of keys) {
      const g = groups[k] || {};
      const isActive =
        !!state.journalFilter &&
        state.journalFilter.field === field &&
        state.journalFilter.value === k;
      const rowStyle = isActive
        ? "cursor:pointer;background:var(--accent-dim,rgba(127,127,127,0.18));"
        : "cursor:pointer;";
      html +=
        '<tr class="' + (g.low_sample ? "low-sample" : "") + '" ' +
        'data-jf-field="' + escapeHtml(field) + '" data-jf-value="' + escapeHtml(k) + '" ' +
        'title="Klicken: Entries-Tabelle auf ' + escapeHtml(_journalFieldLabel(field)) +
        " = " + escapeHtml(k) + ' filtern" style="' + rowStyle + '">' +
        "<td>" + escapeHtml(k) + "</td>" +
        "<td>" + fmt(g.sample, 0) + "</td>" +
        "<td>" +
        (g.win_rate != null ? fmt(g.win_rate * 100, 1) + "%" : "—") +
        "</td>" +
        "<td>" +
        (g.avg_realized_rrr != null ? fmt(g.avg_realized_rrr, 2) : "—") +
        "</td>" +
        "</tr>";
    }
    html += "</tbody></table></div>";
    return html;
  }

  /** Journal (KI shadow book) tab: stats block (win rate + Wilson CI + avg
   *  realized R, STAY_OUT rate, breakdowns, caveats) + a read-only table of
   *  recent entries with outcome badges. Read-only, no writes — measurement
   *  only, never touches the order/gate path. */
  function renderJournal(stats, entries) {
    const body = $("journal-body");
    if (!body) return;
    entries = Array.isArray(entries) ? entries : [];
    const hasStats = stats && stats.overall;

    if (!hasStats && !entries.length) {
      body.className = "placeholder";
      body.textContent = "Noch keine Journal-Einträge.";
      return;
    }

    let html = '<div class="journal-content">';

    if (hasStats) {
      const ov = stats.overall || {};
      const tot = stats.totals || {};
      const ci = Array.isArray(ov.win_rate_ci95) ? ov.win_rate_ci95 : null;
      const sample = ov.sample || 0;

      html += '<div class="journal-stats">';
      if (!sample) {
        html +=
          '<p class="muted journal-empty-stats">Noch keine aufgelösten Einträge — ' +
          "zu wenig Daten für eine belastbare Trefferquote.</p>";
      } else {
        html +=
          '<div class="journal-overall' +
          (ov.low_sample ? " low-sample" : "") +
          '">' +
          '<span class="journal-stat"><b>Win-Rate:</b> ' +
          (ov.win_rate != null ? fmt(ov.win_rate * 100, 1) + "%" : "—") +
          " (n=" + fmt(sample, 0) + ")" +
          (ci
            ? " CI95 [" +
              fmt(ci[0] * 100, 1) +
              "%–" +
              fmt(ci[1] * 100, 1) +
              "%]"
            : "") +
          "</span>" +
          '<span class="journal-stat"><b>Ø realized R:</b> ' +
          (ov.avg_realized_rrr != null ? fmt(ov.avg_realized_rrr, 2) : "—") +
          "</span>" +
          (ov.low_sample
            ? '<span class="journal-lowflag">zu wenig Daten</span>'
            : "") +
          "</div>";
      }
      html +=
        '<div class="journal-totals muted">' +
        "Proposals: " + fmt(tot.proposals, 0) +
        " · STAY_OUT-Rate: " +
        (tot.stay_out_rate != null ? fmt(tot.stay_out_rate * 100, 1) + "%" : "—") +
        " · Pending: " + fmt(tot.pending, 0) +
        " · Expired: " + fmt(tot.expired, 0) +
        " · Skipped: " + fmt(tot.skipped, 0) +
        "</div>";

      html += renderJournalBreakdown("Nach Confidence", stats.by_confidence, "setup_confidence");
      html += renderJournalBreakdown("Nach Action", stats.by_action, "action");
      html += renderJournalBreakdown("Nach Provider", stats.by_provider, "provider");

      const caveats = Array.isArray(stats.caveats) ? stats.caveats : [];
      if (caveats.length) {
        html += '<ul class="journal-caveats muted">';
        for (const c of caveats) {
          html += "<li>" + escapeHtml(String(c)) + "</li>";
        }
        html += "</ul>";
      }
      html += "</div>";
    }

    if (entries.length) {
      // Task 40 (N3-17): a clicked breakdown row (renderJournalBreakdown)
      // sets state.journalFilter — apply it here as a client-side filter on
      // the Entries table. Survives a data refresh (loadJournal re-renders
      // with the SAME state.journalFilter still set) and clears cleanly via
      // the chip's ✕ button or re-clicking the already-active row
      // (onJournalBodyClick toggles it off).
      const activeFilter = state.journalFilter;
      const filteredEntries = activeFilter
        ? entries.filter(function (e) {
            return String((e && e[activeFilter.field]) || "") === activeFilter.value;
          })
        : entries;

      if (activeFilter) {
        html +=
          '<div class="muted" style="display:flex;align-items:center;gap:8px;margin:2px 0 8px;">' +
          "<span>Filter: <b>" + escapeHtml(_journalFieldLabel(activeFilter.field)) + " = " +
          escapeHtml(activeFilter.value) + "</b> (" + fmt(filteredEntries.length, 0) +
          " von " + fmt(entries.length, 0) + ")</span>" +
          '<button type="button" data-jf-clear="1" style="cursor:pointer;border:1px solid currentColor;' +
          'background:none;border-radius:10px;padding:0 8px;font:inherit;color:inherit;">' +
          "✕ Filter löschen</button>" +
          "</div>";
      }

      if (filteredEntries.length) {
        html +=
          '<table class="history-table journal-table"><thead><tr>' +
          "<th>Zeit</th><th>Symbol</th><th>Action</th><th>Conf</th><th>Entry</th>" +
          "<th>SL</th><th>TP1</th><th>RRR</th><th>Outcome</th>" +
          "</tr></thead><tbody>";
        for (const e of filteredEntries) {
          const st = e.status || "PENDING";
          html +=
            "<tr>" +
            "<td>" + escapeHtml(shortIso(e.created_at)) + "</td>" +
            "<td>" + escapeHtml(e.symbol || "—") + "</td>" +
            '<td class="hist-action action-' +
            escapeHtml(e.action || "") +
            '">' +
            escapeHtml(e.action || "—") +
            "</td>" +
            "<td>" + escapeHtml(e.setup_confidence || "—") + "</td>" +
            "<td>" + (e.entry_price != null ? fmt(e.entry_price, 4) : "—") + "</td>" +
            "<td>" + (e.stop_loss != null ? fmt(e.stop_loss, 4) : "—") + "</td>" +
            "<td>" + (e.tp1 != null ? fmt(e.tp1, 4) : "—") + "</td>" +
            "<td>" + (e.rrr != null ? fmt(e.rrr, 2) : "—") + "</td>" +
            "<td>" +
            '<span class="journal-badge journal-badge-' +
            escapeHtml(st) +
            '">' +
            escapeHtml(st) +
            "</span>" +
            (e.ambiguous
              ? ' <span class="journal-ambiguous" title="tp1 und SL im selben Candle — pessimistisch als LOSS gewertet">~</span>'
              : "") +
            "</td>" +
            "</tr>";
        }
        html += "</tbody></table>";
      } else {
        html += '<p class="muted history-empty">Keine Einträge für diesen Filter.</p>';
      }
    } else {
      html += '<p class="muted history-empty">Noch keine Journal-Einträge.</p>';
    }

    html += "</div>";
    body.className = "journal-body";
    if (!state._journalWired) {
      body.addEventListener("click", onJournalBodyClick);
      state._journalWired = true;
    }
    body.innerHTML = html;
  }

  /** Delegated click handler for #journal-body (Task 40 / N3-17): a
   *  breakdown-row click sets/toggles the Entries-table filter chip; the
   *  chip's ✕ clears it. Read-only — only re-renders from already-cached
   *  state.journalStats/state.journalEntries, no re-fetch, no writes. */
  function onJournalBodyClick(ev) {
    const clearBtn = ev.target.closest("[data-jf-clear]");
    if (clearBtn) {
      state.journalFilter = null;
      renderJournal(state.journalStats, state.journalEntries);
      return;
    }
    const row = ev.target.closest("[data-jf-field]");
    if (!row) return;
    const field = row.getAttribute("data-jf-field");
    const value = row.getAttribute("data-jf-value");
    if (!field) return;
    const same =
      state.journalFilter &&
      state.journalFilter.field === field &&
      state.journalFilter.value === value;
    state.journalFilter = same ? null : { field: field, value: value };
    renderJournal(state.journalStats, state.journalEntries);
  }

  /** loadJournal: fetch both /api/journal/stats and /api/journal, then render.
   *  Mirrors loadHistory's soft-fail: any error shows an inline message in the
   *  pane and never throws further (journal is measurement-only). */
  async function loadJournal() {
    const body = $("journal-body");
    try {
      const [statsRes, entriesRes] = await Promise.all([
        apiFetch("/api/journal/stats"),
        apiFetch("/api/journal?limit=50"),
      ]);
      if (!statsRes.ok) throw new Error("journal stats " + statsRes.status);
      if (!entriesRes.ok) throw new Error("journal " + entriesRes.status);
      const stats = await statsRes.json();
      const data = await entriesRes.json();
      const entries = Array.isArray(data.entries) ? data.entries : [];
      state.journalStats = stats;
      state.journalEntries = entries;
      renderJournal(stats, entries);
      return { stats: stats, entries: entries };
    } catch (err) {
      console.error("loadJournal", err);
      if (body) {
        body.className = "placeholder";
        body.textContent =
          "Journal-Fehler: " + (err && err.message ? err.message : err);
      }
      return null;
    }
  }

  /** Task 40 (N3-14 Stufe 1): fold a symbol's fill ledger into ROUND-TRIPS
   *  (flat → position → flat) instead of the raw per-fill list — a real
   *  trade log, not a request log. Pure client-side reconstruction from the
   *  `side`/`sz`/`px`/`fee`/`closed_pnl` fields already on each fill; no
   *  backend endpoint involved.
   *
   *  Pairing basis: reconstruct the SIGNED running position from side+sz
   *  alone (buy = +sz, sell = -sz) — this is exact regardless of the
   *  free-text `dir` label, so laddered partial fills (many small adds/
   *  reduces) net out correctly. A round-trip starts the instant the
   *  position leaves 0 and ends the instant it returns to 0:
   *    - 0 → nonzero: opens a new round-trip.
   *    - same-sign, |pos| growing: an ADD (entry side).
   *    - same-sign, |pos| shrinking (incl. exactly to 0): a REDUCE/CLOSE
   *      (exit side) — its closed_pnl is real exchange-reported realized
   *      PnL for that reduction, summed as-is (never recomputed/guessed).
   *    - sign flip in ONE fill (e.g. long 1 → sell 2 → short 1): the fill
   *      is split proportionally by size — the |prevPos| portion closes
   *      the old round-trip (closed_pnl attributed there in full, since
   *      that IS what it was realized on), the remainder opens a new one;
   *      the fill's fee is split by the same size fraction (the fairest
   *      available basis — a single execution has one fee for the whole
   *      fill, no per-portion fee is reported).
   *  A same-symbol re-open (flat → open again later) is automatically a
   *  SEPARATE round-trip: nothing merges across a 0-crossing.
   *
   *  Honesty guards (never fabricate a number):
   *   - `fills` is capped to the last 100 executions (backend limit) — if
   *     the WINDOW'S OLDEST fill doesn't classify as an "open" via the
   *     backend's own `dir` field, the true entry happened before the
   *     window and the reconstructed entry size/price for that first
   *     round-trip is incomplete. Flagged `truncatedStart` and called out
   *     in the UI rather than presented as a clean full round-trip.
   *   - a position still open at the end of the window is NOT a closed
   *     round-trip (no realized PnL exists for it yet) — returned
   *     separately as `openTrade`, rendered as a plain note, never given a
   *     fabricated PnL figure.
   */
  function foldFillsToRoundTrips(fills) {
    const sorted = (fills || [])
      .filter(function (f) { return Number.isFinite(Number(f.sz)) && Math.abs(Number(f.sz)) > 0; })
      .slice()
      .sort(function (a, b) { return (Number(a.time) || 0) - (Number(b.time) || 0); });

    const EPS = 1e-9;
    const closed = [];
    let open = null; // in-progress round-trip accumulator
    let pos = 0; // signed running position size
    let firstProcessed = false;

    function newRt(startTime, sideSign) {
      return {
        side: sideSign > 0 ? "long" : "short",
        startTime: startTime,
        endTime: null,
        entrySz: 0,
        entryNotional: 0,
        exitSz: 0,
        exitNotional: 0,
        pnl: 0,
        fee: 0,
        fillCount: 0,
        isLiq: false,
        truncatedStart: false,
      };
    }

    sorted.forEach(function (f) {
      const sz = Math.abs(Number(f.sz) || 0);
      if (!(sz > 0)) return;
      const px = Number(f.px) || 0;
      const fee = Number(f.fee) || 0;
      const pnl = Number(f.closed_pnl) || 0;
      const delta = f.side === "buy" ? sz : -sz;
      const prevPos = pos;
      const newPos = prevPos + delta;
      const cls = classifyFillDir(f.dir);
      const isFirst = !firstProcessed;
      firstProcessed = true;

      if (Math.abs(prevPos) < EPS) {
        // Flat → nonzero: opens a new round-trip.
        open = newRt(f.time, newPos);
        if (isFirst && cls !== "open") open.truncatedStart = true;
        // A genuine open reports closed_pnl≈0. A NONZERO closed_pnl on an
        // "opening" fill means the fetched fill window began mid-position (the
        // real position was already open before the window) → this fill was
        // misclassified as an open. Capture the real PnL instead of silently
        // dropping it, and flag the round-trip so the UI never claims "no PnL".
        if (Math.abs(pnl) > EPS) open.truncatedStart = true;
        if (cls === "liq") open.isLiq = true;
        open.entrySz += sz;
        open.entryNotional += sz * px;
        open.pnl += pnl;
        open.fee += fee;
        open.fillCount++;
        pos = newPos;
        return;
      }

      const flip = (prevPos > 0 && newPos < -EPS) || (prevPos < 0 && newPos > EPS);
      if (!open) open = newRt(f.time, prevPos); // defensive: should not happen once flat-start is seeded

      if (flip) {
        const closeSz = Math.abs(prevPos);
        const openSz = Math.max(0, sz - closeSz);
        const closeFrac = sz > 0 ? closeSz / sz : 0;
        const openFrac = 1 - closeFrac;
        if (cls === "liq") open.isLiq = true;
        open.exitSz += closeSz;
        open.exitNotional += closeSz * px;
        open.pnl += pnl; // whole reported closed_pnl belongs to the closed leg
        open.fee += fee * closeFrac;
        open.fillCount++;
        open.endTime = f.time;
        closed.push(open);
        open = newRt(f.time, newPos);
        open.entrySz += openSz;
        open.entryNotional += openSz * px;
        open.fee += fee * openFrac;
        open.fillCount++;
        pos = newPos;
        return;
      }

      const growing = Math.abs(newPos) > Math.abs(prevPos) + EPS;
      if (growing) {
        // Same-sign add to the existing round-trip's entry side. An add should
        // not realize PnL; a nonzero closed_pnl here means the window began
        // mid-position → capture it and flag truncation instead of dropping it.
        if (Math.abs(pnl) > EPS) open.truncatedStart = true;
        open.entrySz += sz;
        open.entryNotional += sz * px;
        open.pnl += pnl;
        open.fee += fee;
        open.fillCount++;
        pos = newPos;
        return;
      }

      // Same-sign reduce (partial or exactly-to-zero close).
      if (cls === "liq") open.isLiq = true;
      open.exitSz += sz;
      open.exitNotional += sz * px;
      open.pnl += pnl;
      open.fee += fee;
      open.fillCount++;
      pos = newPos;
      if (Math.abs(newPos) < EPS) {
        pos = 0;
        open.endTime = f.time;
        closed.push(open);
        open = null;
      }
    });

    closed.reverse(); // newest first, matching the rest of the panel
    return { closed: closed, openTrade: open };
  }

  /** Small unstyled sub-tab button (no dedicated CSS class exists for this —
   *  Task 40 is app.js-only, no CSS touched — so the active/inactive look is
   *  applied inline instead of adding a class the stylesheet doesn't know). */
  function _rtSubtabBtn(key, label, active) {
    const style = active
      ? "color:var(--text,inherit);border-bottom-color:var(--accent,currentColor);font-weight:600;"
      : "color:var(--muted,inherit);border-bottom-color:transparent;";
    return (
      '<button type="button" data-rt-subtab="' + escapeHtml(key) + '" ' +
      'aria-pressed="' + (active ? "true" : "false") + '" ' +
      'style="background:none;border:none;border-bottom:2px solid;margin:0 ' +
      '10px 0 0;padding:4px 2px;cursor:pointer;font:inherit;' + style + '">' +
      escapeHtml(label) +
      "</button>"
    );
  }

  /** Round-trip table for the "Trades" tab (Task 40 / N3-14 Stufe 1). */
  function renderRoundTripsHtml(fills, symLabel) {
    const folded = foldFillsToRoundTrips(fills);
    let html = "";
    if (!folded.closed.length) {
      html +=
        '<div class="trades-empty">Noch keine abgeschlossenen Round-Trips für ' +
        escapeHtml(symLabel) + ".</div>";
    } else {
      html +=
        '<table class="history-table rt-table"><thead><tr>' +
        "<th>Zeit</th><th>Seite</th><th>Größe</th><th>Entry Ø</th><th>Exit Ø</th>" +
        "<th>PnL</th><th>Fees</th><th>Netto</th><th>Fills</th><th></th>" +
        "</tr></thead><tbody>";
      folded.closed.forEach(function (rt) {
        const entryPx = rt.entrySz > 0 ? rt.entryNotional / rt.entrySz : 0;
        const exitPx = rt.exitSz > 0 ? rt.exitNotional / rt.exitSz : 0;
        const net = rt.pnl - rt.fee;
        const pnlCls = rt.pnl > 0 ? "pnl-pos" : rt.pnl < 0 ? "pnl-neg" : "";
        const netCls = net > 0 ? "pnl-pos" : net < 0 ? "pnl-neg" : "";
        const t0 = Number(rt.startTime);
        const timeTxt =
          Number.isFinite(t0) && t0 > 0
            ? escapeHtml(relTime(new Date(t0).toISOString()))
            : "—";
        const flags = [];
        if (rt.isLiq) {
          flags.push('<span class="hist-err" title="Round-Trip enthält eine Liquidation">LIQ</span>');
        }
        if (rt.truncatedStart) {
          flags.push(
            '<span class="muted" title="Position begann vor dem geladenen Fill-Fenster ' +
            '(letzte 100 Fills) — Seite kann invertiert und PnL/Beträge ' +
            'unvollständig sein">Fenster-Anfang ⚠</span>'
          );
        }
        html +=
          "<tr>" +
          "<td>" + timeTxt + "</td>" +
          '<td><span class="side-tag ' + (rt.side === "long" ? "tag-long" : "tag-short") + '">' +
          (rt.side === "long" ? "LONG" : "SHORT") + "</span></td>" +
          "<td>" + fmt(rt.entrySz, 4) + "</td>" +
          "<td>" + fmt(entryPx, 4) + "</td>" +
          "<td>" + (rt.exitSz > 0 ? fmt(exitPx, 4) : "—") + "</td>" +
          '<td class="' + pnlCls + '">' + (rt.pnl >= 0 ? "+" : "") + fmt(rt.pnl, 2) + "</td>" +
          "<td>" + fmt(rt.fee, 2) + "</td>" +
          '<td class="' + netCls + '">' + (net >= 0 ? "+" : "") + fmt(net, 2) + "</td>" +
          "<td>" + fmt(rt.fillCount, 0) + "</td>" +
          "<td>" + flags.join(" ") + "</td>" +
          "</tr>";
      });
      html += "</tbody></table>";
    }
    if (folded.openTrade && folded.openTrade.entrySz > 0) {
      const ot = folded.openTrade;
      const t0 = Number(ot.startTime);
      const otPnl = Number(ot.pnl) || 0;
      const otHasPnl = Math.abs(otPnl) > 1e-9;
      const sinceTxt =
        Number.isFinite(t0) && t0 > 0
          ? ", seit " + escapeHtml(relTime(new Date(t0).toISOString()))
          : "";
      const head =
        "Aktuell offene Position (" +
        fmt(ot.fillCount, 0) + " Fill" + (ot.fillCount === 1 ? "" : "s") + sinceTxt + ")";
      let msg;
      if (ot.truncatedStart || otHasPnl) {
        // Window began mid-position: side/totals may be off, and a real PnL was
        // already booked before the window. NEVER claim "noch kein Realized-PnL".
        msg =
          head + ". ⚠ Fill-Fenster beginnt mitten in der Position — Seite und " +
          "Beträge ggf. unvollständig" +
          (otHasPnl
            ? "; im Fenster bereits realisiert: " + (otPnl >= 0 ? "+" : "") + fmt(otPnl, 2)
            : "") + ".";
      } else {
        msg = head + " — noch kein Realized-PnL, Position läuft weiter.";
      }
      html += '<div class="trades-empty">' + msg + "</div>";
    }
    return html;
  }

  /** Raw per-fill ledger — the pre-existing "Trades" view (C3-08), unchanged,
   *  now just extracted into its own render fn so it can sit behind the
   *  "Einzel-Fills" sub-tab next to the new round-trip view. */
  function renderFillsListHtml(fills) {
    const sorted = fills.slice().sort(function (a, b) {
      return (Number(b.time) || 0) - (Number(a.time) || 0);
    });
    return (
      '<div class="trades-list">' +
      sorted
        .map(function (f) {
          const side = f.side === "buy" ? "buy" : "sell";
          const sym = String(f.symbol || state.symbol || "—").split("_")[0];
          const t = Number(f.time);
          const iso = Number.isFinite(t) && t > 0 ? new Date(t).toISOString() : null;
          const pnl = Number(f.closed_pnl);
          const hasPnl = Number.isFinite(pnl) && pnl !== 0;
          const pnlCls = hasPnl ? (pnl > 0 ? "pnl-pos" : "pnl-neg") : "";
          const pnlTxt = hasPnl ? (pnl >= 0 ? "+" : "") + fmt(pnl, 2) : "—";
          return (
            '<div class="trade-row">' +
            '<span class="trade-sym">' + escapeHtml(sym) + "</span>" +
            '<span class="trade-side ' + side + '">' +
            (side === "buy" ? "BUY" : "SELL") + "</span>" +
            '<span class="trade-sz">' + fmt(f.sz, 4) + "</span>" +
            '<span class="trade-px">' + fmt(f.px, 4) + "</span>" +
            '<span class="trade-pnl ' + pnlCls + '">' + escapeHtml(pnlTxt) + "</span>" +
            '<span class="trade-time">' + (iso ? escapeHtml(relTime(iso)) : "") + "</span>" +
            "</div>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  /** Delegated click handler for #trades-body — only the sub-tab toggle
   *  lives here (read-only view switch, no order/SL path touched). */
  function onTradesBodyClick(ev) {
    const btn = ev.target.closest("[data-rt-subtab]");
    if (!btn) return;
    const key = btn.getAttribute("data-rt-subtab");
    if (key !== "roundtrips" && key !== "fills") return;
    if (state.tradesSubTab === key) return;
    state.tradesSubTab = key;
    renderTrades();
  }

  /** Trades tab: executed fills (state.fills) as a compact ledger, PLUS
   *  (Task 40 / N3-14 Stufe 1) a folded round-trip view — a real trade log
   *  with realized PnL/fees per round-trip, not just a list of executions.
   *  Round-trip folding is HL-only for now (MEXC stays Stufe 2, out of
   *  scope per the task brief) even though MEXC's /api/fills already
   *  reports supported=true — the raw fill ledger still works there. */
  function renderTrades() {
    const el = $("trades-body");
    if (!el) return;
    if (!state._tradesWired) {
      el.addEventListener("click", onTradesBodyClick);
      state._tradesWired = true;
    }
    const symLabel = String(state.symbol || "—").split("_")[0];
    const fills = Array.isArray(state.fills)
      ? state.fills.filter(function (f) { return symMatch(f.symbol, state.symbol); })
      : [];
    if (!fills.length) {
      el.className = "trades-body muted";
      el.innerHTML =
        '<div class="trades-empty">Noch keine ausgeführten Trades für ' +
        escapeHtml(symLabel) + ".</div>";
      return;
    }

    const hl = isHlExchange();
    const sub = hl && state.tradesSubTab === "fills" ? "fills" : hl ? "roundtrips" : "fills";
    let html = "";
    if (hl) {
      html +=
        '<div style="display:flex;border-bottom:1px solid var(--hairline,currentColor);margin-bottom:8px;">' +
        _rtSubtabBtn("roundtrips", "Round-Trips", sub === "roundtrips") +
        _rtSubtabBtn("fills", "Einzel-Fills", sub === "fills") +
        "</div>";
    } else {
      html +=
        '<div class="trades-empty" style="padding-bottom:0;">' +
        "Round-Trip-Auswertung aktuell nur für Hyperliquid (MEXC folgt in Stufe 2) " +
        "— Einzel-Fills unten." +
        "</div>";
    }
    html += sub === "roundtrips" ? renderRoundTripsHtml(fills, symLabel) : renderFillsListHtml(fills);

    el.className = "trades-body";
    el.innerHTML = html;
  }

  /** Tabbed data panel: toggle the active pane, contextual action buttons and
   *  trigger an immediate render/refresh of the selected tab's data. Polling
   *  keeps writing into hidden panes; they simply show when re-selected. */
  function switchDataTab(name) {
    const tabs = document.querySelectorAll(".data-tab");
    if (!tabs.length) return;
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
    } else if (name === "journal") {
      loadJournal();
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

  /** POST /api/journal/clear: wipes the journal_entries measurement table.
   *  Deliberately separate from clearHistory()/history-clear — the journal
   *  survives that reset by design; this is the explicit opt-in. */
  async function clearJournal() {
    if (state.journalClearBusy) return;
    const text =
      "Journal (KI-Shadow-Book) wirklich leeren?\n\n" +
      "Löscht alle geloggten Analyse-Einträge samt Win/Loss-Auswertung. " +
      "Das Journal überlebt normalerweise einen Historie-Reset — nur dieser " +
      "Button löscht es. Nicht umkehrbar.";
    if (!window.confirm(text)) return;
    state.journalClearBusy = true;
    try {
      const res = await apiFetch("/api/journal/clear", {
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
      showToast(
        "Journal geleert: " + fmt(data.deleted, 0) + " Einträge",
        "ok"
      );
      loadJournal();
    } catch (err) {
      showToast(
        "Journal leeren fehlgeschlagen: " + (err && err.message),
        "err"
      );
    } finally {
      state.journalClearBusy = false;
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
      state.proposalAt = null;
      drawProposalLines();
      setApplyEnabled(false);
      // U2-05: a stale proposal must not leave behind a TP2/TP3 reminder or
      // a still-ticking drift timer for a panel that no longer exists.
      if (state._proposalDriftTimer) {
        clearInterval(state._proposalDriftTimer);
        state._proposalDriftTimer = null;
      }
      _clearTpLadderReminder();
      return;
    }

    const p = data.proposal;
    const anno = data.annotations || {};
    state.proposal = p;
    state.proposalSymbol = data.symbol || state.symbol;
    state.proposalApplied = false;
    // U2-04: the age shown must reflect the ORIGINAL analysis time, not the
    // moment this render runs — a cache hit (data.cached_age_s) or the "neu"
    // refresh button both re-render an already-aged proposal.
    state.proposalAt =
      Date.now() - (data.cached_age_s != null ? Number(data.cached_age_s) * 1000 : 0);
    const stayOut = p.action === "STAY_OUT";
    setApplyEnabled(!stayOut);
    drawProposalLines();

    const levels = p.key_levels || {};
    const mgmt = p.management || {};
    const fmtN = (v) => (v == null || v === "" ? "—" : fmt(v, 6));

    // R-multiples for the TP tiles (reward per unit of risk)
    const rMult = (tp) => {
      const r = rMultiple(tp, p.entry_price, p.stop_loss);
      return r == null ? "" : "+" + fmt(r, 1) + "R";
    };

    // Header: big action badge + symbol
    let html =
      '<div class="an-head">' +
      '<span class="proposal-action action-' + escapeHtml(p.action || "") + '">' +
      escapeHtml((p.action || "—").replace(/_/g, " ")) + "</span>" +
      '<span class="an-sym">' + escapeHtml(state.proposalSymbol || state.symbol || "") + "</span>" +
      '<span class="an-lev">' + escapeHtml(p.recommended_leverage || "") + "</span>" +
      (data.cached
        ? '<span class="an-cache-badge" title="Aus dem Cache — keine erneute KI-Anfrage">' +
          "gecacht vor " + escapeHtml(String(data.cached_age_s != null ? data.cached_age_s : 0)) + "s" +
          ' <button type="button" class="an-cache-refresh">neu</button></span>'
        : "") +
      "</div>";

    // U2-03/U2-04: subtle sub-header — resolved provider/model (so a silent
    // fallback is visible) on the left, live age/drift on the right. The
    // drift span itself is filled in by updateProposalDriftDisplay() right
    // after render (and on a timer) — it needs state.lastPx, which can move
    // between renders, so it is NOT baked into this static html string.
    const providerLabel = _llmProviderLabel(data.provider);
    html +=
      '<div class="an-subhead">' +
      (providerLabel || data.model
        ? '<span class="an-provider-badge" title="Von der KI-Analyse tatsächlich verwendeter Provider/Modell">' +
          escapeHtml(providerLabel) +
          (data.model ? " · " + escapeHtml(String(data.model)) : "") +
          "</span>" +
          (data.provider_fallback
            ? '<span class="an-fallback-flag" title="Konfigurierter Provider war nicht einsatzbereit — automatischer Fallback auf diesen Provider">⚠ Fallback</span>'
            : "")
        : "") +
      '<span id="proposal-drift" class="an-drift"></span>' +
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
              p.rrr != null && p.rrr >= minRrr() ? "solide" : "knapp", "tile-rrr");
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

    // U2-01: "Größe (KI)" — the conviction-tier sizing note from Task 14b
    // (high -> full risk budget / medium -> ~1/2 / low -> ~1/4). Was computed
    // by the LLM but never surfaced anywhere in the panel.
    const sizingNote = !stayOut ? String(p.position_sizing_note || "").trim() : "";

    // Management (structured invalidation price first, then any free-text)
    const invPx = Number(p.invalidation_price);
    const hasInvPx = Number.isFinite(invPx) && invPx > 0;
    if (sizingNote || mgmt.move_sl_to_be || mgmt.early_invalidation || hasInvPx) {
      html +=
        '<div class="an-mgmt">' +
        (sizingNote ? '<div><b>Größe (KI):</b> ' + escapeHtml(sizingNote) + "</div>" : "") +
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
    const cacheRefreshBtn = body.querySelector(".an-cache-refresh");
    if (cacheRefreshBtn) {
      cacheRefreshBtn.addEventListener("click", function () {
        runAnalyze(true);
      });
    }

    // U2-04: live drift (age + entry-vs-lastPx) — filled in immediately, then
    // refreshed on a timer (mirrors the scan-age display pattern) so it keeps
    // moving with state.lastPx between analyses instead of freezing at the
    // value computed at analysis time.
    if (state._proposalDriftTimer) clearInterval(state._proposalDriftTimer);
    updateProposalDriftDisplay();
    state._proposalDriftTimer = setInterval(updateProposalDriftDisplay, 15000);
  }

  const PROPOSAL_DRIFT_STALE_MIN = 5;
  const PROPOSAL_DRIFT_STALE_PCT = 0.5;

  /** Refresh just the "Analyse Nm alt · Preis ±X%" header on the
   *  already-rendered proposal panel (called on a timer + right after
   *  render) — never rebuilds the whole panel. Past either staleness
   *  threshold the line turns amber with a "neu analysieren" hint (U2-04). */
  function updateProposalDriftDisplay() {
    const el = $("proposal-drift");
    if (!el) return;
    const p = state.proposal;
    if (!p || state.proposalAt == null) {
      el.textContent = "";
      el.classList.remove("an-drift-stale");
      return;
    }
    const ageMin = (Date.now() - state.proposalAt) / 60000;
    const ageLabel = ageMin < 1 ? "gerade eben" : Math.floor(ageMin) + " min alt";
    let driftPct = null;
    if (p.entry_price != null && state.lastPx != null && Number(p.entry_price) > 0) {
      driftPct =
        ((Number(state.lastPx) - Number(p.entry_price)) / Number(p.entry_price)) * 100;
    }
    const driftLabel = driftPct != null ? (driftPct >= 0 ? "+" : "") + fmt(driftPct, 2) + " %" : "—";
    const stale =
      ageMin > PROPOSAL_DRIFT_STALE_MIN ||
      (driftPct != null && Math.abs(driftPct) > PROPOSAL_DRIFT_STALE_PCT);
    el.textContent =
      "Analyse " + ageLabel + " · Preis " + driftLabel + (stale ? " · neu analysieren" : "");
    el.classList.toggle("an-drift-stale", stale);
  }

  /** U2-03: human label for the resolved LLM provider (mirrors the llm-label
   *  mapping used for the health-check dot). Falls back to the raw string
   *  (still escaped by the caller) so an unrecognized provider never renders
   *  as nothing. */
  function _llmProviderLabel(provider) {
    const p = String(provider || "").trim().toLowerCase();
    if (p === "xai" || p === "grok") return "xAI";
    if (p === "claude" || p === "anthropic") return "Claude";
    if (p === "openai" || p === "codex") return "OpenAI";
    if (p === "ollama" || p === "local") return "Ollama";
    return provider ? String(provider) : "";
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

  // A3-01: escapeHtml moved to utils.js (pure helper, loaded as a global
  // before app.js). Call sites unchanged.

  /** Focus the KI dropdown so the trader can switch provider in one click
   *  (used by the "⚠" credit/rate-limit error affordance below). */
  function focusLlmSelect() {
    const sel = $("llm-select");
    if (sel) {
      sel.focus();
      if (typeof sel.showPicker === "function") {
        try {
          sel.showPicker();
        } catch (_) {}
      }
    }
  }

  /** True when a message is a categorized provider error (app/llm/client.py
   *  ._categorize_provider_http_error — auth/credits/rate-limit), which is
   *  always prefixed with "⚠" and should get the prominent warn-banner
   *  treatment instead of a plain error line. */
  function isLlmWarnMessage(text) {
    return String(text == null ? "" : text).indexOf("⚠") === 0;
  }

  /** Shared markup for a categorized "⚠ ..." provider error: the warn banner
   *  plus a "KI wechseln" button. Callers MUST wire the button themselves via
   *  wireKiSwitchButtons() once this HTML is attached to the DOM (this
   *  returns a string, not a live node, so it works both for a direct
   *  innerHTML assignment and when embedded inside a larger template
   *  string). Text is escaped here — callers must not escape it again. */
  function llmWarnBannerHtml(text) {
    return (
      '<div class="error-text error-llm-warn">' + escapeHtml(text) + "</div>" +
      '<button type="button" class="btn-ki-switch">KI wechseln</button>'
    );
  }

  /** Wire every ".btn-ki-switch" button inside `container` to focus the KI
   *  dropdown. Safe to call repeatedly on re-rendered markup (querySelectorAll
   *  only ever sees the buttons currently in the DOM). */
  function wireKiSwitchButtons(container) {
    if (!container) return;
    container.querySelectorAll(".btn-ki-switch").forEach(function (btn) {
      btn.addEventListener("click", focusLlmSelect);
    });
  }

  function showProposalError(msg) {
    const body = $("proposal-body");
    if (!body) return;
    body.className = "proposal-body";
    const text = String(msg == null ? "" : msg);
    if (isLlmWarnMessage(text)) {
      // Credit/rate-limit style provider error (app/llm/client.py categorizes
      // these) — render prominently with a one-click "KI wechseln" affordance
      // instead of the plain error line.
      body.innerHTML = llmWarnBannerHtml(text);
      wireKiSwitchButtons(body);
    } else {
      body.innerHTML = '<p class="error-text">' + escapeHtml(text) + "</p>";
    }
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
    // Defense-in-depth (H-1): even though runAnalyze now discards stale
    // cross-symbol responses, never let a proposal be applied to a ticket
    // for a different active symbol than the one it was generated for.
    if (!symMatch(state.proposalSymbol, state.symbol)) {
      setTicketError(
        "Vorschlag gehört zu " + state.proposalSymbol + ", nicht zum aktiven Symbol."
      );
      return;
    }

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
    // Both notices below share the single-slot toast. Collect them and emit ONE
    // combined toast at the end — otherwise the second showToast() overwrites
    // the first and a warning is silently lost (e.g. the LIMIT notice would
    // vanish behind the TP2/TP3 notice in the common tiered-TP + pullback case).
    const applyNotices = [];
    // The proposal turned the ticket into a LIMIT order (pullback entry). Make
    // that unmistakable — otherwise the trader sends a resting limit thinking
    // they are in the market now (exactly the AVAX confusion).
    if (typeEl && typeEl.value === "limit" && p.entry_price != null) {
      applyNotices.push(
        "⏳ Als LIMIT bei " + fmt(p.entry_price, 4) + " übernommen — die Order " +
          "wartet, bis der Kurs dieses Niveau erreicht. Für sofortigen Einstieg " +
          'auf „Market" wechseln.'
      );
    }

    // U2-02: the ticket only has ONE take-profit field — TP1 goes into it as
    // before, but TP2/TP3 must never just silently vanish. Surface them as a
    // read-only reminder line in the ticket + a toast, instead of building a
    // full multi-TP order feature.
    if (p.tp2 != null || p.tp3 != null) {
      const parts = [];
      if (p.tp2 != null) parts.push("TP2 " + fmt(p.tp2, 6));
      if (p.tp3 != null) parts.push("TP3 " + fmt(p.tp3, 6));
      const reminderEl = _ensureTpLadderReminderEl();
      if (reminderEl) {
        reminderEl.textContent =
          "KI-Leiter zusätzlich: " + parts.join(" · ") +
          " — nicht als Order übernommen, ggf. manuell nachziehen.";
        reminderEl.classList.remove("hidden");
      }
      applyNotices.push("TP2/TP3 nicht als Order übernommen — siehe Hinweis im Ticket.");
    } else {
      _clearTpLadderReminder();
    }

    if (applyNotices.length) {
      showToast(applyNotices.join("   ·   "), null);
    }

    // Core KI lines are now represented by the ticket lines — keep only levels
    state.proposalApplied = true;
    drawProposalLines();
    drawTicketLines();
  }

  /** Read-only "TP2/TP3 not applied" line under the ticket's TP field
   *  (U2-02). Created lazily so no template change is needed; idempotent —
   *  a repeat call reuses the same element instead of stacking duplicates. */
  function _ensureTpLadderReminderEl() {
    let el = $("tp-ladder-reminder");
    if (el) return el;
    const anchor = $("risk-readout") || $("order-form");
    if (!anchor || !anchor.parentNode) return null;
    el = document.createElement("p");
    el.id = "tp-ladder-reminder";
    el.className = "hint muted tp-ladder-reminder hidden";
    anchor.parentNode.insertBefore(el, anchor);
    return el;
  }

  function _clearTpLadderReminder() {
    const el = $("tp-ladder-reminder");
    if (!el) return;
    el.textContent = "";
    el.classList.add("hidden");
  }

  async function runAnalyze(force) {
    if (state.analyzeBusy) {
      // Reached when triggered programmatically (e.g. a scan-chip click)
      // while a manual "Analysieren" click is already in flight — the button
      // itself is disabled during a manual click, so this path otherwise
      // fires silently and leaves the trader waiting for nothing (M-2).
      showToast("Analyse läuft bereits — bitte warten.", null);
      return;
    }
    const btn = $("btn-analyze");
    // A3-11: symbol/tf come from state (the single source of truth) — never the
    // raw input text nor the .tf-btn.active DOM class.
    const symbol = state.symbol || "BTC_USDT";
    const tf = state.tf || "15m";
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

    // If this coin was surfaced by the market scanner, hand its verdict to the
    // analyzer so it confirms/refutes the screen instead of re-deriving blind
    // (B1 — closes the "scanner finds it, analysis says STAY_OUT" gap).
    const symU = String(symbol).toUpperCase().trim();
    let scannerVerdict = null;
    const scanRows = (state.scanResults && state.scanResults.results) || [];
    const hit = scanRows.find(function (r) {
      return symMatch(r.symbol, symU);
    });
    if (hit) {
      scannerVerdict = {
        bias: hit.bias,
        setup: hit.setup,
        key_level: hit.key_level != null ? hit.key_level : hit.entry,
        score: hit.score,
        // S2-04: pass the screener's own rationale through so the analyzer
        // sees WHY the coin was flagged (truncated — free LLM text, not a
        // structured field; server-side sanitizer truncates again anyway).
        reason: hit.reason ? String(hit.reason).slice(0, 120) : undefined,
      };
    }

    // Capture the symbol this request was issued for; the LLM call can take
    // 10-30s and the trader may switch coins before it resolves. Compared
    // against state.symbol at resolve time below to discard a stale response
    // instead of rendering/enabling Apply for the wrong coin (H-1).
    const reqSymbol = symU;

    try {
      // A3-08: abortable READ — a coin switch (loadMarket) aborts this
      // in-flight analyze so a discarded LLM call stops burning tokens.
      const res = await apiFetchAbortable("analyze", "/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: symU,
          tf: tf,
          htf: htf,
          scanner_verdict: scannerVerdict,
          force: !!force,
        }),
      });
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }
      if (!symMatch(reqSymbol, state.symbol)) {
        // Symbol changed while this analyze was in flight — drop the
        // response entirely: don't render it, don't show an error for it,
        // don't touch the (now different coin's) proposal/ticket state.
        return;
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
      // A3-08: an aborted analyze (coin switch) is intentional — the panel was
      // already torn down by loadMarket; don't surface it as a network error.
      if (err && err.name === "AbortError") return;
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
      const res = await apiFetch("/api/symbols");
      const data = await res.json();
      if (!Array.isArray(data.symbols) || !data.symbols.length) return;
      state.allSymbols = data.symbols.map(function (s) {
        return String(s).toUpperCase();
      });
      ensureSymbolOption(state.symbol); // A3-11: active symbol from state
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

    // A3-11: update state FIRST (single source of truth), THEN reflect to the
    // DOM. loadMarket() re-affirms state.symbol, but setting it here makes the
    // active symbol authoritative the instant the switch begins. (The tab-rename
    // above still ran against the PREVIOUS state.symbol, as it must.)
    state.symbol = sym;
    const input = $("symbol-input");
    if (input) {
      input.value = sym;
      input.dataset.touched = "1";
    }
    // tf is read from state (the single source), NOT the .tf-btn.active class.
    const tf = state.tf || "15m";
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

  /** A3-11: the ONE writer of state.tf. Updates state (single source of truth)
   *  FIRST, derives the paired HTF, reflects the active button to the DOM
   *  (output only), then reloads the active symbol at the new tf. Every tf entry
   *  point (the .tf-btn bar) routes through here — nothing reads .tf-btn.active
   *  as a competing truth anymore. */
  function setTf(tf) {
    tf = String(tf || state.tf || "15m");
    state.tf = tf;
    // HTF always higher than LTF when possible (same mapping as before).
    let htf = "1H";
    if (tf === "5m" || tf === "15m") htf = "1H";
    else if (tf === "1H") htf = "4H";
    else if (tf === "4H") htf = "1D";
    else if (tf === "1D") htf = "1D";
    state.htf = htf;
    // Reflect state → DOM (input/output only): mark exactly the active button.
    document.querySelectorAll(".tf-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tf") === tf);
    });
    loadMarket(state.symbol, state.tf, state.htf);
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
      const raw = localStorage.getItem(PERSIST.OPEN_TABS);
      const arr = raw ? JSON.parse(raw) : null;
      state.openTabs =
        Array.isArray(arr) && arr.length ? arr : [state.symbol || "BTC_USDT"];
    } catch (_) {
      state.openTabs = [state.symbol || "BTC_USDT"];
    }
  }

  function saveOpenTabs() {
    try {
      localStorage.setItem(PERSIST.OPEN_TABS, JSON.stringify(state.openTabs));
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
    const inChart = state.activeView !== "overview";
    const active = String(state.symbol || "").toUpperCase();
    // A3-10/V3-07: the tab bar was rebuilt on every 30s poll. Fingerprint the
    // only things that change its markup — view, active symbol, tab order and
    // each tab's long/short dot — and skip the rebuild entirely when nothing
    // moved, so a poll no longer thrashes the bar (or drops a mid-click).
    const fp =
      (inChart ? "C" : "O") + "|" + active + "|" +
      state.openTabs
        .map(function (s) {
          return s + ":" + (tabPositionSide(s) || "");
        })
        .join(",");
    if (state._symbolTabsFp === fp) return;
    state._symbolTabsFp = fp;
    bar.querySelectorAll(".symbol-tab").forEach(function (el) {
      el.remove();
    });

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
      const raw = localStorage.getItem(PERSIST.WATCHLIST);
      const arr = raw ? JSON.parse(raw) : null;
      state.watchlist = Array.isArray(arr) ? arr : [];
    } catch (_) {
      state.watchlist = [];
    }
  }
  function saveWatchlist() {
    try {
      localStorage.setItem(PERSIST.WATCHLIST, JSON.stringify(state.watchlist));
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

  // A3-01: relTime moved to utils.js (pure helper, loaded as a global before
  // app.js). Call sites unchanged.

  /** Render cached headlines. EVERY feed string goes through escapeHtml
   *  (feeds are untrusted), links are http(s)-whitelisted + noopener.
   *  V3-04: a "Veraltet" marker in the header when the server served a stale
   *  cached payload (all feeds down), and an honest error message instead of
   *  "keine Schlagzeilen" when there are zero items AND feed errors — a total
   *  outage must never read like a quiet news day. */
  function renderNews() {
    const box = $("overview-news");
    if (!box) return;
    const staleBadge = state.newsStale
      ? '<span class="news-stale-badge" title="Feeds gerade nicht erreichbar — letzter bekannter Stand">Veraltet</span>'
      : "";
    const head =
      '<div class="news-head"><h3 class="overview-subhead">Nachrichten</h3>' +
      staleBadge + "</div>";
    const all = state.newsItems || [];
    if (!all.length) {
      const errs = state.newsErrors || [];
      const body = errs.length
        ? '<div class="news-empty news-error">Newsfeeds nicht erreichbar: ' +
          escapeHtml(errs.join("; ")) + "</div>"
        : '<div class="news-empty">Keine aktuellen Schlagzeilen.</div>';
      box.innerHTML = head + body;
      return;
    }
    // Curated desk, not a log: a handful of items, the freshest featured.
    const items = all.slice(0, 14);
    const leadCount = Math.min(3, items.length);

    // Coins the user actually tracks — every watchlist entry + every open
    // position — reduced to their base coin (BTC_USDT → BTC). Used to flag
    // headlines mentioning something the user holds/watches.
    const watched = [];
    const pushCoin = function (sym) {
      const base = String(sym || "").toUpperCase().split("_")[0].trim();
      if (base && base.length >= 2 && watched.indexOf(base) === -1) watched.push(base);
    };
    (state.watchlist || []).forEach(pushCoin);
    ((state.account && state.account.positions) || []).forEach(function (p) {
      if (Math.abs(Number(p.hold_vol) || 0) > 0) pushCoin(p.symbol);
    });

    // Whole-word, case-insensitive watched-coin mentions in a text blob.
    function coinsMentioned(text) {
      const t = String(text || "");
      return watched.filter(function (c) {
        const esc = c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        return new RegExp("\\b" + esc + "\\b", "i").test(t);
      });
    }

    // Source → subtle, distinct token-based color chip.
    function srcChip(source) {
      const s = String(source || "");
      const key = s.toLowerCase().replace(/[^a-z]/g, "");
      const known = key === "coindesk" || key === "cointelegraph" || key === "decrypt";
      const cls = known ? "news-src-" + key : "news-src-other";
      return '<span class="news-src ' + cls + '">' + escapeHtml(s || "—") + "</span>";
    }

    // Build one clickable card. EVERY feed string is escaped (feeds are
    // untrusted); links are http(s)-whitelisted + rel="noopener noreferrer".
    function card(it, kind) {
      const url = String((it && it.url) || "");
      const safe = /^https?:\/\//i.test(url) ? url : "";
      const hits = coinsMentioned(
        String((it && it.title) || "") + " " + String((it && it.summary) || "")
      );
      const hit = hits.length > 0;
      const coinBadge = hit
        ? '<span class="news-coin">● ' + escapeHtml(hits.slice(0, 2).join(" ")) + "</span>"
        : "";
      const cls =
        (kind === "lead" ? "news-lead" : "news-row") + (hit ? " news-hit" : "");
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
        coinBadge +
        srcChip((it && it.source) || "") +
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

    let html = head + '<div class="news-leads">' + leads + "</div>";
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
      const res = await apiFetch("/api/news");
      if (res.ok) {
        const data = await res.json();
        state.newsItems = data.items || [];
        // V3-04: surface data.errors/stale instead of swallowing them — a
        // total feed outage must read as an honest error/stale marker, never
        // as a quiet "no headlines".
        state.newsErrors = data.errors || [];
        state.newsStale = !!data.stale;
        state._newsLast = Date.now();
      }
    } catch (e) {
      console.error("refreshNews", e);
    } finally {
      state._newsBusy = false;
    }
    if (state.activeView === "overview") renderNews();
  }

  /** Hardened against double-fetches: a busy guard plus a ~60s min-interval
   *  (V3-03) so the account poll (30s) and the overview timer (30s) landing
   *  close together can't fire two /api/mini requests back to back. Reuses
   *  the cached candles (fresh position/PnL badges still redraw from
   *  state.account). The server additionally caches /api/mini for
   *  MINI_CACHE_TTL_S, so a fresh tab/second browser hitting the same
   *  symbols within that window doesn't re-fetch from the exchange either. */
  async function refreshOverview() {
    if (state.activeView !== "overview") return; // never redraw in background
    const syms = overviewSymbols();
    if (!syms.length) {
      renderOverviewGrid();
      return;
    }
    if (state._miniBusy) return;
    const now = Date.now();
    // V3-03: candle-refresh cadence relaxed to ~60s (was 10s) — the server
    // now caches /api/mini for MINI_CACHE_TTL_S anyway, and the mini-charts
    // don't need faster-than-a-minute candles. Position/PnL badges still
    // redraw on every call below via the cached candles + fresh state.account.
    if (now - (state._miniLast || 0) < 60000 && Object.keys(state.overviewData).length) {
      renderOverviewGrid(); // fresh enough — reuse cached candles, update PnL badges
      return;
    }
    state._miniBusy = true;
    let fetchFailed = false;
    try {
      const res = await apiFetch(
        "/api/mini?symbols=" + encodeURIComponent(syms.join(",")) + "&tf=15m&limit=96"
      );
      if (res.ok) {
        const data = await res.json();
        (data.results || []).forEach(function (r) {
          state.overviewData[String(r.symbol || "").toUpperCase()] = r;
        });
        // V3-02: data.errors used to be swallowed entirely — a failed coin's
        // tile just kept showing its last candles with no indication
        // anything was wrong. Map "SYM: reason" entries onto overviewErrors
        // so the tile can show an honest error badge instead, and clear the
        // entry for any requested symbol that came back clean this round.
        const errBySym = {};
        (data.errors || []).forEach(function (e) {
          const m = /^([^:]+):\s*([\s\S]*)$/.exec(String(e || ""));
          if (m) errBySym[m[1].trim().toUpperCase()] = m[2].trim();
        });
        syms.forEach(function (s) {
          if (errBySym[s]) state.overviewErrors[s] = errBySym[s];
          else delete state.overviewErrors[s];
        });
        state._miniLast = Date.now();
      } else {
        fetchFailed = true;
      }
    } catch (e) {
      console.error("refreshOverview", e);
      fetchFailed = true;
    } finally {
      state._miniBusy = false;
    }
    if (fetchFailed) {
      // Total outage: a tile with no candle data yet must say so rather than
      // sit blank/frozen with no explanation. A tile that already has data
      // keeps showing it (better a slightly stale chart than none), but a
      // fresh request must not fail silently.
      syms.forEach(function (s) {
        if (!state.overviewData[s]) state.overviewErrors[s] = "Abruf fehlgeschlagen";
      });
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

  /** Compute the render model for one overview tile: price/change/pnl badges,
   *  marks, candles, plus two fingerprints — dataFp (badges/price) and canvasFp
   *  (candles+marks). Shared by buildMiniTile (create) and updateMiniTile
   *  (in-place patch) so both read one source of truth. */
  function _miniTileModel(sym, chartColors, cs) {
    const key = sym.toUpperCase();
    const d = state.overviewData[key] || {};
    const pos = positionFor(sym);
    const isWatch = state.watchlist.indexOf(key) !== -1 && !pos;
    const err = state.overviewErrors && state.overviewErrors[key];
    const last = Number(d.last_price);
    const chg = Number(d.change_pct);
    const chgCls = Number.isFinite(chg) ? (chg >= 0 ? "pos-pos" : "pos-neg") : "";
    const chgTxt = Number.isFinite(chg) ? (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%" : "—";

    let pnl = null;
    if (pos) {
      // `cs` is the ACTIVE chart symbol's contractSize. Reusing it to recompute
      // PnL for every tile would be wrong on MEXC (different contract sizes,
      // F-10). Only the active symbol's own tile uses that local recompute;
      // every other tile uses the exchange's own unrealized_pnl.
      const isActiveSym = symMatch(sym, state.symbol);
      if (isActiveSym && Number.isFinite(last)) {
        const entry = Number(pos.entry_price);
        const vol = Number(pos.hold_vol);
        const short = String(pos.side || "").toLowerCase() === "short";
        const pcs = positionContractSize(pos.contract_size, cs);
        if (Number.isFinite(entry) && Number.isFinite(vol)) {
          pnl = computePnl(last, entry, vol, pcs, short);
        }
      } else {
        pnl = Number(pos.unrealized_pnl);
      }
    }

    const marks = [];
    if (pos) {
      // A3-01: route through markerKey() so a manual SL/TP still draws even when
      // the tile's display symbol is a typed full pair on Hyperliquid.
      const mk = state.tradeMarkers && state.tradeMarkers[markerKey(key)];
      marks.push({ price: Number(pos.entry_price), color: chartColors.level });
      if (mk && mk.sl) marks.push({ price: Number(mk.sl), color: chartColors.short });
      if (mk && mk.tp) marks.push({ price: Number(mk.tp), color: chartColors.long });
    }
    const candles = d.candles || [];
    const lastC = candles.length ? candles[candles.length - 1] : null;
    const canvasFp = [
      candles.length,
      lastC ? lastC.time : "",
      lastC ? lastC.close : "",
      marks.map(function (m) { return Number.isFinite(m.price) ? m.price : "-"; }).join(","),
    ].join("|");
    const dataFp = [
      Number.isFinite(last) ? fmt(last, 6) : "-",
      chgTxt,
      pnl != null && Number.isFinite(pnl) ? fmt(pnl, 2) : "-",
      canvasFp,
    ].join("~");

    return {
      key: key, d: d, pos: pos, isWatch: isWatch, err: err,
      last: last, chgCls: chgCls, chgTxt: chgTxt, pnl: pnl,
      marks: marks, candles: candles, canvasFp: canvasFp, dataFp: dataFp,
    };
  }

  function _miniPnlHtml(pnl) {
    if (pnl == null || !Number.isFinite(pnl)) return "";
    const pc = pnl >= 0 ? "pos-pos" : "pos-neg";
    return '<span class="mini-pnl ' + pc + '">' + (pnl >= 0 ? "+" : "") + fmt(pnl, 2) + "</span>";
  }

  /** Build one mini-tile DOM node for `sym`. Reused for BOTH the positions group
   *  and the watchlist group (V3-06). Carries data-datafp/data-canvasfp so a
   *  later poll can patch badges/price in place and only redraw the canvas when
   *  candle data actually changed (A3-10/V3-07). */
  function buildMiniTile(sym, chartColors, cs) {
    const m = _miniTileModel(sym, chartColors, cs);
    const tile = document.createElement("div");
    tile.className =
      "mini-tile" +
      (m.pos ? " pos " + String(m.pos.side || "").toLowerCase() : "") +
      (m.err ? " mini-tile-error" : "");
    tile.setAttribute("data-symbol", m.key);
    tile.setAttribute("data-datafp", m.dataFp);
    tile.setAttribute("data-canvasfp", m.canvasFp);

    tile.innerHTML =
      (m.isWatch ? '<button type="button" class="mini-remove" title="Entfernen">×</button>' : "") +
      '<div class="mini-head"><span class="mini-sym">' + escapeHtml(m.key) + "</span>" +
      '<span class="mini-price">' + (Number.isFinite(m.last) ? fmt(m.last, 6) : "—") + "</span></div>" +
      '<div class="mini-badges">' + _miniPnlHtml(m.pnl) +
      '<span class="mini-chg ' + m.chgCls + '">' + m.chgTxt + "</span></div>" +
      // V3-02: a failed tile says so — never a silently frozen chart.
      (m.err
        ? '<div class="mini-err-badge" title="' + escapeHtml(m.err) + '">⚠ ' +
          escapeHtml(m.err) + "</div>"
        : "") +
      '<canvas class="mini-canvas"></canvas>';

    const cv = tile.querySelector(".mini-canvas");
    // Draw after insertion so the canvas has a measured width.
    requestAnimationFrame(function () {
      drawMiniCandles(cv, m.candles, m.marks);
    });

    tile.addEventListener("click", function (e) {
      if (e.target.closest(".mini-remove")) {
        removeWatch(m.key);
        return;
      }
      goToSymbol(m.key, { newTab: true });
    });
    return tile;
  }

  /** A3-10/V3-07: patch an existing tile's price/change/PnL in place and redraw
   *  its canvas ONLY when the candle/marks data changed — no DOM rebuild while
   *  the grid structure (which coins, which group) is unchanged. */
  function updateMiniTile(tile, m) {
    if (tile.getAttribute("data-datafp") === m.dataFp) return;
    tile.setAttribute("data-datafp", m.dataFp);
    const priceEl = tile.querySelector(".mini-price");
    if (priceEl) priceEl.textContent = Number.isFinite(m.last) ? fmt(m.last, 6) : "—";
    const badges = tile.querySelector(".mini-badges");
    if (badges) {
      badges.innerHTML =
        _miniPnlHtml(m.pnl) + '<span class="mini-chg ' + m.chgCls + '">' + m.chgTxt + "</span>";
    }
    if (tile.getAttribute("data-canvasfp") !== m.canvasFp) {
      tile.setAttribute("data-canvasfp", m.canvasFp);
      const cv = tile.querySelector(".mini-canvas");
      requestAnimationFrame(function () {
        drawMiniCandles(cv, m.candles, m.marks);
      });
    }
  }

  /** V3-06: positions are their own group at the TOP of the grid, sorted by
   *  known risk (largest first) — money at stake leads, not insertion order.
   *  Positions without a known risk yet (no SL synced) sort after those with
   *  a number, so the group stays stable instead of jumping around. */
  function renderOverviewGrid() {
    const chartColors = getChartColors();
    const grid = $("overview-grid");
    if (!grid) return;
    const syms = overviewSymbols();
    if (!syms.length) {
      if (state._gridStructFp !== "EMPTY") {
        state._gridStructFp = "EMPTY";
        grid.innerHTML =
          '<div class="overview-empty">Keine offenen Positionen. Coins über „+ Beobachten" hinzufügen.</div>';
      }
      return;
    }
    const cs = contractSize();

    const posSyms = [];
    const watchSyms = [];
    syms.forEach(function (s) {
      if (positionFor(s)) posSyms.push(s);
      else watchSyms.push(s);
    });
    posSyms.sort(function (a, b) {
      const ra = positionRiskUsdt(positionFor(a));
      const rb = positionRiskUsdt(positionFor(b));
      if (ra == null && rb == null) return 0;
      if (ra == null) return 1;
      if (rb == null) return -1;
      return rb - ra;
    });

    // A3-10/V3-07: the grid was rebuilt from scratch on every poll. Fingerprint
    // its STRUCTURE — group membership, order, per-tile error state — and when
    // it's unchanged just patch each tile's price/PnL/canvas in place instead
    // of tearing the whole grid down.
    const errOf = function (s) {
      return (state.overviewErrors && state.overviewErrors[s.toUpperCase()]) || "";
    };
    const structFp =
      "P:" + posSyms.map(function (s) { return s.toUpperCase() + ":" + errOf(s); }).join(",") +
      "|W:" + watchSyms.map(function (s) { return s.toUpperCase() + ":" + errOf(s); }).join(",");
    if (state._gridStructFp === structFp && grid.querySelector(".mini-tile")) {
      posSyms.concat(watchSyms).forEach(function (s) {
        const m = _miniTileModel(s, chartColors, cs);
        const tile = grid.querySelector('.mini-tile[data-symbol="' + m.key + '"]');
        if (tile) updateMiniTile(tile, m);
      });
      return;
    }
    state._gridStructFp = structFp;
    grid.innerHTML = "";

    function addGroupHead(label) {
      const head = document.createElement("div");
      head.className = "overview-group-head";
      head.textContent = label;
      grid.appendChild(head);
    }

    if (posSyms.length) {
      addGroupHead("Positionen · nach Risiko (" + posSyms.length + ")");
      posSyms.forEach(function (s) {
        grid.appendChild(buildMiniTile(s, chartColors, cs));
      });
    }
    if (watchSyms.length) {
      addGroupHead("Watchlist (" + watchSyms.length + ")");
      watchSyms.forEach(function (s) {
        grid.appendChild(buildMiniTile(s, chartColors, cs));
      });
    }
  }

  function drawMiniCandles(cv, candles, marks) {
    if (!cv) return;
    const chartColors = getChartColors();
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
    // Faint raster so each tile reads like a real chart, not a sparkline.
    // V3-05: token color via getChartColors()/getComputedStyle (T26 helper),
    // not a hardcoded hex/rgba literal.
    ctx.strokeStyle = chartColorAlpha(chartColors.level, 0.10);
    ctx.lineWidth = 1;
    const hLines = 4;
    for (let g = 1; g < hLines; g++) {
      const gy = Math.round(pad + (plotH * g) / hLines) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, gy);
      ctx.lineTo(w, gy);
      ctx.stroke();
    }
    const vLines = 6;
    for (let g = 1; g < vLines; g++) {
      const gx = Math.round((w * g) / vLines) + 0.5;
      ctx.beginPath();
      ctx.moveTo(gx, pad);
      ctx.lineTo(gx, h - pad);
      ctx.stroke();
    }
    const n = cs.length,
      bw = Math.max(1, (w - 2) / n);
    cs.forEach(function (c, i) {
      const x = 1 + i * bw + bw / 2;
      const up = c.close >= c.open;
      ctx.strokeStyle = up ? chartColors.long : chartColors.short;
      ctx.fillStyle = up ? chartColors.long : chartColors.short;
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

  // ── KI-Keys panel (authenticated post-setup key management) ──────────
  var KI_DEFAULT_MODELS = {
    claude: "claude-opus-4-8",
    xai: "grok-4",
    openai: "gpt-5.1",
    ollama: "llama3.1",
  };

  function kiPill(ok, text) {
    var pill = $("ki-test-pill");
    if (!pill) return;
    pill.classList.remove("hidden");
    pill.classList.toggle("pill-ok", ok);
    pill.classList.toggle("pill-bad", !ok);
    pill.textContent = text;
  }

  function kiFail(msg) {
    var el = $("ki-keys-error");
    if (!el) return;
    if (!msg) {
      el.classList.add("hidden");
      return;
    }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function renderKiStatus(data) {
    var ul = $("ki-keys-status");
    if (!ul) return;
    ul.innerHTML = "";
    (data.providers || []).forEach(function (p) {
      var li = document.createElement("li");
      var dot = document.createElement("span");
      dot.className = "status-dot " + (p.configured ? "ok" : "unknown");
      var txt = document.createElement("span");
      txt.textContent =
        p.label +
        " — " +
        (p.configured ? "konfiguriert" : "kein Key") +
        (p.model ? " · " + p.model : "") +
        (p.id === data.active ? "  (aktiv)" : "");
      li.appendChild(dot);
      li.appendChild(txt);
      ul.appendChild(li);
    });
  }

  function kiSyncProvider() {
    var p = $("ki-provider").value;
    var isOllama = p === "ollama";
    var kf = $("ki-key-field");
    if (kf) kf.classList.toggle("hidden", isOllama);
    var modelInput = $("ki-model");
    if (modelInput && !modelInput.value.trim()) {
      modelInput.value = KI_DEFAULT_MODELS[p] || "";
    }
    var pill = $("ki-test-pill");
    if (pill) pill.classList.add("hidden");
  }

  async function loadKiStatus() {
    try {
      const res = await apiFetch("/api/settings/llm");
      if (!res.ok) return;
      const data = await res.json();
      renderKiStatus(data);
    } catch (err) {
      console.error("loadKiStatus", err);
    }
  }

  function openKiModal() {
    const modal = $("ki-keys-modal");
    if (!modal) return;
    kiFail("");
    var pill = $("ki-test-pill");
    if (pill) pill.classList.add("hidden");
    var modelInput = $("ki-model");
    if (modelInput) modelInput.value = "";
    kiSyncProvider();
    modal.classList.remove("hidden");
    loadKiStatus();
  }

  function closeKiModal() {
    const modal = $("ki-keys-modal");
    if (modal) modal.classList.add("hidden");
  }

  async function kiTestProvider() {
    var p = $("ki-provider").value;
    kiPill(true, "teste…");
    try {
      const res = await apiFetch("/api/settings/test-provider", {
        method: "POST",
        body: JSON.stringify({
          provider: p,
          api_key: $("ki-api-key").value.trim(),
          model: $("ki-model").value.trim(),
        }),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        kiPill(false, detailToText(data.detail || data) || "Fehler");
        return;
      }
      var ms = data.latency_ms != null ? " (" + data.latency_ms + " ms)" : "";
      kiPill(!!data.ok, (data.detail || (data.ok ? "OK" : "Fehler")) + ms);
    } catch (err) {
      kiPill(false, "Netzwerkfehler");
    }
  }

  async function kiSaveKey() {
    kiFail("");
    var p = $("ki-provider").value;
    var apiKey = $("ki-api-key").value.trim();
    var model = $("ki-model").value.trim();
    if (p !== "ollama" && !apiKey && !model) {
      return kiFail("Nichts zu speichern — Key oder Modell eingeben.");
    }
    var btn = $("btn-ki-save");
    btn.disabled = true;
    try {
      const res = await apiFetch("/api/settings/llm-key", {
        method: "POST",
        body: JSON.stringify({ provider: p, api_key: apiKey, model: model }),
      });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        kiFail(detailToText(data.detail || data) || "Speichern fehlgeschlagen");
        return;
      }
      renderKiStatus(data);
      $("ki-api-key").value = "";
      showToast("KI-Key gespeichert", "ok");
      // Refresh the existing provider dropdown so the newly-configured
      // provider becomes selectable in the hot-swap.
      loadLlm();
    } catch (err) {
      kiFail("Netzwerkfehler: " + (err && err.message ? err.message : err));
    } finally {
      btn.disabled = false;
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
      if (!res.ok || data.ok === false) {
        // A close can fail (or only PARTIALLY fill / stay unverified) while still
        // returning HTTP 200 — never paint that green (audit F-A1): the residual
        // position keeps running and the trader must not believe they are flat.
        showToast(detailToText(data.detail || data), "err");
        loadAccount();
        loadOpenOrders();
        return;
      }
      const closedTxt =
        "Geschlossen: " + fmt(data.closed_vol, 4) + " von " + fmt(data.hold_vol, 4);
      const warns = Array.isArray(data.warnings)
        ? data.warnings.filter(Boolean)
        : [];
      const notFlat =
        data.status && String(data.status).toLowerCase() !== "closed";
      if (warns.length || notFlat) {
        // Partial / unverified close: surface it as a warning, not success —
        // the position may still be open. Verify on the exchange.
        showToast(
          closedTxt +
            (notFlat ? " · " + escapeHtml(String(data.status)) : "") +
            (warns.length ? " — " + escapeHtml(warns.join("; ")) : "") +
            " — Position prüfen!",
          "err"
        );
      } else {
        showToast(closedTxt, "ok");
      }
      loadAccount();
      loadOpenOrders();
      loadHistory();
    } catch (err) {
      showToast("Schließen fehlgeschlagen: " + (err && err.message), "err");
    } finally {
      state.closeBusy = false;
    }
  }

  /** N3-09: Move/replace the stop-loss of an OPEN position to an ARBITRARY
   *  price `px`. A REAL money action: always confirmed, guarded against
   *  double-submit (state.slBusy), and any backend detail is surfaced verbatim.
   *  The server places the new stop, OID-verifies it, THEN cancels the old one
   *  (/api/orders/modify-sl cancel+replace), so the position is never left
   *  unprotected during the move. `opts.be` only tweaks the confirm/toast
   *  wording so the Break-Even button reads identically to before; the geometry
   *  is validated server-side (long-SL<entry etc.) — this client never bypasses
   *  that. There is NO path to the API that skips the window.confirm below. */
  async function moveStopTo(symbol, side, px, opts) {
    opts = opts || {};
    if (state.slBusy || state.closeBusy || state.orderBusy) return;
    if (!symbol || !side || !Number.isFinite(px) || px <= 0) return;
    const isBe = !!opts.be;
    const target = isBe ? "Break-Even " + fmt(px, 6) : fmt(px, 6);
    const text =
      "Stop-Loss der " + String(side).toUpperCase() + "-Position " + symbol +
      " auf " + target + " setzen?\n\n" +
      "Ein neuer Stop wird platziert und verifiziert, danach ein bestehender " +
      "alter Stop gecancelt. Dies ist eine echte Order-Aktion.";
    if (!window.confirm(text)) return;
    state.slBusy = true;
    try {
      const res = await apiFetch("/api/orders/modify-sl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: symbol, side: side, new_sl: px }),
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
        (isBe ? "SL → Break-Even gesetzt: " : "SL gesetzt: ") +
          fmt(data.new_sl != null ? data.new_sl : px, 6) +
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

  /** Thin wrapper: SL → fee-adjusted break-even. Behaviour identical to the
   *  original moveStopToBreakEven (same confirm/toast wording via opts.be). */
  async function moveStopToBreakEven(symbol, side, be) {
    return moveStopTo(symbol, side, be, { be: true });
  }

  /* ── C3-04b: drag the SL line on the chart ─────────────────────────────
     The overlay canvas stays pointer-events:none (normal pan/zoom) EXCEPT
     while the cursor is within ±4px of the CURRENT position's SL line, where
     it turns interactive (`sl-drag-armed`). A drag renders a ghost line only;
     dropping ARMS a pending value and asks for confirmation — the send goes
     through the SAME /api/orders/modify-sl cancel+replace path as the BE move.
     There is NO path from pointerup straight to the API: pointerup restores
     the chart, then calls moveStopViaDrag(), which is gated by window.confirm.
     Chart handleScroll/handleScale are ALWAYS restored on drag end (drop,
     Esc, pointer-leave, cancel) so a drag can never freeze the chart. */

  /** SL geometry of the ONE active position on the current chart symbol, or
   *  null. Mirrors the SL/TP classification in _drawTradeZones: the adverse-
   *  side exchange trigger is the SL, the favourable one the TP; manual mode
   *  falls back to the remembered marker SL/TP. Never returns a ticket-draft
   *  or TP line as the SL — only the real stop of an OPEN position. */
  function getActiveSlGeom() {
    const series = state.candleSeries;
    if (!series) return null;
    const positions = (state.account && state.account.positions) || [];
    for (const p of positions) {
      if (!symMatch(p.symbol, state.symbol)) continue;
      const entry = Number(p.entry_price);
      if (!Number.isFinite(entry) || entry <= 0) continue;
      const short = String(p.side || "").toLowerCase() === "short";
      let sl = null;
      let tp = null;
      const stops = (state.openOrders && state.openOrders.stop_orders) || [];
      stops.forEach(function (s) {
        if (s.symbol && !symMatch(s.symbol, state.symbol)) return;
        // T41b: shared SL/TP classifier (field → label → geometry) — same
        // source _drawTradeZones uses, so the draggable SL is the SAME stop the
        // zone overlay draws (mirrors app/orders/protection.py).
        const c = classifyTriggers(s, short ? "short" : "long", entry);
        if (c.sl != null) sl = c.sl;
        if (c.tp != null) tp = c.tp;
      });
      const mk = state.tradeMarkers && state.tradeMarkers[markerKey(p.symbol)];
      if (mk) {
        if (sl == null && mk.sl) sl = mk.sl;
        if (tp == null && mk.tp) tp = mk.tp;
      }
      if (sl == null || !(sl > 0)) continue; // no stop → nothing to drag
      const vol = Number(p.hold_vol) || 0;
      const cs = positionContractSize(p.contract_size, contractSize());
      return { symbol: p.symbol, side: short ? "short" : "long", entry, sl, vol, cs, tp };
    }
    return null;
  }

  /** Toggle the overlay between inert (normal chart pan/zoom passes through)
   *  and interactive (grabbable SL line). Class-driven — see .trade-overlay
   *  rules in app.css. */
  function setSlOverlayInteractive(on) {
    const overlay = tradeOverlayCanvas();
    if (!overlay || on === state._slOverlayOn) return;
    state._slOverlayOn = on;
    overlay.classList.toggle("sl-drag-armed", !!on);
  }

  /** ALWAYS restore chart interaction. Called on every drag terminus so a
   *  stuck handleScroll:false can never freeze the chart. */
  function endSlDrag() {
    state._slDrag = null;
    try {
      if (state.chart) state.chart.applyOptions({ handleScroll: true, handleScale: true });
    } catch (_) {}
    drawTradeZones(); // ghost gone → real SL line restored to its original spot
  }

  /** Hover hit-test on the chart wrap (fires for moves over chart AND overlay,
   *  since both bubble here). Arms the overlay only inside the ±4px SL band. */
  function onSlHoverMove(ev) {
    if (state._slDrag) return; // dragging: handled by pointer handlers below
    const overlay = tradeOverlayCanvas();
    if (!overlay || !state.candleSeries) return;
    const geom = getActiveSlGeom();
    if (!geom) { state._slHoverGeom = null; setSlOverlayInteractive(false); return; }
    const rect = overlay.getBoundingClientRect();
    const y = ev.clientY - rect.top;
    const slY = state.candleSeries.priceToCoordinate(geom.sl);
    if (slY == null || Math.abs(y - slY) > 4) {
      state._slHoverGeom = null;
      setSlOverlayInteractive(false);
      return;
    }
    state._slHoverGeom = geom;
    setSlOverlayInteractive(true);
  }

  function onSlDragStart(ev) {
    const geom = state._slHoverGeom || getActiveSlGeom();
    if (!geom || !state.candleSeries) return;
    if (state.orderBusy || state.slBusy || state.closeBusy) {
      showToast("Order in Arbeit — SL-Drag gesperrt.", null);
      return;
    }
    ev.preventDefault();
    state._slDrag = Object.assign({}, geom, { newSl: geom.sl });
    const overlay = tradeOverlayCanvas();
    try { overlay.setPointerCapture(ev.pointerId); } catch (_) {}
    // freeze pan/zoom for the duration of the drag
    try {
      if (state.chart) state.chart.applyOptions({ handleScroll: false, handleScale: false });
    } catch (_) {}
    drawTradeZones();
  }

  function onSlDragMove(ev) {
    if (!state._slDrag || !state.candleSeries) return;
    const overlay = tradeOverlayCanvas();
    const rect = overlay.getBoundingClientRect();
    const y = ev.clientY - rect.top;
    const price = state.candleSeries.coordinateToPrice(y);
    if (price == null || !(price > 0)) return;
    state._slDrag.newSl = price;
    drawTradeZones();
  }

  /** Drop: NEVER sends. Restore the chart first, then (if the new price moved
   *  and is on the correct side of entry) ARM the confirm. */
  function onSlDragEnd(ev) {
    if (!state._slDrag) return;
    const d = state._slDrag;
    const overlay = tradeOverlayCanvas();
    try { overlay.releasePointerCapture(ev.pointerId); } catch (_) {}
    const newSl = d.newSl;
    endSlDrag(); // restores chart + clears drag BEFORE any confirm/async work
    if (!Number.isFinite(newSl) || !(newSl > 0)) return;
    // Client-side reject an obviously-invalid SL (wrong side of entry) with a
    // clear message instead of sending it — the server gates this too.
    const wrongSide = d.side === "long" ? newSl >= d.entry : newSl <= d.entry;
    if (wrongSide) {
      showToast(
        "SL auf falscher Seite des Entrys (" +
          (d.side === "long" ? "Long-SL über" : "Short-SL unter") +
          " Entry) — abgebrochen.",
        "err"
      );
      return;
    }
    // No meaningful change → don't bother the trader with a confirm.
    if (Math.abs(newSl - d.sl) <= Math.abs(d.sl) * 1e-6) return;
    moveStopViaDrag(d.symbol, d.side, newSl);
  }

  function onSlDragCancel() {
    if (state._slDrag) endSlDrag();
  }

  function onSlWrapLeave() {
    // Pointer left the chart mid-drag → abort, restore SL + chart.
    if (state._slDrag) endSlDrag();
    setSlOverlayInteractive(false);
  }

  function onSlDragKey(ev) {
    if (ev.key === "Escape" && state._slDrag) endSlDrag();
  }

  /** Self-contained SL move via the EXISTING cancel+replace endpoint
   *  (/api/orders/modify-sl) — same path moveStopToBreakEven uses. Its own
   *  window.confirm guarantees no send without explicit confirmation, and the
   *  slBusy/orderBusy guards block a confirm while another order is in flight. */
  async function moveStopViaDrag(symbol, side, newSl) {
    if (state.slBusy || state.closeBusy || state.orderBusy) {
      showToast("Order in Arbeit — SL-Verschiebung nicht gesendet.", null);
      return;
    }
    if (!symbol || !side || !Number.isFinite(newSl) || newSl <= 0) return;
    const text =
      "Stop-Loss der " + String(side).toUpperCase() + "-Position " + symbol +
      " per Drag auf " + fmt(newSl, 6) + " verschieben?\n\n" +
      "Ein neuer Stop wird platziert und verifiziert, danach ein bestehender " +
      "alter Stop gecancelt. Dies ist eine echte Order-Aktion.";
    if (!window.confirm(text)) return; // explicit confirm — the ONLY send gate
    state.slBusy = true;
    try {
      const res = await apiFetch("/api/orders/modify-sl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: symbol, side: side, new_sl: newSl }),
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
        "SL verschoben: " +
          fmt(data.new_sl != null ? data.new_sl : newSl, 6) +
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
    // A scan-result click only switches to that coin's chart — it does NOT
    // auto-analyse. The detail analysis is a deliberate second step ("Analysieren"),
    // so browsing scan hits never spends an LLM call on its own (user request).
    goToSymbol(sym, { newTab: true });
    showToast("Chart geladen — 'Analysieren' für die KI-Detailanalyse.", "ok");
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

  // S2-06: a scan is a snapshot, not a live feed — past 10 minutes the
  // underlying setup may already be gone, so the strip must say so instead
  // of quietly aging into a stale click target.
  const SCAN_STALE_MIN = 10;

  function scanAgeMinutes(scannedAt) {
    if (typeof scannedAt !== "number") return null;
    return (Date.now() / 1000 - scannedAt) / 60;
  }

  function scanAgeLabel(scannedAt) {
    const mins = scanAgeMinutes(scannedAt);
    if (mins === null) return "";
    if (mins < 1) return "gerade eben";
    return "vor " + Math.floor(mins) + "m";
  }

  /** Refresh just the "vor Xm" age label + stale hint on the already-rendered
   *  scan strip, without rebuilding the chips (called on a timer). */
  function updateScanAgeDisplay() {
    const strip = $("scan-strip");
    const data = state.scanResults;
    if (!strip || !data) return;
    const mins = scanAgeMinutes(data.scanned_at);
    if (mins === null) return;
    const stale = mins > SCAN_STALE_MIN;
    const ageEl = strip.querySelector("#scan-age");
    if (ageEl) ageEl.textContent = scanAgeLabel(data.scanned_at);
    strip.classList.toggle("scan-stale", stale);
    const head = strip.querySelector(".scan-strip-head");
    let hintEl = strip.querySelector(".scan-stale-hint");
    if (stale && head && !hintEl) {
      hintEl = document.createElement("span");
      hintEl.className = "scan-stale-hint";
      hintEl.textContent = " · veraltet — neu scannen";
      head.appendChild(hintEl);
    } else if (!stale && hintEl) {
      hintEl.remove();
    }
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

    if (state._scanAgeTimer) clearInterval(state._scanAgeTimer);
    if (strip && data && typeof data.scanned_at === "number") {
      state._scanAgeTimer = setInterval(updateScanAgeDisplay, 15000);
    }

    const ageSpan = ' · <span id="scan-age">' + escapeHtml(scanAgeLabel(data && data.scanned_at)) + "</span>";

    if (!strip) return;
    if (!rows.length) {
      strip.className = "scan-strip";
      const emptyMsg = allRows.length
        ? "Alle Top-Kandidaten bereits offen (Position oder Order) — nichts Neues vorzuschlagen."
        : "Kein Setup mit klarem Edge — auch das ist ein Ergebnis.";
      strip.innerHTML =
        '<div class="scan-strip-head">Markt-Scan · ' +
        escapeHtml(String(data.model_used || "?")) + " · " +
        ((data.scanned || []).length || 0) + " Coins" + ageSpan + "</div>" +
        '<div class="scan-empty">' + emptyMsg + '</div>';
      updateScanAgeDisplay();
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
      ageSpan +
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
    updateScanAgeDisplay();
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

    // T3-01: a stale limit price must not be reachable (typing/tabbing into
    // it) once we're on Market — disabled also removes it from tab order.
    const priceEl = $("ticket-price");
    if (priceEl) priceEl.disabled = type === "market";

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
      const savedSize = localStorage.getItem(PERSIST.SIZE_MODE);
      if (savedSize === "margin" || savedSize === "position") state.sizeMode = savedSize;
    } catch (_) {}
    document.querySelectorAll(".size-mode-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setSizeMode(btn.getAttribute("data-size-mode") || "position");
      });
    });
    setSizeMode(state.sizeMode); // reflect restored mode in buttons + label
    setSltpMode(state.sltpMode); // build the %/currency suffix badges (T3-02)
    syncTicketSegments();
    updateTriggerModeUi();
  }

  function wireUi() {
    wireTicketSegments();
    wireSymbolPicker();
    wireSymbolTabs();
    // A3-11: the .tf-btn bar routes through setTf() — the ONE writer of
    // state.tf. It updates state first, then reflects the active button + HTF.
    document.querySelectorAll(".tf-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        setTf(btn.getAttribute("data-tf"));
      });
    });

    const loadBtn = $("btn-load");
    if (loadBtn) {
      loadBtn.addEventListener("click", () => {
        // A3-11: reload the ACTIVE symbol/tf from state (single source of truth).
        loadMarket(state.symbol, state.tf, state.htf);
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
        wirePricePaste(el); // T3-09
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
      if (e.key === "Escape") {
        closeConfirmModal();
        closeKiModal();
      }
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

    const jrnBtn = $("btn-journal-refresh");
    if (jrnBtn) {
      jrnBtn.addEventListener("click", () => {
        loadJournal();
      });
    }

    const jrnClearBtn = $("btn-journal-clear");
    if (jrnClearBtn) {
      jrnClearBtn.addEventListener("click", () => clearJournal());
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

    const kiKeysBtn = $("btn-ki-keys");
    if (kiKeysBtn) {
      kiKeysBtn.addEventListener("click", () => openKiModal());
    }
    const kiProvider = $("ki-provider");
    if (kiProvider) {
      kiProvider.addEventListener("change", () => {
        const m = $("ki-model");
        if (m) m.value = "";
        kiSyncProvider();
      });
    }
    const kiTestBtn = $("btn-ki-test");
    if (kiTestBtn) {
      kiTestBtn.addEventListener("click", () => kiTestProvider());
    }
    const kiSaveBtn = $("btn-ki-save");
    if (kiSaveBtn) {
      kiSaveBtn.addEventListener("click", () => kiSaveKey());
    }
    document.querySelectorAll("[data-close-ki]").forEach((el) => {
      el.addEventListener("click", () => closeKiModal());
    });

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

  // A3-01: numOrNull moved to utils.js (pure helper, loaded as a global before
  // app.js). Call sites unchanged.

  /** T3-09: a native <input type=number> simply refuses a de-DE formatted
   *  paste like "61.234,56" — a comma is not a legal character in a number
   *  input's value, so the browser drops/mangles it on paste instead of
   *  parsing it. We intercept the raw clipboard text ourselves and resolve
   *  it to a plain float before it ever reaches the input.
   *
   *  Heuristic: when BOTH separators are present, whichever comes LAST is
   *  the decimal separator (the other is thousands-grouping and gets
   *  stripped) — "61.234,56" -> comma is last -> 61234.56; "61,234.56" ->
   *  dot is last -> 61234.56. When only a comma is present, a single comma
   *  followed by 1-2 trailing digits is read as a de-DE decimal comma
   *  ("1234,5" -> 1234.5); anything else (multiple commas, or 3+ trailing
   *  digits) is thousands-grouping and the commas are stripped.
   *
   *  A lone dot ("1.234") is deliberately NOT reinterpreted as a thousands
   *  separator — that is genuinely ambiguous (US decimal vs. de-DE
   *  thousands with no decimal shown) and guessing wrong would silently
   *  10x/1000x a price. We take the standard/native reading (1.234) rather
   *  than guess; a value that still can't parse (e.g. two lone dots,
   *  "1.234.567" with no comma to disambiguate) reports null so the caller
   *  can toast instead of writing garbage into the field. */
  function parsePastedPrice(raw) {
    let s = String(raw == null ? "" : raw).trim();
    if (!s) return null;
    // Strip whitespace (incl. thin/nbsp used as thousands grouping) and any
    // currency/unit noise, keep only digits, separators and a leading sign.
    s = s.replace(/[\s  ]/g, "");
    s = s.replace(/[^0-9.,\-]/g, "");
    if (!s) return null;

    const hasComma = s.indexOf(",") !== -1;
    const hasDot = s.indexOf(".") !== -1;

    if (hasComma && hasDot) {
      if (s.lastIndexOf(",") > s.lastIndexOf(".")) {
        s = s.replace(/\./g, "").replace(",", "."); // de-DE: 61.234,56
      } else {
        s = s.replace(/,/g, ""); // en-US: 61,234.56
      }
    } else if (hasComma) {
      const parts = s.split(",");
      if (parts.length === 2 && parts[1].length >= 1 && parts[1].length <= 2) {
        s = parts[0] + "." + parts[1]; // de-DE decimal comma: 1234,5
      } else {
        s = s.replace(/,/g, ""); // thousands grouping: 61,234 / 1,234,567
      }
    }

    const n = Number(s);
    return Number.isFinite(n) ? n : null;
  }

  /** Wires a paste handler onto a price <input> that normalizes de-DE/en-US
   *  separators via parsePastedPrice() instead of letting the browser mangle
   *  (or silently empty) the field. Unreadable input toasts rather than
   *  guessing or dropping it silently (T3-09). */
  function wirePricePaste(el) {
    el.addEventListener("paste", function (e) {
      const cd = e.clipboardData || window.clipboardData;
      const raw = cd ? cd.getData("text") : "";
      if (!raw) return; // nothing to intercept — let the default paste run
      const n = parsePastedPrice(raw);
      if (n == null) {
        e.preventDefault();
        showToast(
          'Eingefügter Wert "' + raw.trim() + '" ist nicht lesbar — bitte Zahl manuell eintragen.',
          "err"
        );
        return;
      }
      e.preventDefault();
      el.value = String(n);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  function readTicket() {
    // A3-11 (trade-safety): the ticket binds to the ACTIVE symbol in state —
    // NOT the raw #symbol-input text. Typing a symbol without pressing Enter and
    // hitting Preview/Confirm therefore acts on the ACTIVE coin, never a stray
    // half-typed one. goToSymbol() is the only writer of state.symbol.
    const symbol = state.symbol || "BTC_USDT";
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
               s.rrr != null && s.rrr >= minRrr() ? "gut" : "") +
      "</div></div>";

    // 3) Warnings (non-blocking) — T3-05: still shown when a gate blocks (the
    // trader should see EVERYTHING wrong at once, not just the first error),
    // just re-labeled "außerdem…" since they're additional to the blocker
    // above, not the only thing standing between here and confirm.
    const warnings = preview.warnings || gate.warnings || [];
    if (warnings.length) {
      html +=
        '<div class="warn-box"><div class="warn-title">' +
        (okGates ? "Hinweise:" : "Außerdem:") +
        '</div><ul class="warn-list">' +
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
    const weakRrr = canConfirm && s.rrr != null && s.rrr < minRrr();
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
    // T3-07: focus the Abbrechen button (not the browser default of the
    // first focusable/first confirm-ish control) so an already-fingers-on-
    // Enter user lands on "cancel", never accidentally on "send live".
    const cancelBtn = $("btn-confirm-cancel");
    if (cancelBtn) cancelBtn.focus();

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
            // T3-06: expiry must not be a dead end — one click re-runs the
            // preview instead of forcing the trader to hunt for "Abbrechen"
            // and re-find the send button themselves.
            errEl.classList.remove("hidden");
            errEl.innerHTML =
              "Token abgelaufen — bitte erneut prüfen. " +
              '<button type="button" id="btn-confirm-expired-retry" class="btn btn-secondary">' +
              "Abgelaufen — neu prüfen</button>";
            const retryBtn = $("btn-confirm-expired-retry");
            if (retryBtn) {
              retryBtn.addEventListener("click", function () {
                closeConfirmModal();
                runPreview();
              });
            }
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
      return "Chance/Risiko zu niedrig — Take-Profit weiter setzen oder SL enger (min. 1:" +
        fmt(minRrr(), 1) + ").";
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
    // T3-07: the confirm modal owns the current token/ack state — if it's
    // open, Enter in a ticket field (or a stray submit) must NOT silently
    // kick off a second preview and replace the token/checkbox the trader
    // is looking at right now. Close it explicitly first.
    const openModal = $("confirm-modal");
    if (openModal && !openModal.classList.contains("hidden")) return;
    if (state.orderBusy) return;
    // U-03: the send button is disabled via updateOrderButtonsEnabled() when
    // apiAllowed===false, but a focused form field still submits on Enter,
    // bypassing that disabled state. Guard here too so Enter can't slip an
    // order through on a symbol where API orders are locked.
    if (state.apiAllowed === false) {
      setTicketError("apiAllowed=false — API-Orders für dieses Symbol gesperrt");
      return;
    }
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

  /** C3-03/T3-08: after an order actually PLACED, the ticket's disposable
   *  inputs must not linger — a stale SL/TP/limit-price becomes a duplicate
   *  chart line on the next redraw (drawTicketLines dedupes those, but the
   *  cleaner fix is to not leave them stale at all), and a stale Manual
   *  trigger mode would silently carry into the NEXT submit (sticky-manual
   *  duplicate-order risk). Size/leverage/side are KEPT — a series of
   *  same-setup trades shouldn't have to re-type those. Called ONLY from
   *  runConfirm's success branch (after res.ok, order confirmed placed) —
   *  never on error/block/timeout, where the trader still needs their inputs
   *  to correct and resubmit. */
  function resetTicketAfterConfirm() {
    ["ticket-price", "ticket-sl", "ticket-tp1"].forEach(function (id) {
      const el = $(id);
      if (el) el.value = "";
    });
    state.triggerMode = "auto";
    document.querySelectorAll(".trigger-mode-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-trigger") === "auto");
    });
    updateTriggerModeUi();
    try { drawTicketLines(); } catch (_) {}
    try { updateRiskReadout(); } catch (_) {}
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
          state.tradeEntryTimes[key] = state.liveBar.time; // legacy in-memory fallback
        }
        // A3-01: write under the canonical key so every read site (position
        // zones, mini-tiles, the manual-SL alarm) can find it again.
        // C3-07: entryMs is the RAW ms timestamp, not bucketed to the
        // CURRENT tf — bucketing happens at draw time (barOpenTimeSec in
        // _drawTradeZones), so the zone start survives both a reload (this
        // is persisted, tradeEntryTimes above is not) and a TF switch (a
        // bucket fixed to today's tf is not a bar time on a different tf).
        state.tradeMarkers[markerKey(key)] = {
          sl: Number(sm.stop_loss) || null,
          tp: Number(sm.take_profit) || null,
          side: sm.side,
          manual: (sm.trigger_mode || state.triggerMode) === "manual",
          ts: Date.now(),
          entryMs: Date.now(),
        };
        saveTradeMarkers();
      }
      // Success-only (we're past the `!res.ok` early-return above and any
      // sl_verified check — the order IS placed on the exchange either way,
      // sl_verified only distinguishes whether the protective stop was
      // confirmed): clear the disposable ticket inputs + un-stick Manual
      // mode now, never on the error/catch paths below.
      resetTicketAfterConfirm();
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
    loadJournal,
    renderJournal,
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
    // O6: skip the 30s account/fills/orders polls while the tab is hidden
    // (no point fetching into a page nobody is looking at); refresh immediately
    // when it becomes visible again so the data is never stale on return.
    const _whenVisible = function (fn) {
      return function () { if (!document.hidden) fn(); };
    };
    setInterval(_whenVisible(loadAccount), 30000);
    setInterval(_whenVisible(loadFills), 30000); // same cadence as the account poll
    setInterval(_whenVisible(loadOpenOrders), 30000);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      loadAccount();
      loadOpenOrders();
      loadFills();
      // E3-05: the chart poll and overview timer were skipped while hidden.
      // They self-schedule via setInterval so they re-arm on their own, but the
      // next fire can be up to a full interval away — kick an immediate silent
      // catch-up now so returning to the tab is never stale.
      if (state.activeView === "chart" && state.symbol && state._chartKey) {
        try {
          loadMarket(state.symbol, state.tf || "15m", state.htf || "1H", { silent: true });
        } catch (_) { /* next interval tick will retry */ }
      }
      if (state.activeView === "overview") {
        try { refreshOverview(); } catch (_) {}
        try { refreshNews(); } catch (_) {}
      }
    });
    // Live chart poll, adaptive:
    //  - WS live: ticks stream in real time already; full refresh
    //    (indicators, structure) every 15 s to spare the exchange API.
    //  - WS down/error: full reload every 5 s so the chart stays live.
    let pollTick = 0;
    setInterval(function () {
      // U-02: independent of the early-returns below — this is the ONLY hook
      // that re-checks staleness when NO tick arrives at all (a dead feed
      // never calls setLivePrice/checkManualSlAlarm again to notice itself).
      updateStaleBanner();
      // Task 4/E3-01: client app-ping watchdog. A pong proves the SOCKET is
      // alive; it is tracked separately from _lastTickTs (price data) so a
      // frozen-but-open WS (laptop sleep, network change, silent HL
      // subscription loss) gets force-closed and reconnected instead of
      // sitting there forever "live" with dead prices. Piggybacks on this
      // existing 5s tick instead of adding a second timer.
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        const _now = Date.now();
        if (_now - (state._lastAppPingTs || 0) >= APP_PING_INTERVAL_MS) {
          state._lastAppPingTs = _now;
          try {
            state.ws.send(JSON.stringify({ type: "ping" }));
          } catch (_) {
            /* ignore — onerror/onclose will handle a truly dead socket */
          }
        }
        if (state._lastPongTs != null && _now - state._lastPongTs > APP_PONG_TIMEOUT_MS) {
          console.warn("WS app-ping timeout — forcing reconnect");
          try {
            state.ws.close();
          } catch (_) {
            /* ignore */
          }
        }
      }
      // E3-05: skip the full /api/market snapshot poll while the tab is hidden
      // (a background tab was pulling the whole market ~every 15s — up to ~17k
      // requests/night for nothing). This guard sits AFTER the WS app-ping
      // watchdog and updateStaleBanner above ON PURPOSE: the WebSocket, its
      // tick-driven uPnL, and the manual-SL alarm MUST keep running while
      // hidden — only the HTTP snapshot poll is gated. The visibilitychange
      // handler fires an immediate silent loadMarket on return so the chart
      // isn't stale; this setInterval keeps ticking either way (no re-arm
      // needed — it self-fires on the next interval, it does not setTimeout).
      if (document.hidden) return;
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
    // A3-11 boot seed: state is the single source of truth. state.symbol/tf/htf
    // already carry the store.js defaults (tf "15m" matches the template's
    // default-active .tf-btn); the server hint overrides only the symbol. Seed
    // state, then reflect it TO the DOM (input value + active tf button) — the
    // DOM is never read back as a competing truth.
    const symbol = (h && h.default_symbol) || state.symbol || "BTC";
    state.tf = state.tf || "15m";
    state.htf = state.htf || "1H";
    state.symbol = String(symbol).toUpperCase().trim();
    const bootInput = $("symbol-input");
    if (bootInput) bootInput.value = state.symbol;
    document.querySelectorAll(".tf-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tf") === state.tf);
    });
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
      if (document.hidden) return; // E3-05: no snapshot polls into a hidden tab
      if (state.activeView === "overview") {
        refreshOverview();
        refreshNews(); // internally throttled to 5 min
      }
    }, 30000);

    showOverview(); // start on the overview tab; also triggers the first mini refresh
  });
})();

