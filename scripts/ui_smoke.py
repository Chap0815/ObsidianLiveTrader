"""Offline browser checks: rendered templates + intercepted fixture APIs only.

Run with .venv/Scripts/python.exe scripts/ui_smoke.py [--capture-only].
Requires the optional dev dependency playwright and `playwright install chromium`.
Never imports app.main, reads .env, starts a server, or contacts an exchange.
Screenshots are written to the ignored .superpowers/ui-review directory.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import mimetypes
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:18787"


def market(symbol="BTC"):
    base = {"BTC": 64000, "ETH": 3200, "SOL": 145}.get(symbol, 1)
    now = int(time.time()) // 900 * 900
    candles = []
    for i in range(160):
        op = base * (0.982 + i * 0.0001 + math.sin(i * 0.45) * 0.002)
        cl = op + math.sin(i * 1.7) * base * 0.001
        candles.append(
            {
                "time": (now - (159 - i) * 900) * 1000,
                "open": op,
                "close": cl,
                "high": max(op, cl) + base * 0.001,
                "low": min(op, cl) - base * 0.001,
                "vol": 50 + i % 17,
            }
        )
    return {
        "symbol": symbol,
        "last_price": candles[-1]["close"],
        "contract": {
            "contractSize": 1,
            "maxLeverage": 20,
            "apiAllowed": True,
            "priceUnit": 0.1,
            "volUnit": 0.00001,
            "minVol": 0.00001,
        },
        "funding": {"fundingRate": 0.000012},
        "ltf": {
            "candles": candles,
            "indicators": {
                "last": {"rsi14": 54.82, "ema20": base * 0.996, "ema50": base * 0.989}
            },
            "structure": {
                "support": base * 0.98,
                "resistance": base * 1.02,
                "range_low": base * 0.97,
                "range_high": base * 1.03,
            },
        },
    }


async def run(capture_only=False):
    env = Environment(
        loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape()
    )
    html = env.get_template("dashboard.html").render(
        default_symbol="BTC",
        trading_enabled=False,
        hl_testnet=True,
        exchange="hyperliquid",
        max_leverage=20,
    )
    setup_html = env.get_template("setup.html").render(
        default_models={
            "claude": "claude-sonnet-5",
            "xai": "grok-4",
            "openai": "gpt-5.1",
            "ollama": "llama3.1",
        },
        risk_cards=[
            {"value": "conservative", "title": "Conservative", "desc": "0.5% risk"},
            {"value": "balanced", "title": "Balanced", "desc": "1% risk"},
            {"value": "free", "title": "Unrestricted", "desc": "no equity cap"},
        ],
    )
    out = ROOT / ".superpowers/ui-review"
    out.mkdir(parents=True, exist_ok=True)
    calls, errors = [], []
    health = {
        "ok": True,
        "exchange": "hyperliquid",
        "exchange_configured": True,
        "hl_testnet": True,
        "trading_enabled": False,
        "live_trading": False,
        "default_symbol": "BTC",
        "max_leverage": 20,
        "max_risk_pct": 1,
        "llm_provider": "claude",
        "llm_configured": True,
    }
    account = {"equity_usdt": 12500, "available_usdt": 12500, "positions": []}
    providers = [{"id": "claude", "label": "Claude", "configured": True}]
    symbols_degraded = {"active": False}

    async def route_request(route):
        url = urlparse(route.request.url)
        path = url.path
        if url.netloc != "127.0.0.1:18787":
            errors.append("Unexpected external request: " + url.netloc)
            return await route.abort()
        if path == "/":
            return await route.fulfill(content_type="text/html", body=html)
        if path == "/setup-test":
            return await route.fulfill(content_type="text/html", body=setup_html)
        if path.startswith("/static/"):
            file = (ROOT / "app" / path.lstrip("/")).resolve()
            if not file.is_relative_to(ROOT / "app/static") or not file.is_file():
                return await route.fulfill(status=404)
            return await route.fulfill(
                path=file,
                content_type=mimetypes.guess_type(file)[0]
                or "application/octet-stream",
            )
        calls.append((route.request.method, path))
        data = {}
        if path == "/api/health":
            data = health
        elif path == "/api/account":
            data = account
        elif path == "/api/symbols":
            data = (
                {
                    "symbols": ["BTC", "ETH", "SOL"],
                    "error": "private upstream diagnostic",
                    "fallback": True,
                }
                if symbols_degraded["active"]
                else {"symbols": ["BTC", "ETH", "SOL"], "error": None}
            )
        elif path in ("/api/llm", "/api/settings/llm"):
            data = {"provider": "claude", "providers": providers}
        elif path.startswith("/api/market/"):
            if path.endswith("/ETH"):
                await asyncio.sleep(0.4)
            data = market(path.rsplit("/", 1)[-1])
        elif path == "/api/mini":
            symbols = parse_qs(url.query).get("symbols", ["BTC,ETH,SOL"])[0].split(",")
            data = {
                "results": [
                    {
                        "symbol": s,
                        "last_price": market(s)["last_price"],
                        "change_pct": 1.24,
                        "candles": market(s)["ltf"]["candles"],
                    }
                    for s in symbols
                ],
                "errors": [],
            }
        elif path == "/api/news":
            data = {"items": [], "errors": [], "stale": False}
        elif path == "/api/orders/open":
            data = {"orders": [], "stop_orders": []}
        elif path == "/api/fills":
            data = {"supported": True, "fills": []}
        elif path == "/api/history":
            data = {"proposals": [], "orders": []}
        elif path == "/api/positions/alerts":
            data = {"alerts": []}
        elif path in ("/api/journal", "/api/journal/stats"):
            data = {"entries": [], "total": 0, "by_confidence": {}, "by_regime": {}}
        elif path == "/api/orders/preview":
            ticket = route.request.post_data_json
            data = {
                "ok": True,
                "token": "offline-preview",
                "expires_in_seconds": 60,
                "summary": {
                    "symbol": ticket["symbol"],
                    "side": ticket["side"],
                    "order_type": "market",
                    "vol": ticket["vol"],
                    "leverage": 5,
                    "notional_usdt": 100,
                    "entry_for_risk": market()["last_price"],
                    "stop_loss": ticket.get("stop_loss"),
                    "take_profit": ticket.get("take_profit"),
                    "risk_usdt": 2,
                    "risk_pct": 0.016,
                    "rrr": 2,
                    "trigger_mode": ticket.get("trigger_mode", "auto"),
                },
            }
        elif path == "/api/orders/confirm":
            await asyncio.sleep(0.8)
            data = {"external_oid": "offline-only", "sl_verified": True}
        else:
            errors.append("Unmocked API: " + path)
            return await route.fulfill(
                status=404, json={"detail": "Offline fixture missing"}
            )
        await route.fulfill(json=data)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
        )
        await context.route("**/*", route_request)
        await context.route_web_socket("**/*", lambda ws: ws.close())
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        await page.goto(ORIGIN)
        await page.wait_for_function(
            "window.Trader && Trader.state.health && document.querySelector('.symbol-tab')"
        )
        for width in (1440, 390, 320):
            await page.set_viewport_size({"width": width, "height": 900})
            if not capture_only:
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), f"Overview overflows at {width}"
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.screenshot(path=out / "overview-desktop.png", full_page=True)
        await page.locator("#symbol-input").fill("BTC")
        await page.locator("#symbol-input").press("Enter")
        await page.wait_for_function("Trader.state.market && Trader.state._chartKey")
        await page.evaluate("document.fonts.ready")
        prefix = "before" if capture_only else "after"
        dimensions = [
            (2048, 1037),
            (1920, 1080),
            (1440, 1000),
            (1280, 900),
            (1024, 768),
            (768, 1024),
            (390, 844),
            (320, 740),
            (1440, 720),
        ]
        for width, height in dimensions:
            await page.set_viewport_size({"width": width, "height": height})
            await page.screenshot(path=out / f"{prefix}-{width}.png", full_page=True)
            overflow = await page.evaluate(
                "document.documentElement.scrollWidth > innerWidth"
            )
            print(f"viewport {width}x{height}: overflow={overflow}")
            if not capture_only:
                assert not overflow, f"Page overflows at {width}px"
                await chart_layout_checks(page, width, height)
        if not capture_only:
            await interaction_checks(page, calls, health, account)
            symbols_degraded["active"] = True
            degraded_page = await context.new_page()
            degraded_page.on("pageerror", lambda e: errors.append(str(e)))
            degraded_page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            await degraded_page.goto(ORIGIN)
            await degraded_page.wait_for_function(
                "window.Trader && Trader.state.allSymbols.length === 3"
            )
            symbol_notice = await degraded_page.evaluate(
                """() => {
                  const toast = document.getElementById('toast');
                  return {
                    usable: Trader.state.allSymbols.includes('BTC'),
                    visible: !!toast && !toast.classList.contains('hidden') &&
                      toast.textContent.includes('symbol list'),
                    leaked: !!toast && toast.textContent.includes('private upstream diagnostic')
                  };
                }"""
            )
            assert symbol_notice["usable"], "Fallback symbol list was not usable"
            assert symbol_notice["visible"], "Fallback symbol list was not visibly marked"
            assert not symbol_notice["leaked"], "Symbol fallback leaked provider details"
            await degraded_page.close()
            symbols_degraded["active"] = False
            print("PASS fallback symbol list remains usable and is visibly marked")
            for failure_mode in ("http", "network"):
                failed_page = await context.new_page()
                failed_page.on("pageerror", lambda e: errors.append(str(e)))
                failed_page.on(
                    "console",
                    lambda msg: errors.append(msg.text)
                    if msg.type == "error"
                    else None,
                )
                await failed_page.add_init_script(
                    f"""(() => {{
                      const failureMode = {failure_mode!r};
                      const originalFetch = window.fetch.bind(window);
                      window.fetch = (input, opts = {{}}) => {{
                        const raw = input && input.url ? input.url : input;
                        const url = new URL(String(raw), location.href);
                        if (url.pathname !== '/api/symbols') {{
                          return originalFetch(input, opts);
                        }}
                        window.__symbolsFailureSeen = failureMode;
                        if (failureMode === 'http') {{
                          return Promise.resolve(new Response('', {{status: 503}}));
                        }}
                        return Promise.reject(new TypeError('offline symbol read'));
                      }};
                    }})()"""
                )
                await failed_page.goto(ORIGIN)
                await failed_page.wait_for_function(
                    "window.Trader && window.__symbolsFailureSeen"
                )
                await failed_page.evaluate(
                    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
                visible = await failed_page.evaluate(
                    """() => {
                      const toast = document.getElementById('toast');
                      return !!toast && !toast.classList.contains('hidden') &&
                        toast.textContent.includes('symbol list');
                    }"""
                )
                assert visible, (
                    f"Symbol-list {failure_mode} failure was not visibly marked"
                )
                await failed_page.close()
            print("PASS symbol-list HTTP/network failures are visibly marked")
            setup_page = await context.new_page()
            setup_page.on("pageerror", lambda e: errors.append(str(e)))
            await setup_page.goto(ORIGIN + "/setup-test")
            await expect(setup_page.locator("html")).to_have_attribute("lang", "en")
            await expect(
                setup_page.get_by_role("heading", name="Set up Obsidian Live Trader")
            ).to_be_visible()
            await setup_page.locator(
                'label.choice-card:has(input[name="exchange"][value="mexc"])'
            ).click()
            await expect(setup_page.locator("#fields-mexc")).to_be_visible()
            await expect(setup_page.locator("#fields-hl")).to_be_hidden()
            visible_text = await setup_page.locator("body").inner_text()
            for german_label in (
                "Einrichtungsassistent",
                "Börse",
                "Risiko-Profil",
                "Speichern",
                "Bestätigung",
            ):
                assert german_label not in visible_text, (
                    f"German setup label remained visible: {german_label}"
                )
            for width in (1440, 768, 390, 320):
                await setup_page.set_viewport_size({"width": width, "height": 900})
                overflow = await setup_page.evaluate("""() => ({
                  page: document.documentElement.scrollWidth,
                  viewport: innerWidth,
                  offenders: Array.from(document.querySelectorAll('body *'))
                    .filter(el => el.getBoundingClientRect().right > innerWidth + 1)
                    .slice(0, 8)
                    .map(el => ({tag: el.tagName, cls: el.className, right: el.getBoundingClientRect().right}))
                })""")
                assert overflow["page"] <= overflow["viewport"], (
                    f"Setup overflows at {width}px: {overflow}"
                )
            await setup_page.set_viewport_size({"width": 390, "height": 844})
            await setup_page.screenshot(path=out / "setup-390.png", full_page=True)
            await setup_page.close()
            print("PASS English first-run setup is responsive and interactive")
        assert not errors, errors
        await browser.close()
    print("Offline browser checks passed. Screenshots:", out)


async def chart_layout_checks(page, width, height):
    # The chart widget includes both candle canvas and time axis.
    await page.wait_for_function("""() => {
      const wrap = document.getElementById('chart-wrap');
      const widget = document.querySelector('#chart .tv-lightweight-charts');
      return widget && Math.abs(widget.getBoundingClientRect().height - wrap.clientHeight) <= 2;
    }""")
    sizes = await page.evaluate("""() => {
      const box = selector => document.querySelector(selector).getBoundingClientRect();
      return {
        chart: box('#chart-wrap').height,
        stats: box('.statbar').height,
        price: box('.stat-price').height,
        font: parseFloat(getComputedStyle(document.getElementById('ctx-price')).fontSize),
        overlay: box('#trade-overlay').height
      };
    }""")
    assert sizes['font'] <= 16, f"Oversized price at {width}px: {sizes}"
    assert sizes['price'] < sizes['stats'] * 0.6, f"Price spans rows at {width}px: {sizes}"
    assert abs(sizes['overlay'] - sizes['chart']) <= 2, f"Overlay misaligned: {sizes}"
    if width > 1000:
        assert sizes['chart'] >= height * 0.55, f"Desktop chart collapsed: {sizes}"
    if width >= 1280:
        assert sizes['stats'] <= 130, f"Desktop market context too tall: {sizes}"
    print(f"PASS chart layout {width}x{height}: chart={sizes['chart']:.0f}px, stats={sizes['stats']:.0f}px")


async def interaction_checks(page, calls, health, account):
    await page.evaluate(
        """() => {
          const originalFetch = window.fetch.bind(window);
          window.__fillsProbe = {calls: [], aborts: 0, restore() { window.fetch = originalFetch; }};
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/fills') return originalFetch(input, opts);
            const symbol = url.searchParams.get('symbol');
            window.__fillsProbe.calls.push(symbol);
            if (window.__fillsProbe.calls.length > 1) {
              return Promise.resolve(new Response(
                JSON.stringify({supported: true, fills: []}),
                {status: 200, headers: {'Content-Type': 'application/json'}}
              ));
            }
            return new Promise((resolve, reject) => {
              const signal = opts && opts.signal;
              if (signal) signal.addEventListener('abort', () => {
                window.__fillsProbe.aborts += 1;
                reject(new DOMException('superseded fill read', 'AbortError'));
              }, {once: true});
            });
          };
        }"""
    )
    await page.locator('[data-tab="trades"]').click()
    await page.wait_for_function("__fillsProbe.calls.length === 1")
    await page.locator("#symbol-input").fill("ETH")
    await page.locator("#symbol-input").press("Enter")
    await page.wait_for_function(
        "__fillsProbe.calls.length === 2 && __fillsProbe.aborts === 1"
    )
    assert await page.evaluate(
        "__fillsProbe.calls[0] === 'BTC' && __fillsProbe.calls[1] === 'ETH'"
    )
    await page.evaluate("__fillsProbe.restore()")
    await page.locator("#symbol-input").fill("BTC")
    await page.locator("#symbol-input").press("Enter")
    await page.wait_for_function(
        "Trader.state.market && Trader.state.market.symbol === 'BTC'"
    )
    print("PASS superseded symbol fill read is aborted without console error")

    fills_soft_error = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const confirmedFills = [{
            symbol: 'BTC', px: 60000, sz: 0.1, side: 'buy',
            time: 1710000005000, dir: 'Open Long'
          }];
          Trader.state.fills = confirmedFills;
          window.__fillsSoftError = {calls: 0};
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/fills') return originalFetch(input, opts);
            window.__fillsSoftError.calls += 1;
            return Promise.resolve(new Response(JSON.stringify({
              supported: true,
              fills: [],
              error: 'exchange unavailable'
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            document.querySelector('[data-tab="positions"]').click();
            document.querySelector('[data-tab="trades"]').click();
            while (window.__fillsSoftError.calls < 1) {
              await new Promise(requestAnimationFrame);
            }
            await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const toast = document.getElementById('toast');
            return {
              preserved: Trader.state.fills === confirmedFills,
              visibleWarning: !!toast && !toast.classList.contains('hidden') &&
                toast.textContent.includes('Trade history is stale')
            };
          } finally {
            window.fetch = originalFetch;
            delete window.__fillsSoftError;
          }
        }"""
    )
    assert fills_soft_error["preserved"], (
        "Fills soft-error replaced the last confirmed trade history"
    )
    assert fills_soft_error["visibleWarning"], (
        "Preserved trade history was not visibly marked stale"
    )
    print("PASS fills soft-error preserves and visibly marks confirmed trade history")

    for failure_mode in ("http", "network"):
        fills_read_error = await page.evaluate(
            """async (failureMode) => {
              const originalFetch = window.fetch.bind(window);
              const confirmedFills = [{
                symbol: 'BTC', px: 60100, sz: 0.2, side: 'buy',
                time: 1710000006000, dir: 'Open Long'
              }];
              Trader.state.fills = confirmedFills;
              Trader.state._fillsStaleWarned = false;
              const toast = document.getElementById('toast');
              if (toast) toast.classList.add('hidden');
              window.__fillsReadError = {calls: 0};
              window.fetch = (input, opts = {}) => {
                const raw = input && input.url ? input.url : input;
                const url = new URL(String(raw), location.href);
                if (url.pathname !== '/api/fills') return originalFetch(input, opts);
                window.__fillsReadError.calls += 1;
                if (failureMode === 'http') {
                  return Promise.resolve(new Response('', {status: 503}));
                }
                return Promise.reject(new TypeError('offline fill read'));
              };
              try {
                document.querySelector('[data-tab="positions"]').click();
                document.querySelector('[data-tab="trades"]').click();
                while (window.__fillsReadError.calls < 1) {
                  await new Promise(requestAnimationFrame);
                }
                await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                return {
                  preserved: Trader.state.fills === confirmedFills,
                  visibleWarning: !!toast && !toast.classList.contains('hidden') &&
                    toast.textContent.includes('Trade history is stale')
                };
              } finally {
                window.fetch = originalFetch;
                delete window.__fillsReadError;
              }
            }""",
            failure_mode,
        )
        assert fills_read_error["preserved"], (
            f"Fills {failure_mode} error replaced the last confirmed trade history"
        )
        assert fills_read_error["visibleWarning"], (
            f"Trade history {failure_mode} failure was not visibly marked stale"
        )
    print("PASS fills HTTP/network errors preserve and visibly mark trade history")

    await page.evaluate(
        """() => {
          const originalFetch = window.fetch.bind(window);
          window.__historyProbe = {calls: 0, aborts: 0, restore() { window.fetch = originalFetch; }};
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/history') return originalFetch(input, opts);
            window.__historyProbe.calls += 1;
            if (window.__historyProbe.calls > 1) {
              return Promise.resolve(new Response(
                JSON.stringify({proposals: [], orders: []}),
                {status: 200, headers: {'Content-Type': 'application/json'}}
              ));
            }
            return new Promise((resolve, reject) => {
              const signal = opts && opts.signal;
              if (signal) signal.addEventListener('abort', () => {
                window.__historyProbe.aborts += 1;
                reject(new DOMException('superseded history read', 'AbortError'));
              }, {once: true});
            });
          };
          const button = document.getElementById('btn-history-refresh');
          button.click();
          button.click();
        }"""
    )
    await page.wait_for_function(
        "__historyProbe.calls === 2 && __historyProbe.aborts === 1"
    )
    await page.evaluate("__historyProbe.restore()")
    print("PASS superseded history read is aborted without stale render")

    await page.evaluate(
        """() => {
          const originalFetch = window.fetch.bind(window);
          window.__journalProbe = {
            stats: 0, entries: 0, aborts: 0,
            restore() { window.fetch = originalFetch; }
          };
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            const isStats = url.pathname === '/api/journal/stats';
            const isEntries = url.pathname === '/api/journal';
            if (!isStats && !isEntries) return originalFetch(input, opts);
            const key = isStats ? 'stats' : 'entries';
            window.__journalProbe[key] += 1;
            if (window.__journalProbe[key] > 1) {
              const data = isStats ? {} : {entries: []};
              return Promise.resolve(new Response(JSON.stringify(data), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            return new Promise((resolve, reject) => {
              const signal = opts && opts.signal;
              if (signal) signal.addEventListener('abort', () => {
                window.__journalProbe.aborts += 1;
                reject(new DOMException('superseded journal read', 'AbortError'));
              }, {once: true});
            });
          };
          const button = document.getElementById('btn-journal-refresh');
          button.click();
          button.click();
        }"""
    )
    await page.wait_for_function(
        "__journalProbe.stats === 2 && __journalProbe.entries === 2 && __journalProbe.aborts === 2"
    )
    await page.evaluate("__journalProbe.restore()")
    print("PASS superseded journal stats and entries reads are both aborted")

    await page.evaluate(
        """() => {
          const originalFetch = window.fetch.bind(window);
          window.__calibrationProbe = {calls: 0, aborts: 0, restore() { window.fetch = originalFetch; }};
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/journal/stats') return originalFetch(input, opts);
            window.__calibrationProbe.calls += 1;
            if (window.__calibrationProbe.calls > 1) {
              return Promise.resolve(new Response(JSON.stringify({}), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            return new Promise((resolve, reject) => {
              const signal = opts && opts.signal;
              if (signal) signal.addEventListener('abort', () => {
                window.__calibrationProbe.aborts += 1;
                reject(new DOMException('superseded calibration read', 'AbortError'));
              }, {once: true});
            });
          };
          const button = document.getElementById('btn-calibration-refresh');
          button.click();
          button.click();
        }"""
    )
    await page.wait_for_function(
        "__calibrationProbe.calls === 2 && __calibrationProbe.aborts === 1"
    )
    await page.evaluate("__calibrationProbe.restore()")
    print("PASS superseded calibration read is aborted without stale render")

    await page.evaluate(
        """() => {
          const originalFetch = window.fetch.bind(window);
          const waiting = [];
          window.__accountProbe = {
            calls: 0,
            blocking: true,
            release() {
              this.blocking = false;
              waiting.splice(0).forEach((resolve) => resolve());
            },
            restore() { window.fetch = originalFetch; }
          };
          window.fetch = async (...args) => {
            const raw = args[0] && args[0].url ? args[0].url : args[0];
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/account') return originalFetch(...args);
            window.__accountProbe.calls += 1;
            const response = await originalFetch(...args);
            if (window.__accountProbe.blocking) {
              await new Promise((resolve) => waiting.push(resolve));
            }
            return response;
          };
        }"""
    )
    await page.evaluate(
        """() => {
          window.__accountBatch = Promise.all([
            Trader.loadAccount(), Trader.loadAccount(), Trader.loadAccount(),
            Trader.loadAccount(), Trader.loadAccount()
          ]);
        }"""
    )
    assert await page.evaluate(
        "__accountProbe.calls === 1 && Trader.state._accountLoadQueued === true"
    )
    await page.evaluate("__accountProbe.release()")
    await page.evaluate("window.__accountBatch")
    await page.wait_for_function(
        "__accountProbe.calls === 2 && Trader.state._accountLoadPromise === null"
    )
    await page.evaluate("__accountProbe.restore()")
    print("PASS account refresh bursts serialize with one trailing reconciliation")

    account_soft_error = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const confirmedFlat = {
            equity_usdt: 12500,
            available_usdt: 12500,
            positions: [],
            error: null
          };
          Trader.state.account = confirmedFlat;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/account') return originalFetch(input, opts);
            return Promise.resolve(new Response(JSON.stringify({
              equity_usdt: 0,
              available_usdt: 0,
              positions: [],
              error: 'exchange unavailable'
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            await Trader.loadAccount();
            const toast = document.getElementById('toast');
            return {
              preserved: Trader.state.account === confirmedFlat,
              visibleWarning: !!toast && !toast.classList.contains('hidden') &&
                toast.textContent.includes('Account data is stale')
            };
          } finally {
            window.fetch = originalFetch;
          }
        }"""
    )
    assert account_soft_error["preserved"], (
        "Account soft-error replaced a confirmed flat snapshot"
    )
    assert account_soft_error["visibleWarning"], (
        "Preserved account snapshot was not visibly marked stale"
    )
    print("PASS account soft-error preserves and visibly marks a confirmed flat snapshot")

    await page.evaluate(
        """() => {
          const originalFetch = window.fetch.bind(window);
          const waiting = [];
          window.__ordersProbe = {
            calls: 0,
            blocking: true,
            release() {
              this.blocking = false;
              waiting.splice(0).forEach((resolve) => resolve());
            },
            restore() { window.fetch = originalFetch; }
          };
          window.fetch = async (...args) => {
            const raw = args[0] && args[0].url ? args[0].url : args[0];
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/orders/open') return originalFetch(...args);
            window.__ordersProbe.calls += 1;
            const response = await originalFetch(...args);
            if (window.__ordersProbe.blocking) {
              await new Promise((resolve) => waiting.push(resolve));
            }
            return response;
          };
          const button = document.getElementById('btn-orders-refresh');
          for (let i = 0; i < 5; i += 1) button.click();
        }"""
    )
    assert await page.evaluate(
        "__ordersProbe.calls === 1 && Trader.state._ordersLoadQueued === true"
    )
    await page.evaluate("__ordersProbe.release()")
    await page.wait_for_function(
        "__ordersProbe.calls === 2 && Trader.state._ordersLoadPromise === null"
    )
    await page.evaluate("__ordersProbe.restore()")
    print("PASS open-order refresh bursts serialize with one trailing reconciliation")

    for failure_mode in ("soft", "http", "network"):
        orders_read_error = await page.evaluate(
            """async (failureMode) => {
              const originalFetch = window.fetch.bind(window);
              Trader.state.openOrders = {
                orders: [{orderId: 91, symbol: 'BTC', price: 60000}],
                stop_orders: [{orderId: 92, symbol: 'BTC', stopLossPrice: 59000}],
                error: null
              };
              window.__ordersReadError = {calls: 0};
              window.fetch = (input, opts = {}) => {
                const raw = input && input.url ? input.url : input;
                const url = new URL(String(raw), location.href);
                if (url.pathname !== '/api/orders/open') return originalFetch(input, opts);
                window.__ordersReadError.calls += 1;
                if (failureMode === 'network') {
                  return Promise.reject(new TypeError('offline order read'));
                }
                const payload = failureMode === 'soft'
                  ? {orders: [], stop_orders: [], error: 'private upstream diagnostic'}
                  : {detail: 'private upstream diagnostic'};
                return Promise.resolve(new Response(JSON.stringify(payload), {
                  status: failureMode === 'http' ? 503 : 200,
                  headers: {'Content-Type': 'application/json'}
                }));
              };
              try {
                document.getElementById('btn-orders-refresh').click();
                while (window.__ordersReadError.calls < 1 ||
                       Trader.state._ordersLoadPromise !== null) {
                  await new Promise(requestAnimationFrame);
                }
                const body = document.getElementById('open-orders-body');
                return {
                  unknown: Trader.state.openOrders === null,
                  generic: !!body && body.textContent.includes('currently unavailable'),
                  leaked: !!body && body.textContent.includes('private upstream diagnostic')
                };
              } finally {
                window.fetch = originalFetch;
                delete window.__ordersReadError;
              }
            }""",
            failure_mode,
        )
        assert orders_read_error["unknown"], (
            f"Open-order {failure_mode} failure did not invalidate known state"
        )
        assert orders_read_error["generic"], (
            f"Open-order {failure_mode} failure lacked a generic UI message"
        )
        assert not orders_read_error["leaked"], (
            f"Open-order {failure_mode} failure leaked provider details"
        )
    print("PASS open-order soft/HTTP/network failures stay unknown and generic")

    stops_partial_error = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          window.__stopsPartialError = {calls: 0};
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/orders/open') return originalFetch(input, opts);
            window.__stopsPartialError.calls += 1;
            return Promise.resolve(new Response(JSON.stringify({
              orders: [],
              stop_orders: [],
              stops_error: 'private stop endpoint diagnostic',
              error: null
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            document.getElementById('btn-orders-refresh').click();
            while (window.__stopsPartialError.calls < 1 ||
                   Trader.state._ordersLoadPromise !== null) {
              await new Promise(requestAnimationFrame);
            }
            const text = document.getElementById('open-orders-body').textContent;
            return {
              partialKnown: !!Trader.state.openOrders &&
                !!Trader.state.openOrders.stops_error,
              generic: text.includes('protective orders are currently unavailable'),
              falseEmpty: text.trim() === 'No open orders',
              leaked: text.includes('private stop endpoint diagnostic')
            };
          } finally {
            window.fetch = originalFetch;
            delete window.__stopsPartialError;
          }
        }"""
    )
    assert stops_partial_error["partialKnown"], (
        "Partial stop-order failure discarded the known regular-order result"
    )
    assert stops_partial_error["generic"], (
        "Partial stop-order failure was not visible in the order pane"
    )
    assert not stops_partial_error["falseEmpty"], (
        "Unknown stop-order state was rendered as no open orders"
    )
    assert not stops_partial_error["leaked"], (
        "Partial stop-order failure leaked provider details"
    )
    print("PASS partial stop-order failure stays visible and does not fake empty")

    # Hyperliquid LIMIT is intentionally fail-closed until every later/partial
    # fill can be durably protected. The UI must mirror the backend gate.
    await expect(page.locator('.type-btn[data-type="limit"]')).to_be_disabled()
    await expect(page.locator("#ticket-type")).to_have_value("market")
    print("PASS Hyperliquid limit entry is unavailable in UI")

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.locator("#ticket-sl").fill("61000")
    before = sum(path == "/api/market/ETH" for _, path in calls)
    await page.locator("#symbol-input").fill("ETH")
    await page.locator("#symbol-input").press("Enter")
    assert await page.evaluate(
        "Trader.state.symbol === 'ETH' && Trader.state.market === null && Trader.state.lastPx === null"
    )
    await expect(page.locator("#ticket-sl")).to_have_value("")
    await expect(page.locator("#btn-send-order")).to_be_disabled()
    await page.wait_for_function(
        "Trader.state.market && Trader.state.market.symbol === 'ETH'"
    )
    await page.locator("#ticket-sl").focus()  # blur must not issue a second load
    assert sum(path == "/api/market/ETH" for _, path in calls) - before == 1
    print("PASS symbol switch clears old prices and loads once")

    await page.locator("#ticket-sl").fill("3000")
    await page.evaluate("Trader.loadMarket('ETH', '15m', '1H')")
    await expect(page.locator("#ticket-sl")).to_have_value("3000")
    print("PASS same-symbol refresh preserves ticket")

    async def paste(text):
        await page.locator("#ticket-sl").evaluate(
            """(el, text) => {
          const data = new DataTransfer(); data.setData('text', text);
          el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
        }""",
            text,
        )

    for text, expected in [
        ("0,12345", "0.12345"),
        ("1,234", "1.234"),
        ("61.234,56", "61234.56"),
        ("61,234.56", "61234.56"),
        ("0.000001", "0.000001"),
    ]:
        await paste(text)
        await expect(page.locator("#ticket-sl")).to_have_value(expected)
    await paste("Price 12foo34")
    await expect(page.locator("#ticket-sl")).to_have_value("0.000001")
    print("PASS decimal paste and rejection without overwriting")

    await page.locator('[data-tab="positions"]').focus()
    await page.keyboard.press("ArrowRight")
    await expect(page.locator('[data-tab="orders"]')).to_be_focused()
    await expect(page.locator('[data-pane="orders"]')).to_be_visible()
    await page.keyboard.press("Home")
    await expect(page.locator('[data-tab="positions"]')).to_be_focused()

    await page.locator("#btn-ki-keys").click()
    await expect(page.locator("#ki-provider")).to_be_focused()
    for _ in range(12):
        await page.keyboard.press("Tab")
        assert await page.evaluate(
            "document.activeElement.closest('#ki-keys-modal') !== null"
        )
    await page.keyboard.press("Escape")
    await expect(page.locator("#btn-ki-keys")).to_be_focused()
    assert await page.locator("main").evaluate("el => !el.inert")
    print("PASS tab navigation and modal focus containment/restore")

    await page.locator("#symbol-input").fill("BTC")
    await page.locator("#symbol-input").press("Enter")
    await page.wait_for_function(
        "Trader.state.market && Trader.state.market.symbol === 'BTC'"
    )
    await page.locator("#ticket-usdt").fill("100")
    await page.locator("#ticket-sl").fill("61000")
    await page.locator("#ticket-tp1").fill("68000")
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_disabled()
    await expect(page.locator("#btn-confirm-cancel")).to_be_focused()
    await page.set_viewport_size({"width": 390, "height": 600})
    footer = await page.locator("#btn-confirm-cancel").bounding_box()
    assert footer and footer["y"] >= 0 and footer["y"] + footer["height"] <= 600
    await page.screenshot(path=ROOT / ".superpowers/ui-review/preview-mobile.png")
    await page.keyboard.press("Escape")
    await expect(page.locator("#btn-send-order")).to_be_focused()
    assert not any(path == "/api/orders/confirm" for _, path in calls)
    print("PASS disarmed preview blocks confirm; mobile actions remain visible")

    # Only the intercepted fixture becomes armed. There is no backend/server.
    health["trading_enabled"] = True
    await page.locator('[data-trigger="manual"]').click()
    await page.locator("#btn-send-order").click()
    await page.locator("#manual-ack-box").check()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    account["positions"] = [
        {
            "symbol": "BTC",
            "side": "long",
            "hold_vol": 0.00156,
            "entry_price": 64000,
            "mark_price": 64000,
            "leverage": 5,
            "unrealized_pnl": 0,
            "im": 20,
            "contract_size": 1,
        }
    ]
    await page.locator("#btn-confirm-live").click()
    await expect(page.locator("#confirm-modal")).to_have_attribute("aria-busy", "true")
    await expect(page.locator("#btn-confirm-cancel")).to_be_disabled()
    await page.keyboard.press("Escape")
    await page.locator("#confirm-modal .modal-backdrop").dispatch_event("click")
    await expect(page.locator("#confirm-modal")).to_be_visible()
    assert await page.evaluate("Trader.state._ttlTimer === null")
    await expect(page.locator("#confirm-modal")).to_be_hidden()
    assert sum(path == "/api/orders/confirm" for _, path in calls) == 1
    assert await page.evaluate(
        "Trader.state.tradeMarkers.BTC.sl === 61000 && Trader.state.tradeMarkers.BTC.tp === 68000 && Trader.state.tradeMarkers.BTC.manual"
    )
    assert await page.locator("main").evaluate("el => !el.inert")
    print("PASS delayed confirm cannot dismiss; single request and SL/TP retained")
    await expect(page.locator("#btn-send-order")).to_be_focused()
    await expect(page.locator("#btn-send-order")).to_have_text("Review order")

    await page.evaluate("""() => Trader.renderProposal({symbol: 'BTC', provider: 'claude', proposal: {
      action: 'GO_LONG', entry_price: 64000, stop_loss: 61000, tp1: 68000, rrr: 1.33,
      setup_confidence: 'MEDIUM', rationale: 'Test analysis: assess price structure, volume, and risk distance together.',
      pre_mortem: 'A pullback below support would invalidate the premise.',
      key_levels: {support: 62000, resistance: 68000}
    }})""")
    for width in (1440, 390, 320):
        await page.set_viewport_size({"width": width, "height": 900})
        await page.screenshot(
            path=ROOT / f".superpowers/ui-review/position-{width}.png", full_page=True
        )
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth"
        ), f"Position overflows at {width}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    asyncio.run(run(parser.parse_args().capture_only))
