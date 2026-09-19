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
    preview_contract = {
        "ok": True,
        "token": "offline-preview",
        "expires_in_seconds": 60,
        "errors": [],
        "warnings": [],
    }
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
        "position_reevaluation_allowed": False,
    }
    account = {"equity_usdt": 12500, "available_usdt": 12500, "positions": []}
    providers = [
        {
            "id": "claude",
            "label": "Claude",
            "configured": True,
            "model": "claude-sonnet-5",
            "default_model": "claude-sonnet-5",
        },
        {
            "id": "ollama",
            "label": "Ollama (local)",
            "configured": True,
            "model": "llama3.1",
            "default_model": "llama3.1",
        },
    ]
    llm = {
        "provider": "claude",
        "omit_capability": False,
        "response_provider": None,
        "invalid_provider_row": False,
        "hold_next_get": True,
        "get_started": asyncio.Event(),
        "release_get": asyncio.Event(),
    }
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
        elif path == "/api/llm":
            held_snapshot = None
            if route.request.method == "GET" and llm["hold_next_get"]:
                llm["hold_next_get"] = False
                held_snapshot = {
                    "provider": llm["provider"],
                    "providers": list(providers),
                    "position_reevaluation_allowed": False,
                }
                llm["get_started"].set()
                await llm["release_get"].wait()
            if route.request.method == "POST":
                await asyncio.sleep(0.15)
                llm["provider"] = route.request.post_data_json["provider"]
            if held_snapshot is not None:
                data = held_snapshot
            else:
                data = {
                    "provider": llm["response_provider"] or llm["provider"],
                    "providers": (
                        providers + [None]
                        if llm["invalid_provider_row"]
                        else providers
                    ),
                }
                if not llm["omit_capability"]:
                    data["position_reevaluation_allowed"] = (
                        llm["provider"] == "ollama"
                    )
        elif path == "/api/settings/llm":
            data = {"provider": llm["provider"], "providers": providers}
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
            data = {"supported": True, "fills": [], "error": None}
        elif path == "/api/history":
            data = {"proposals": [], "orders": []}
        elif path == "/api/positions/alerts":
            data = {"alerts": []}
        elif path in ("/api/journal", "/api/journal/stats"):
            data = {"entries": [], "total": 0, "by_confidence": {}, "by_regime": {}}
        elif path == "/api/orders/preview":
            ticket = route.request.post_data_json
            data = {
                "ok": preview_contract["ok"],
                "token": preview_contract["token"],
                "expires_in_seconds": preview_contract["expires_in_seconds"],
                "errors": preview_contract["errors"],
                "warnings": preview_contract["warnings"],
                "gate": {
                    "errors": preview_contract["errors"],
                    "warnings": preview_contract["warnings"],
                },
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
            data = {
                "ok": True,
                "status": "placed",
                "external_oid": "offline-only",
                "sl_verified": True,
                "sl_checked": True,
                "sl_fully_verified": True,
                "sl_detail": "synthetic protection verified",
                "warnings": [],
                "post_errors": [],
            }
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
            await interaction_checks(
                page, calls, health, account, preview_contract, llm
            )
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
            setup_singleflight = await setup_page.evaluate(
                """async () => {
                  const originalFetch = window.fetch.bind(window);
                  let requestCalls = 0;
                  let releaseRequest;
                  window.fetch = (input, opts = {}) => {
                    const raw = input && input.url ? input.url : input;
                    const url = new URL(String(raw), location.href);
                    if (url.pathname !== '/api/setup') {
                      return originalFetch(input, opts);
                    }
                    requestCalls += 1;
                    return new Promise((resolve) => { releaseRequest = resolve; });
                  };
                  try {
                    const exchange = document.querySelector(
                      'input[name="exchange"][value="hl-mainnet"]'
                    );
                    exchange.checked = true;
                    exchange.dispatchEvent(new Event('change', {bubbles: true}));
                    document.getElementById('mainnet-confirm').value = 'MAINNET';
                    document.getElementById('hl-private-key').value =
                      '0x' + 'a'.repeat(64);
                    const provider = document.getElementById('llm-provider');
                    provider.value = 'none';
                    provider.dispatchEvent(new Event('change', {bubbles: true}));

                    const form = document.getElementById('setup-form');
                    const submit = () => form.dispatchEvent(new Event('submit', {
                      bubbles: true, cancelable: true
                    }));
                    submit();
                    submit();
                    await Promise.resolve();
                    const button = document.getElementById('btn-save');
                    const during = {
                      requestCalls,
                      formBusy: form.getAttribute('aria-busy'),
                      buttonDisabled: button.disabled,
                      buttonText: button.textContent.trim()
                    };

                    const success = document.getElementById('setup-ok');
                    const successVisible = new Promise((resolve) => {
                      if (!success.classList.contains('hidden')) {
                        resolve();
                        return;
                      }
                      const observer = new MutationObserver(() => {
                        if (!success.classList.contains('hidden')) {
                          observer.disconnect();
                          resolve();
                        }
                      });
                      observer.observe(success, {attributes: true});
                    });
                    releaseRequest(new Response(JSON.stringify({ok: true}), {
                      status: 200,
                      headers: {'Content-Type': 'application/json'}
                    }));
                    await successVisible;
                    return {
                      during,
                      finalRequestCalls: requestCalls,
                      successVisible: !success.classList.contains('hidden'),
                      successText: success.textContent.trim(),
                      errorHidden: document.getElementById('setup-error')
                        .classList.contains('hidden'),
                      formBusy: form.getAttribute('aria-busy'),
                      buttonDisabled: button.disabled
                    };
                  } finally {
                    window.fetch = originalFetch;
                  }
                }"""
            )
            assert setup_singleflight["during"] == {
                "requestCalls": 1,
                "formBusy": "true",
                "buttonDisabled": True,
                "buttonText": "Saving…",
            }, setup_singleflight
            assert setup_singleflight["finalRequestCalls"] == 1, setup_singleflight
            assert setup_singleflight["successVisible"] is True, setup_singleflight
            assert setup_singleflight["successText"] == (
                "Mainnet configuration saved. Trading remains disabled. "
                "Complete the Mainnet safety checklist and set the separate "
                "Mainnet acknowledgement before arming."
            ), setup_singleflight
            assert setup_singleflight["errorHidden"] is True, setup_singleflight
            assert setup_singleflight["formBusy"] == "true", setup_singleflight
            assert setup_singleflight["buttonDisabled"] is True, setup_singleflight
            await setup_page.close()
            print("PASS English first-run setup is responsive and interactive")
            print("PASS first-run setup submit is singleflight and accessible")
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


async def interaction_checks(page, calls, health, account, preview_contract, llm):
    health["trading_enabled"] = True
    health["exchange_configured"] = False
    await page.evaluate("() => Trader.loadHealth()")
    not_ready = await page.evaluate(
        """() => ({
          status: document.querySelector('#arm-status .arm-text').textContent.trim(),
          previewDisabled: document.getElementById('btn-send-order').disabled,
          previewTitle: document.getElementById('btn-send-order').title
        })"""
    )
    assert not_ready == {
        "status": "ARMED · EXCHANGE NOT READY",
        "previewDisabled": True,
        "previewTitle": (
            "Exchange credentials are incomplete; order preview is unavailable"
        ),
    }
    health["trading_enabled"] = False
    health["exchange_configured"] = True
    await page.evaluate("() => Trader.loadHealth()")
    print("PASS armed but unconfigured exchange is visibly blocked")

    sizing_stale = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const sl = document.getElementById('ticket-sl');
          const usdt = document.getElementById('ticket-usdt');
          const leverage = document.getElementById('ticket-leverage');
          const button = document.getElementById('btn-suggest-vol');
          const originalValues = {
            sl: sl.value, usdt: usdt.value, leverage: leverage.value
          };
          const pending = [];
          const waitForCalls = async (count) => {
            for (let i = 0; i < 100 && pending.length < count; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            if (pending.length < count) throw new Error('sizing request did not start');
          };
          const reply = (index, notional) => pending[index].resolve(
            new Response(JSON.stringify({
              vol: 0.01, notional_usdt: notional, base_amount: 0.001
            }), {status: 200, headers: {'Content-Type': 'application/json'}})
          );
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/sizing/suggest') {
              return originalFetch(input, opts);
            }
            return new Promise((resolve) => pending.push({
              resolve,
              body: JSON.parse(String(opts.body || '{}'))
            }));
          };
          try {
            leverage.value = '5';
            usdt.value = '100';
            sl.value = '62000';
            button.click();
            await waitForCalls(1);
            sl.value = '61000';
            sl.dispatchEvent(new Event('input', {bubbles: true}));
            reply(0, 900);
            await new Promise((resolve) => setTimeout(resolve, 20));
            const afterChangedInput = usdt.value;

            usdt.value = '100';
            sl.value = '62000';
            button.click();
            await waitForCalls(2);
            sl.value = '61000';
            sl.dispatchEvent(new Event('input', {bubbles: true}));
            button.click();
            await waitForCalls(3);
            reply(2, 222);
            for (let i = 0; i < 100 && usdt.value !== '222'; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const afterLatest = usdt.value;
            reply(1, 999);
            await new Promise((resolve) => setTimeout(resolve, 20));
            return {
              requestedStops: pending.map((item) => item.body.stop_loss),
              afterChangedInput,
              afterLatest,
              afterOlder: usdt.value
            };
          } finally {
            window.fetch = originalFetch;
            sl.value = originalValues.sl;
            usdt.value = originalValues.usdt;
            leverage.value = originalValues.leverage;
            sl.dispatchEvent(new Event('input', {bubbles: true}));
            const toast = document.getElementById('toast');
            if (toast) toast.classList.add('hidden');
          }
        }"""
    )
    assert sizing_stale == {
        "requestedStops": [62000, 62000, 61000],
        "afterChangedInput": "100",
        "afterLatest": "222",
        "afterOlder": "222",
    }
    print("PASS sizing suggestions discard changed-input and superseded responses")

    stale_preview = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const sl = document.getElementById('ticket-sl');
          const tp = document.getElementById('ticket-tp1');
          const usdt = document.getElementById('ticket-usdt');
          const originalValues = {sl: sl.value, tp: tp.value, usdt: usdt.value};
          let releasePreview;
          let requestBody = null;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/orders/preview') {
              return originalFetch(input, opts);
            }
            requestBody = JSON.parse(String(opts.body || '{}'));
            return new Promise((resolve) => { releasePreview = resolve; });
          };
          try {
            usdt.value = '100';
            sl.value = '62000';
            tp.value = '68000';
            document.getElementById('btn-send-order').click();
            for (let i = 0; i < 100 && !releasePreview; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            if (!releasePreview) throw new Error('preview request did not start');
            sl.value = '61000';
            sl.dispatchEvent(new Event('input', {bubbles: true}));
            releasePreview(new Response(JSON.stringify({
              ok: true,
              token: 'stale-offline-preview',
              expires_in_seconds: 60,
              summary: {
                symbol: 'BTC', side: 'long', order_type: 'market',
                vol: 0.001, leverage: 5, notional_usdt: 100,
                entry_for_risk: 64000, stop_loss: 62000,
                take_profit: 68000, risk_usdt: 2, risk_pct: 0.016,
                rrr: 2, trigger_mode: 'auto'
              }
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const modal = document.getElementById('confirm-modal');
            const error = document.getElementById('ticket-error');
            return {
              requestedStop: requestBody && requestBody.stop_loss,
              currentStop: Number(sl.value),
              modalHidden: modal.classList.contains('hidden'),
              previewToken: Trader.state.previewToken,
              errorVisible: !error.classList.contains('hidden'),
              errorText: error.textContent.trim()
            };
          } finally {
            window.fetch = originalFetch;
            const modal = document.getElementById('confirm-modal');
            if (!modal.classList.contains('hidden')) {
              document.getElementById('btn-confirm-cancel').click();
            }
            sl.value = originalValues.sl;
            tp.value = originalValues.tp;
            usdt.value = originalValues.usdt;
            sl.dispatchEvent(new Event('input', {bubbles: true}));
            const toast = document.getElementById('toast');
            if (toast) toast.classList.add('hidden');
          }
        }"""
    )
    assert stale_preview == {
        "requestedStop": 62000,
        "currentStop": 61000,
        "modalHidden": True,
        "previewToken": None,
        "errorVisible": True,
        "errorText": (
            "Ticket inputs changed while risk gates were checked. "
            "Review the updated order again."
        ),
    }
    print("PASS changed ticket cannot open a stale Preview-to-Confirm modal")

    health["trading_enabled"] = True
    await page.evaluate("() => Trader.loadHealth()")
    await page.locator("#ticket-usdt").fill("100")
    await page.locator("#ticket-sl").fill("61000")
    await page.locator("#ticket-tp1").fill("68000")
    preview_contract["expires_in_seconds"] = '<img id="ttl-injection" src="x">'
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_hidden()
    assert await page.locator("#ttl-injection").count() == 0
    assert await page.evaluate("Trader.state.previewToken === null")
    await expect(page.locator("#ticket-error")).to_contain_text(
        "Invalid preview response"
    )
    preview_contract["expires_in_seconds"] = 60

    preview_contract.update(
        ok=False,
        token=None,
        errors=["Synthetic risk gate rejected this order"],
        warnings=["Synthetic warning remains visible"],
    )
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_disabled()
    await expect(page.locator("#confirm-body")).to_contain_text(
        "Synthetic risk gate rejected this order"
    )
    await expect(page.locator("#confirm-body")).to_contain_text(
        "Synthetic warning remains visible"
    )
    assert await page.evaluate("Trader.state.previewToken === null")
    await page.keyboard.press("Escape")

    preview_contract.update(
        ok=True,
        token="offline-preview",
        errors="SYNTHETIC_MALFORMED_ERRORS",
        warnings=[],
    )
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_hidden()
    await expect(page.locator("#ticket-error")).to_contain_text(
        "Invalid preview response"
    )
    assert await page.evaluate("Trader.state.previewToken === null")

    preview_contract.update(errors=[], warnings=[])
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    print("PASS preview response contract exposes gate blockers and rejects malformed 200s")

    confirm_calls_before = sum(
        path == "/api/orders/confirm" for _, path in calls
    )
    health["exchange_configured"] = False
    await page.locator("#btn-confirm-live").click()
    await page.wait_for_function("!Trader.state.orderBusy")
    assert sum(path == "/api/orders/confirm" for _, path in calls) == confirm_calls_before
    await expect(page.locator("#confirm-error")).to_contain_text(
        "EXCHANGE NOT READY"
    )
    health["exchange_configured"] = True
    await page.locator("#btn-confirm-cancel").click()
    await page.evaluate("() => Trader.loadHealth()")
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    print("PASS fresh exchange readiness is required before confirmation")

    stale_health_confirm = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalConsoleError = console.error;
          let confirmCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/health') {
              return Promise.resolve(new Response('', {status: 503}));
            }
            if (url.pathname === '/api/orders/confirm') confirmCalls += 1;
            return originalFetch(input, opts);
          };
          console.error = () => {};
          try {
            document.getElementById('btn-confirm-live').click();
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            return {
              confirmCalls,
              staleArmedState: Trader.state.health.trading_enabled,
              errorText: document.getElementById('confirm-error').textContent.trim()
            };
          } finally {
            window.fetch = originalFetch;
            console.error = originalConsoleError;
          }
        }"""
    )
    assert stale_health_confirm == {
        "confirmCalls": 0,
        "staleArmedState": True,
        "errorText": (
            "Unable to verify the current trading state. Review the order again."
        ),
    }
    await page.locator("#btn-confirm-cancel").click()
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    print("PASS failed health refresh cannot reuse stale armed state for confirmation")

    invalid_confirm = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalAccount = Trader.state.account;
          const accountSnapshot = {
            equity_usdt: 12500, available_usdt: 12000,
            positions: [{
              symbol: 'BTC', side: 'long', hold_vol: 0.01,
              entry_price: 64000, leverage: 2, liquidate_price: 50000,
              unrealized_pnl: 0, contract_size: 1
            }]
          };
          let confirmCalls = 0;
          let previewCalls = 0;
          let accountCalls = 0;
          let orderCalls = 0;
          const accountResolvers = [];
          const orderResolvers = [];
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/orders/preview') previewCalls += 1;
            if (url.pathname === '/api/account') {
              accountCalls += 1;
              return new Promise((resolve) => { accountResolvers.push(resolve); });
            }
            if (url.pathname === '/api/orders/open') {
              orderCalls += 1;
              return new Promise((resolve) => { orderResolvers.push(resolve); });
            }
            if (url.pathname !== '/api/orders/confirm') {
              return originalFetch(input, opts);
            }
            confirmCalls += 1;
            if (confirmCalls > 1) {
              return Promise.resolve(new Response(JSON.stringify({
                detail: 'synthetic upstream failure'
              }), {
                status: 502, headers: {'Content-Type': 'application/json'}
              }));
            }
            return Promise.resolve(new Response(JSON.stringify({
              ok: true,
              status: 'placed',
              external_oid: 'offline-missing-protection-flags'
            }), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
          };
          try {
            Trader.state.account = accountSnapshot;
            Trader.renderPositions(accountSnapshot);
            void Trader.loadAccount();
            document.getElementById('btn-orders-refresh').click();
            for (let i = 0; i < 100 &&
                 (accountCalls < 1 || orderCalls < 1); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.getElementById('btn-confirm-live').click();
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const error = document.getElementById('confirm-error');
            const toast = document.getElementById('toast');
            const unknownOutcome = {
              latched: Trader.state.confirmOutcomeUnknown,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              closeEnabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => !item.disabled),
              stopEnabled: !document.querySelector('.cp-sl-edit-btn').disabled,
              killswitchEnabled: !document.getElementById('btn-killswitch').disabled,
              accountCalls,
              orderCalls,
              toastText: toast.textContent.trim()
            };
            const invalidResponseErrorText = error.textContent.trim();
            document.getElementById('btn-confirm-cancel').click();
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            await Promise.resolve();
            const previewBlocked = {
              previewCalls,
              ticketError: document.getElementById('ticket-error').textContent.trim()
            };
            accountResolvers[0](new Response(JSON.stringify(accountSnapshot), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            orderResolvers[0](new Response(JSON.stringify({orders: [], stop_orders: []}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 &&
                 (accountCalls < 2 || orderCalls < 2); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const afterPreexistingReads = {
              latched: Trader.state.confirmOutcomeUnknown,
              accountReady: Trader.state._confirmUnknownAccountReady,
              ordersReady: Trader.state._confirmUnknownOrdersReady,
              accountCalls,
              orderCalls
            };
            accountResolvers[1](new Response(JSON.stringify(accountSnapshot), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 &&
                 !Trader.state._confirmUnknownAccountReady; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const afterAccountOnly = {
              latched: Trader.state.confirmOutcomeUnknown,
              entryDisabled: document.getElementById('btn-send-order').disabled
            };
            orderResolvers[1](new Response(JSON.stringify({orders: [], stop_orders: []}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 && Trader.state.confirmOutcomeUnknown; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const afterReconciliation = {
              latched: Trader.state.confirmOutcomeUnknown,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              toastText: toast.textContent.trim()
            };
            Trader.state.previewToken = 'synthetic-server-error-token';
            await Trader.runConfirm();
            for (let i = 0; i < 100 &&
                 (accountCalls < 3 || orderCalls < 3); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const serverErrorOutcome = {
              latched: Trader.state.confirmOutcomeUnknown,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              errorText: error.textContent.trim(),
              toastText: toast.textContent.trim(),
              accountCalls,
              orderCalls
            };
            accountResolvers[2](new Response(JSON.stringify(accountSnapshot), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            orderResolvers[2](new Response(JSON.stringify({orders: [], stop_orders: []}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 && Trader.state.confirmOutcomeUnknown; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            return {
              confirmCalls,
              modalHidden: document.getElementById('confirm-modal')
                .classList.contains('hidden'),
              previewToken: Trader.state.previewToken,
              confirmDisabled: document.getElementById('btn-confirm-live').disabled,
              size: document.getElementById('ticket-usdt').value,
              stop: document.getElementById('ticket-sl').value,
              takeProfit: document.getElementById('ticket-tp1').value,
              errorVisible: !error.classList.contains('hidden'),
              errorText: invalidResponseErrorText,
              unknownOutcome,
              previewBlocked,
              afterPreexistingReads,
              afterAccountOnly,
              afterReconciliation,
              serverErrorOutcome,
              serverErrorAfterReconciliation: {
                latched: Trader.state.confirmOutcomeUnknown,
                entryDisabled: document.getElementById('btn-send-order').disabled
              }
            };
          } finally {
            window.fetch = originalFetch;
            Trader.state.account = originalAccount;
            Trader.renderPositions(originalAccount || {positions: []});
          }
        }"""
    )
    assert invalid_confirm == {
        "confirmCalls": 2,
        "modalHidden": True,
        "previewToken": None,
        "confirmDisabled": True,
        "size": "100",
        "stop": "61000",
        "takeProfit": "68000",
        "errorVisible": True,
        "errorText": (
            "CONFIRMATION RESPONSE INVALID — the order may already be placed. "
            "Do not submit it again. Check positions and orders on the exchange."
        ),
        "unknownOutcome": {
            "latched": True,
            "entryDisabled": True,
            "closeEnabled": True,
            "stopEnabled": True,
            "killswitchEnabled": True,
            "accountCalls": 1,
            "orderCalls": 1,
            "toastText": (
                "⚠ Confirmation outcome unknown — the order may be LIVE. Check the "
                "exchange and do not confirm again."
            ),
        },
        "previewBlocked": {
            "previewCalls": 0,
            "ticketError": (
                "Previous confirmation outcome is unknown. Wait for account and "
                "open-order reconciliation before reviewing another order."
            ),
        },
        "afterPreexistingReads": {
            "latched": True,
            "accountReady": False,
            "ordersReady": False,
            "accountCalls": 2,
            "orderCalls": 2,
        },
        "afterAccountOnly": {"latched": True, "entryDisabled": True},
        "afterReconciliation": {
            "latched": False,
            "entryDisabled": False,
            "toastText": (
                "Exchange state refreshed — review positions and open orders before "
                "creating another order."
            ),
        },
        "serverErrorOutcome": {
            "latched": True,
            "entryDisabled": True,
            "errorText": (
                "SERVER ERROR while confirming (HTTP 502) — the order may already "
                "be placed. Do not submit it again. Check positions and orders on "
                "the exchange."
            ),
            "toastText": (
                "⚠ Confirmation outcome unknown after server error — the order may "
                "be LIVE. Check the exchange and do not confirm again."
            ),
            "accountCalls": 3,
            "orderCalls": 3,
        },
        "serverErrorAfterReconciliation": {
            "latched": False,
            "entryDisabled": False,
        },
    }
    print("PASS pre-confirm reads cannot clear the unknown-outcome retry block")
    print("PASS invalid 2xx confirm blocks retry until fresh exchange reconciliation")
    print("PASS HTTP 502 confirm is unknown until fresh exchange reconciliation")
    await page.keyboard.press("Escape")
    await expect(page.locator("#confirm-modal")).to_be_hidden()
    health["trading_enabled"] = False
    await page.evaluate("() => Trader.loadHealth()")
    await page.locator("#ticket-usdt").fill("")
    await page.locator("#ticket-sl").fill("")
    await page.locator("#ticket-tp1").fill("")

    health["trading_enabled"] = True
    await page.evaluate("() => Trader.loadHealth()")
    await page.locator("#ticket-usdt").fill("100")
    await page.locator("#ticket-sl").fill("61000")
    await page.locator("#ticket-tp1").fill("68000")
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    partial_confirm = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          let confirmCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/orders/confirm') {
              return originalFetch(input, opts);
            }
            confirmCalls += 1;
            return Promise.resolve(new Response(JSON.stringify({
              ok: true,
              status: 'placed_partial_fill',
              external_oid: 'offline-partial-fill',
              sl_verified: true,
              sl_checked: true,
              sl_fully_verified: false,
              sl_detail: 'synthetic partial coverage',
              warnings: ['synthetic resting remainder is unprotected'],
              post_errors: []
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            document.getElementById('btn-confirm-live').click();
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            return {
              confirmCalls,
              modalHidden: document.getElementById('confirm-modal')
                .classList.contains('hidden'),
              previewToken: Trader.state.previewToken,
              size: document.getElementById('ticket-usdt').value,
              stop: document.getElementById('ticket-sl').value,
              takeProfit: document.getElementById('ticket-tp1').value,
              toastError: toast.classList.contains('err'),
              toastText: toast.textContent.trim()
            };
          } finally {
            window.fetch = originalFetch;
          }
        }"""
    )
    assert partial_confirm == {
        "confirmCalls": 1,
        "modalHidden": True,
        "previewToken": None,
        "size": "100",
        "stop": "",
        "takeProfit": "",
        "toastError": True,
        "toastText": (
            "PARTIAL FILL: the stop protects only the confirmed fill. The resting "
            "remainder can fill without protection. Check or cancel it on the "
            "exchange now."
        ),
    }
    print("PASS partial-fill confirmation cannot claim full stop protection")

    await page.locator("#ticket-sl").fill("61000")
    await page.locator("#ticket-tp1").fill("68000")
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    recovered_confirm = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          let confirmCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/orders/confirm') {
              return originalFetch(input, opts);
            }
            confirmCalls += 1;
            return Promise.resolve(new Response(JSON.stringify({
              ok: true,
              status: 'recovered_placed',
              external_oid: 'offline-recovered-order',
              sl_verified: true,
              sl_checked: true,
              sl_fully_verified: true,
              sl_detail: 'synthetic protection verified',
              warnings: ['synthetic transport error recovered by external id'],
              post_errors: []
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            document.getElementById('btn-confirm-live').click();
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            return {
              confirmCalls,
              modalHidden: document.getElementById('confirm-modal')
                .classList.contains('hidden'),
              previewToken: Trader.state.previewToken,
              size: document.getElementById('ticket-usdt').value,
              stop: document.getElementById('ticket-sl').value,
              takeProfit: document.getElementById('ticket-tp1').value,
              toastError: toast.classList.contains('err'),
              toastText: toast.textContent.trim()
            };
          } finally {
            window.fetch = originalFetch;
          }
        }"""
    )
    assert recovered_confirm == {
        "confirmCalls": 1,
        "modalHidden": True,
        "previewToken": None,
        "size": "100",
        "stop": "",
        "takeProfit": "",
        "toastError": True,
        "toastText": (
            "ORDER RECOVERED AFTER TRANSPORT ERROR: the exchange reports it as "
            "placed. Verify the order, position, and protection now. Do not submit "
            "it again."
        ),
    }
    print("PASS recovered confirmation cannot look like an ordinary clean placement")

    await page.locator("#ticket-sl").fill("61000")
    await page.locator("#ticket-tp1").fill("68000")
    await page.locator("#btn-send-order").click()
    await expect(page.locator("#confirm-modal")).to_be_visible()
    await expect(page.locator("#btn-confirm-live")).to_be_enabled()
    audit_failed_confirm = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          let confirmCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/orders/confirm') {
              return originalFetch(input, opts);
            }
            confirmCalls += 1;
            return Promise.resolve(new Response(JSON.stringify({
              ok: true,
              status: 'placed',
              external_oid: 'offline-audit-failed-order',
              sl_verified: true,
              sl_checked: true,
              sl_fully_verified: true,
              sl_detail: 'synthetic protection verified',
              warnings: [],
              post_errors: ['audit log failed: synthetic sqlite failure']
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            document.getElementById('btn-confirm-live').click();
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            return {
              confirmCalls,
              modalHidden: document.getElementById('confirm-modal')
                .classList.contains('hidden'),
              previewToken: Trader.state.previewToken,
              size: document.getElementById('ticket-usdt').value,
              stop: document.getElementById('ticket-sl').value,
              takeProfit: document.getElementById('ticket-tp1').value,
              toastError: toast.classList.contains('err'),
              toastText: toast.textContent.trim()
            };
          } finally {
            window.fetch = originalFetch;
          }
        }"""
    )
    assert audit_failed_confirm == {
        "confirmCalls": 1,
        "modalHidden": True,
        "previewToken": None,
        "size": "100",
        "stop": "",
        "takeProfit": "",
        "toastError": True,
        "toastText": (
            "ORDER PLACED, BUT LOCAL RECORDING IS INCOMPLETE. Verify the order and "
            "protection on the exchange. Do not submit it again."
        ),
    }
    print("PASS post-placement audit failure cannot look like a clean placement")
    health["trading_enabled"] = False
    await page.evaluate("() => Trader.loadHealth()")

    killswitch_busy = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalConfirm = window.confirm;
          const originalAccount = Trader.state.account;
          const originalToken = Trader.state.previewToken;
          const originalMgmt = Trader.state.positionMgmt;
          const originalMgmtKnown = Trader.state.positionMgmtKnown;
          let releaseRequest;
          let releaseAlerts;
          let requestCalls = 0;
          let alertCalls = 0;
          let previewCalls = 0;
          let confirmCalls = 0;
          let closeCalls = 0;
          let armCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/orders/preview') previewCalls += 1;
            if (url.pathname === '/api/orders/confirm') confirmCalls += 1;
            if (url.pathname === '/api/orders/close') closeCalls += 1;
            if (url.pathname === '/api/positions/arm') armCalls += 1;
            if (url.pathname === '/api/positions/alerts') {
              alertCalls += 1;
              if (alertCalls > 1) {
                return Promise.resolve(new Response(JSON.stringify({alerts: [{
                  symbol: 'BTC', side: 'long',
                  armed_rules: {auto_be: false, auto_trail: false}, alerts: {}
                }]}), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
              return new Promise((resolve) => { releaseAlerts = resolve; });
            }
            if (url.pathname !== '/api/positions/killswitch') {
              return originalFetch(input, opts);
            }
            requestCalls += 1;
            if (requestCalls === 2) {
              return Promise.resolve(new Response(JSON.stringify({}), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (requestCalls > 2) {
              return Promise.resolve(new Response(JSON.stringify({disarmed: 1}), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            return new Promise((resolve) => { releaseRequest = resolve; });
          };
          window.confirm = () => true;
          try {
            Trader.state.account = {
              equity_usdt: 12500, available_usdt: 12000,
              positions: [{
                symbol: 'BTC', side: 'long', hold_vol: 0.01,
                entry_price: 64000, leverage: 2, liquidate_price: 50000,
                unrealized_pnl: 0, contract_size: 1
              }]
            };
            Trader.renderPositions(Trader.state.account);
            const button = document.getElementById('btn-killswitch');
            button.click();
            await Promise.resolve();
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            Trader.state.previewToken = 'killswitch-busy-probe';
            await Trader.runConfirm();
            Trader.state.previewToken = originalToken;
            document.querySelector('.cp-close-btn').click();
            document.querySelector('.cp-arm-btn').click();
            const during = {
              disabled: button.disabled,
              ariaBusy: button.getAttribute('aria-busy'),
              text: button.textContent.trim(),
              armDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                .every((item) => item.disabled),
              entryDisabled: document.getElementById('btn-send-order').disabled,
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => item.disabled),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              previewCalls,
              confirmCalls,
              closeCalls,
              armCalls
            };
            button.click();
            await Promise.resolve();
            releaseRequest(new Response(JSON.stringify({disarmed: 2}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 && alertCalls < 1; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            Trader.state.previewToken = 'killswitch-reconcile-probe';
            await Trader.runConfirm();
            Trader.state.previewToken = originalToken;
            const reconciling = {
              stateBusy: Trader.state.killswitchBusy,
              disabled: button.disabled,
              ariaBusy: button.getAttribute('aria-busy'),
              entryDisabled: document.getElementById('btn-send-order').disabled,
              previewCalls,
              confirmCalls
            };
            releaseAlerts(new Response(JSON.stringify({alerts: [{
              symbol: 'SYNTHETIC_PRIVATE_WRONG_SYMBOL', side: 'long',
              armed_rules: {auto_be: false, auto_trail: false}, alerts: {}
            }]}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 && Trader.state.killswitchBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            const reconciliationOutcome = {
              errorStyle: toast.classList.contains('err'),
              text: toast.textContent.trim()
            };
            const unknownAfter = {
              disabled: button.disabled,
              ariaBusy: button.getAttribute('aria-busy'),
              text: button.textContent.trim(),
              armDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                .every((item) => item.disabled),
              armLabel: document.querySelector('.cp-arm-btn').textContent.trim(),
              armTitle: document.querySelector('.cp-arm-btn').title,
              positionMgmtKnown: Trader.state.positionMgmtKnown,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .some((item) => item.disabled),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled
            };
            button.click();
            for (let i = 0; i < 100 &&
                 (requestCalls < 2 || Trader.state.killswitchBusy); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const malformedOutcome = {
              errorStyle: toast.classList.contains('err'),
              text: toast.textContent.trim()
            };
            button.click();
            for (let i = 0; i < 100 &&
                 (requestCalls < 3 || Trader.state.killswitchBusy); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const validOutcome = {
              okStyle: toast.classList.contains('ok'),
              text: toast.textContent.trim()
            };
            return {
              calls: requestCalls,
              during,
              reconciling,
              reconciliationOutcome,
              malformedOutcome,
              validOutcome,
              unknownAfter,
              after: {
                disabled: button.disabled,
                ariaBusy: button.getAttribute('aria-busy'),
                armEnabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                  .every((item) => !item.disabled),
                positionMgmtKnown: Trader.state.positionMgmtKnown,
                entryDisabled: document.getElementById('btn-send-order').disabled,
                closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                  .some((item) => item.disabled),
                stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled
              }
            };
          } finally {
            window.fetch = originalFetch;
            window.confirm = originalConfirm;
            Trader.state.previewToken = originalToken;
            Trader.state.positionMgmt = originalMgmt;
            Trader.state.positionMgmtKnown = originalMgmtKnown;
            Trader.state.account = originalAccount;
            Trader.renderPositions(originalAccount || {positions: []});
            const toast = document.getElementById('toast');
            if (toast) toast.classList.add('hidden');
          }
        }"""
    )
    assert killswitch_busy["calls"] == 3
    assert killswitch_busy["during"] == {
        "disabled": True,
        "ariaBusy": "true",
        "text": "Disabling automation…",
        "armDisabled": True,
        "entryDisabled": True,
        "closeDisabled": True,
        "stopDisabled": True,
        "previewCalls": 0,
        "confirmCalls": 0,
        "closeCalls": 0,
        "armCalls": 0,
    }
    assert killswitch_busy["reconciling"] == {
        "stateBusy": True,
        "disabled": True,
        "ariaBusy": "true",
        "entryDisabled": True,
        "previewCalls": 0,
        "confirmCalls": 0,
    }
    assert killswitch_busy["reconciliationOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Kill-switch outcome unknown — wait for server status before acting again."
        ),
    }
    assert killswitch_busy["malformedOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Kill-switch outcome unknown — wait for server status before acting again."
        ),
    }
    assert killswitch_busy["validOutcome"] == {
        "okStyle": True,
        "text": "Automation disabled for 1 position(s).",
    }
    assert killswitch_busy["unknownAfter"] == {
        "disabled": False,
        "ariaBusy": "false",
        "text": "⏻ Disable all automation",
        "armDisabled": True,
        "armLabel": "⚡ Auto-BE: Unknown",
        "armTitle": "Automation status is unavailable; wait for a successful refresh",
        "positionMgmtKnown": False,
        "entryDisabled": False,
        "closeDisabled": False,
        "stopDisabled": False,
    }
    assert killswitch_busy["after"] == {
        "disabled": False,
        "ariaBusy": "false",
        "armEnabled": True,
        "positionMgmtKnown": True,
        "entryDisabled": False,
        "closeDisabled": False,
        "stopDisabled": False,
    }
    print("PASS invalid 2xx kill-switch response cannot claim automation is disabled")
    print("PASS kill-switch success waits for server-state reconciliation")
    print("PASS cross-symbol automation feed cannot reconcile kill-switch state")
    print("PASS automation kill-switch exposes one consistent busy state")

    arm_busy = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalConfirm = window.confirm;
          const originalAccount = Trader.state.account;
          const originalMgmt = Trader.state.positionMgmt;
          const originalMgmtKnown = Trader.state.positionMgmtKnown;
          const originalToken = Trader.state.previewToken;
          const probeAccount = {
            equity_usdt: 12500, available_usdt: 12000,
            positions: [{
              symbol: 'BTC', side: 'long', hold_vol: 0.01,
              entry_price: 64000, leverage: 2, liquidate_price: 50000,
              unrealized_pnl: 0, contract_size: 1
            }]
          };
          let releaseArm;
          let releaseAlerts;
          let armCalls = 0;
          const armBodies = [];
          let alertCalls = 0;
          let previewCalls = 0;
          let confirmCalls = 0;
          let closeCalls = 0;
          let cancelCalls = 0;
          let killswitchCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/orders/open') {
              return Promise.resolve(new Response(JSON.stringify({
                orders: [{orderId: 601, symbol: 'BTC', side: 'buy', vol: 0.01, price: 62000}],
                stop_orders: []
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (url.pathname === '/api/account') {
              return Promise.resolve(new Response(JSON.stringify(probeAccount), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/positions/alerts') {
              alertCalls += 1;
              if (alertCalls > 1) {
                return Promise.resolve(new Response(JSON.stringify({alerts: [{
                  symbol: 'BTC', side: 'long',
                  armed_rules: {auto_be: false, auto_trail: false}, alerts: {}
                }]}), {status: 200, headers: {'Content-Type': 'application/json'}}));
              }
              return new Promise((resolve) => { releaseAlerts = resolve; });
            }
            if (url.pathname === '/api/orders/preview') previewCalls += 1;
            if (url.pathname === '/api/orders/confirm') confirmCalls += 1;
            if (url.pathname === '/api/orders/close') closeCalls += 1;
            if (url.pathname === '/api/orders/cancel') cancelCalls += 1;
            if (url.pathname === '/api/positions/killswitch') killswitchCalls += 1;
            if (url.pathname !== '/api/positions/arm') {
              return originalFetch(input, opts);
            }
            armCalls += 1;
            armBodies.push(JSON.parse(opts.body));
            if (armCalls > 1) {
              return Promise.resolve(new Response(JSON.stringify({
                symbol: 'BTC', side: 'long', armed_rules: {}
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return new Promise((resolve) => { releaseArm = resolve; });
          };
          window.confirm = () => true;
          try {
            Trader.state.account = probeAccount;
            Trader.state.positionMgmt = {
              'BTC|long': {
                symbol: 'BTC', side: 'long',
                armed_rules: {auto_be: false, auto_trail: false}, alerts: {}
              }
            };
            Trader.state.positionMgmtKnown = true;
            Trader.renderPositions(probeAccount);
            document.getElementById('btn-orders-refresh').click();
            for (let i = 0; i < 100 &&
                 !document.querySelector('.btn-cancel-order'); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.querySelector('.cp-arm-btn[data-action="arm-be"]').click();
            await Promise.resolve();
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            Trader.state.previewToken = 'arm-busy-probe';
            await Trader.runConfirm();
            Trader.state.previewToken = originalToken;
            document.querySelector('.cp-close-btn').click();
            document.querySelector('.btn-cancel-order').click();
            document.querySelector('.cp-arm-btn[data-action="arm-trail"]').click();
            document.getElementById('btn-killswitch').click();
            await Promise.resolve();
            const active = document.querySelector('.cp-arm-btn[data-action="arm-be"]');
            const during = {
              ownAriaBusy: active.getAttribute('aria-busy'),
              ownLabel: active.textContent.trim(),
              allArmDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                .every((item) => item.disabled),
              killswitchDisabled: document.getElementById('btn-killswitch').disabled,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              cancelDisabled: document.querySelector('.btn-cancel-order').disabled,
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => item.disabled),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              previewCalls,
              confirmCalls,
              closeCalls,
              cancelCalls,
              killswitchCalls
            };
            releaseArm(new Response(JSON.stringify({ok: true}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 && alertCalls < 1; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            Trader.state.previewToken = 'arm-reconcile-probe';
            await Trader.runConfirm();
            Trader.state.previewToken = originalToken;
            const reconcilingActive = document.querySelector(
              '.cp-arm-btn[data-action="arm-be"]'
            );
            const reconciling = {
              stateBusy: Object.values(Trader.state.armBusy).some(Boolean),
              ownAriaBusy: reconcilingActive.getAttribute('aria-busy'),
              ownLabel: reconcilingActive.textContent.trim(),
              entryDisabled: document.getElementById('btn-send-order').disabled,
              previewCalls,
              confirmCalls
            };
            releaseAlerts(new Response(JSON.stringify({alerts: [{
              symbol: 'BTC', side: 'long',
              armed_rules: {auto_be: true, auto_trail: false}, alerts: {}
            }]}), {status: 200, headers: {'Content-Type': 'application/json'}}));
            for (let i = 0; i < 100 &&
                 Object.values(Trader.state.armBusy).some(Boolean); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            const invalidOutcome = {
              errorStyle: toast.classList.contains('err'),
              text: toast.textContent.trim()
            };
            document.querySelector('.cp-arm-btn[data-action="arm-be"]').click();
            for (let i = 0; i < 100 &&
                 (armCalls < 2 || Object.values(Trader.state.armBusy).some(Boolean)); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const validOutcome = {
              okStyle: toast.classList.contains('ok'),
              text: toast.textContent.trim()
            };
            return {
              armCalls,
              armBodies,
              during,
              reconciling,
              invalidOutcome,
              validOutcome,
              after: {
                armEnabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                  .every((item) => !item.disabled),
                positionMgmtKnown: Trader.state.positionMgmtKnown,
                killswitchDisabled: document.getElementById('btn-killswitch').disabled,
                entryDisabled: document.getElementById('btn-send-order').disabled,
                cancelDisabled: document.querySelector('.btn-cancel-order').disabled,
                closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                  .some((item) => item.disabled),
                stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled
              }
            };
          } finally {
            window.fetch = originalFetch;
            window.confirm = originalConfirm;
            Trader.state.previewToken = originalToken;
            Trader.state.positionMgmt = originalMgmt;
            Trader.state.positionMgmtKnown = originalMgmtKnown;
            Trader.state.account = originalAccount;
            Trader.renderPositions(originalAccount || {positions: []});
            document.getElementById('btn-orders-refresh').click();
            const finalToast = document.getElementById('toast');
            if (finalToast) finalToast.classList.add('hidden');
          }
        }"""
    )
    assert arm_busy["armCalls"] == 2
    assert arm_busy["armBodies"] == [
        {"symbol": "BTC", "side": "long", "rules": {"auto_be": True}},
        {"symbol": "BTC", "side": "long", "rules": {"auto_be": False}},
    ]
    assert arm_busy["during"] == {
        "ownAriaBusy": "true",
        "ownLabel": "Updating Auto-BE…",
        "allArmDisabled": True,
        "killswitchDisabled": True,
        "entryDisabled": True,
        "cancelDisabled": True,
        "closeDisabled": True,
        "stopDisabled": True,
        "previewCalls": 0,
        "confirmCalls": 0,
        "closeCalls": 0,
        "cancelCalls": 0,
        "killswitchCalls": 0,
    }
    assert arm_busy["reconciling"] == {
        "stateBusy": True,
        "ownAriaBusy": "true",
        "ownLabel": "Updating Auto-BE…",
        "entryDisabled": True,
        "previewCalls": 0,
        "confirmCalls": 0,
    }
    assert arm_busy["invalidOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Automation outcome unknown — wait for server status before acting again."
        ),
    }
    assert arm_busy["validOutcome"] == {
        "okStyle": True,
        "text": "Auto-BE disabled: BTC (long)",
    }
    assert arm_busy["after"] == {
        "armEnabled": True,
        "positionMgmtKnown": True,
        "killswitchDisabled": False,
        "entryDisabled": False,
        "cancelDisabled": False,
        "closeDisabled": False,
        "stopDisabled": False,
    }
    print("PASS invalid 2xx automation response cannot claim an updated rule")
    print("PASS automation arming is mutually exclusive with every money action")

    close_busy = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalConfirm = window.confirm;
          const originalAccount = Trader.state.account;
          let releaseRequest;
          let requestCalls = 0;
          let previewCalls = 0;
          let confirmCalls = 0;
          const probeAccount = {
            equity_usdt: 12500, available_usdt: 12000,
            positions: [{
              symbol: 'BTC', side: 'long', hold_vol: 0.01,
              entry_price: 64000, leverage: 2, liquidate_price: 50000,
              unrealized_pnl: 0, contract_size: 1
            }]
          };
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/account') {
              return Promise.resolve(new Response(JSON.stringify(probeAccount), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/preview') previewCalls += 1;
            if (url.pathname === '/api/orders/confirm') confirmCalls += 1;
            if (url.pathname !== '/api/orders/close') {
              return originalFetch(input, opts);
            }
            requestCalls += 1;
            if (requestCalls > 1) {
              return Promise.resolve(new Response(JSON.stringify({
                ok: true,
                status: 'closed',
                closed_vol: 0.0025,
                hold_vol: 0.01,
                residual_vol: 0.0075,
                verified: true,
                response: {success: true},
                warnings: []
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return new Promise((resolve) => { releaseRequest = resolve; });
          };
          window.confirm = () => true;
          try {
            Trader.state.account = probeAccount;
            Trader.renderPositions(probeAccount);
            document.querySelector('.cp-close-btn').click();
            await Promise.resolve();
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            const originalToken = Trader.state.previewToken;
            Trader.state.previewToken = 'position-action-busy-probe';
            await Trader.runConfirm();
            Trader.state.previewToken = originalToken;
            const during = {
              closeBusy: document.querySelector('.cp-close').getAttribute('aria-busy'),
              closeLabel: document.querySelector('.cp-close-label').textContent.trim(),
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => item.disabled),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              armDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                .every((item) => item.disabled),
              killswitchDisabled: document.getElementById('btn-killswitch').disabled,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              previewCalls,
              confirmCalls
            };
            document.querySelector('.cp-close-btn').click();
            await Promise.resolve();
            releaseRequest(new Response(JSON.stringify({
              ok: true, status: 'closed'
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            for (let i = 0; i < 100 &&
                 (Trader.state.closeBusy || Trader.state.mutationOutcomeUnknown.close);
                 i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            const invalidOutcome = {
              errorStyle: toast.classList.contains('err'),
              text: toast.textContent.trim()
            };
            for (let i = 0; i < 100 &&
                 !document.querySelector('.cp-close-btn'); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.querySelector('.cp-close-btn').click();
            for (let i = 0; i < 100 &&
                 (requestCalls < 2 || Trader.state.closeBusy); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const validOutcome = {
              okStyle: toast.classList.contains('ok'),
              text: toast.textContent.trim()
            };
            return {
              calls: requestCalls,
              during,
              invalidOutcome,
              validOutcome,
              after: {
                closeBusy: document.querySelector('.cp-close').getAttribute('aria-busy'),
                closeLabel: document.querySelector('.cp-close-label').textContent.trim(),
                closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                  .some((item) => item.disabled),
                stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
                killswitchDisabled: document.getElementById('btn-killswitch').disabled,
                entryDisabled: document.getElementById('btn-send-order').disabled
              }
            };
          } finally {
            window.fetch = originalFetch;
            window.confirm = originalConfirm;
            Trader.state.account = originalAccount;
            Trader.renderPositions(originalAccount || {positions: []});
            const toast = document.getElementById('toast');
            if (toast) toast.classList.add('hidden');
          }
        }"""
    )
    assert close_busy["calls"] == 2
    assert close_busy["during"] == {
        "closeBusy": "true",
        "closeLabel": "Closing…",
        "closeDisabled": True,
        "stopDisabled": True,
        "armDisabled": True,
        "killswitchDisabled": True,
        "entryDisabled": True,
        "previewCalls": 0,
        "confirmCalls": 0,
    }
    assert close_busy["invalidOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Close outcome unknown — check the live position and orders on the "
            "exchange before acting again. Do not retry blindly."
        ),
    }
    assert close_busy["validOutcome"] == {
        "okStyle": True,
        "text": "Closed: 0.0025 of 0.01",
    }, close_busy["validOutcome"]
    assert close_busy["after"] == {
        "closeBusy": "false",
        "closeLabel": "Close",
        "closeDisabled": False,
        "stopDisabled": False,
        "killswitchDisabled": False,
        "entryDisabled": False,
    }
    print("PASS invalid 2xx close response cannot claim a verified close")
    print("PASS close request disables every queued position mutation")

    sl_unverified = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalConfirm = window.confirm;
          const originalAccount = Trader.state.account;
          let modifyCalls = 0;
          let previewCalls = 0;
          let releaseModify;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/account') {
              return Promise.resolve(new Response(JSON.stringify(Trader.state.account), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/preview') previewCalls += 1;
            if (url.pathname !== '/api/orders/modify-sl') {
              return originalFetch(input, opts);
            }
            modifyCalls += 1;
            if (modifyCalls === 2) {
              return Promise.resolve(new Response(JSON.stringify({
                ok: true
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (modifyCalls === 3) {
              return Promise.resolve(new Response(JSON.stringify({
                ok: true, verified: true
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (modifyCalls > 3) {
              return Promise.resolve(new Response(JSON.stringify({
                ok: true,
                verified: true,
                status: 'modify_sl_ok',
                new_sl: 63200,
                new_oid: 8103,
                cancelled_old: [8101],
                failed_cancel: [],
                warnings: []
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return new Promise((resolve) => {
              releaseModify = () => resolve(new Response(JSON.stringify({
                ok: true,
                verified: false,
                status: 'modify_sl_unverified_old_kept',
                new_sl: 63000,
                warnings: ['New SL was not verified; the old SL was kept.']
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          };
          window.confirm = () => true;
          try {
            const probeAccount = {
              equity_usdt: 12500,
              available_usdt: 12000,
              positions: [{
                symbol: 'BTC', side: 'long', hold_vol: 0.01,
                entry_price: 64000, leverage: 2, liquidate_price: 50000,
                unrealized_pnl: 0, contract_size: 1
              }]
            };
            Trader.state.account = probeAccount;
            Trader.renderPositions(probeAccount);
            document.querySelector('.cp-sl-edit-btn').click();
            const input = document.querySelector('.cp-sl-input');
            input.value = '63000';
            document.querySelector('.cp-sl-set-btn').click();
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            const busyState = {
              ariaBusy: document.querySelector('.cp-actions').getAttribute('aria-busy'),
              label: document.querySelector('.cp-actions-label').textContent.trim(),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => item.disabled),
              killswitchDisabled: document.getElementById('btn-killswitch').disabled,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              previewCalls
            };
            document.querySelector('.cp-close-btn').click();
            releaseModify();
            for (let i = 0; i < 100 && Trader.state.slBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const toast = document.getElementById('toast');
            const unverifiedOutcome = {
              text: toast ? toast.textContent : '',
              errorStyle: !!toast && toast.classList.contains('err')
            };
            document.querySelector('.cp-sl-edit-btn').click();
            document.querySelector('.cp-sl-input').value = '63100';
            document.querySelector('.cp-sl-set-btn').click();
            for (let i = 0; i < 100 &&
                 (modifyCalls < 2 || Trader.state.slBusy ||
                  Trader.state.mutationOutcomeUnknown.sl); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const malformedOutcome = {
              text: toast ? toast.textContent.trim() : '',
              errorStyle: !!toast && toast.classList.contains('err')
            };
            document.querySelector('.cp-sl-edit-btn').click();
            document.querySelector('.cp-sl-input').value = '63150';
            document.querySelector('.cp-sl-set-btn').click();
            for (let i = 0; i < 100 &&
                 (modifyCalls < 3 || Trader.state.slBusy ||
                  Trader.state.mutationOutcomeUnknown.sl); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const incompleteVerifiedOutcome = {
              text: toast ? toast.textContent.trim() : '',
              errorStyle: !!toast && toast.classList.contains('err')
            };
            document.querySelector('.cp-sl-edit-btn').click();
            document.querySelector('.cp-sl-input').value = '63200';
            document.querySelector('.cp-sl-set-btn').click();
            for (let i = 0; i < 100 &&
                 (modifyCalls < 4 || Trader.state.slBusy); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const validOutcome = {
              text: toast ? toast.textContent.trim() : '',
              okStyle: !!toast && toast.classList.contains('ok')
            };
            return {
              calls: modifyCalls,
              unverifiedOutcome,
              malformedOutcome,
              incompleteVerifiedOutcome,
              validOutcome,
              busyState,
              after: {
                ariaBusy: document.querySelector('.cp-actions').getAttribute('aria-busy'),
                label: document.querySelector('.cp-actions-label').textContent.trim(),
                stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
                closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                  .some((item) => item.disabled),
                killswitchDisabled: document.getElementById('btn-killswitch').disabled,
                entryDisabled: document.getElementById('btn-send-order').disabled
              }
            };
          } finally {
            window.fetch = originalFetch;
            window.confirm = originalConfirm;
            Trader.state.account = originalAccount;
            Trader.renderPositions(originalAccount || {positions: []});
          }
        }"""
    )
    assert sl_unverified["calls"] == 4, "SL result probes did not reach the API"
    assert sl_unverified["busyState"] == {
        "ariaBusy": "true",
        "label": "Updating stop…",
        "stopDisabled": True,
        "closeDisabled": True,
        "killswitchDisabled": True,
        "entryDisabled": True,
        "previewCalls": 0,
    }
    assert sl_unverified["after"] == {
        "ariaBusy": "false",
        "label": "Stop",
        "stopDisabled": False,
        "closeDisabled": False,
        "killswitchDisabled": False,
        "entryDisabled": False,
    }
    assert sl_unverified["unverifiedOutcome"]["errorStyle"], (
        "Unverified SL result was not styled as an error"
    )
    assert "not verified" in sl_unverified["unverifiedOutcome"]["text"].lower(), (
        "Unverified SL result was not stated explicitly"
    )
    assert not sl_unverified["unverifiedOutcome"]["text"].startswith("SL set:"), (
        "Unverified SL result was falsely announced as successfully set"
    )
    assert sl_unverified["malformedOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Stop change outcome unknown — check open stops on the exchange "
            "before acting again. Do not retry blindly."
        ),
    }
    assert sl_unverified["incompleteVerifiedOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Stop change outcome unknown — check open stops on the exchange "
            "before acting again. Do not retry blindly."
        ),
    }
    assert sl_unverified["validOutcome"] == {
        "okStyle": True,
        "text": "SL set: 63,200",
    }
    print("PASS unverified SL replacement is not announced as moved")
    print("PASS invalid 2xx SL response cannot claim a verified stop change")

    cancel_busy = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalAccount = Trader.state.account;
          const originalToken = Trader.state.previewToken;
          const probeAccount = {
            equity_usdt: 12500, available_usdt: 12000,
            positions: [{
              symbol: 'BTC', side: 'long', hold_vol: 0.01,
              entry_price: 64000, leverage: 2, liquidate_price: 50000,
              unrealized_pnl: 0, contract_size: 1
            }]
          };
          let releaseCancel;
          let releasePreview;
          let holdPreview = false;
          let cancelCalls = 0;
          let previewCalls = 0;
          let confirmCalls = 0;
          let closeCalls = 0;
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/orders/open') {
              return Promise.resolve(new Response(JSON.stringify({
                orders: [
                  {orderId: 701, symbol: 'BTC', side: 'buy', vol: 0.01, price: 63000},
                  {orderId: 702, symbol: 'BTC', side: 'buy', vol: 0.01, price: 62000}
                ],
                stop_orders: []
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            if (url.pathname === '/api/account') {
              return Promise.resolve(new Response(JSON.stringify(probeAccount), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/preview') {
              previewCalls += 1;
              if (holdPreview) {
                return new Promise((resolve) => { releasePreview = resolve; });
              }
            }
            if (url.pathname === '/api/orders/confirm') confirmCalls += 1;
            if (url.pathname === '/api/orders/close') closeCalls += 1;
            if (url.pathname !== '/api/orders/cancel') {
              return originalFetch(input, opts);
            }
            cancelCalls += 1;
            if (cancelCalls > 1) {
              return Promise.resolve(new Response(JSON.stringify({
                ok: true, response: {success: true}
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return new Promise((resolve) => { releaseCancel = resolve; });
          };
          try {
            Trader.state.account = probeAccount;
            Trader.renderPositions(probeAccount);
            document.getElementById('btn-orders-refresh').click();
            for (let i = 0; i < 100 &&
                 document.querySelectorAll('.btn-cancel-order').length !== 2; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.querySelector('.btn-cancel-order').click();
            await Promise.resolve();
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            Trader.state.previewToken = 'cancel-busy-probe';
            await Trader.runConfirm();
            Trader.state.previewToken = originalToken;
            document.querySelector('.cp-close-btn').click();
            document.querySelectorAll('.btn-cancel-order')[1].click();
            await Promise.resolve();
            const active = document.querySelector('.btn-cancel-order[data-oid="701"]');
            const during = {
              ownAriaBusy: active.getAttribute('aria-busy'),
              ownLabel: active.textContent.trim(),
              allCancelDisabled: Array.from(document.querySelectorAll('.btn-cancel-order'))
                .every((item) => item.disabled),
              entryDisabled: document.getElementById('btn-send-order').disabled,
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => item.disabled),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              armDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                .every((item) => item.disabled),
              killswitchDisabled: document.getElementById('btn-killswitch').disabled,
              previewCalls,
              confirmCalls,
              closeCalls
            };
            releaseCancel(new Response(JSON.stringify({ok: true}), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 &&
                 (Object.keys(Trader.state._cancelBusy).length ||
                  Trader.state.mutationOutcomeUnknown.cancels['701']); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const cancelToast = document.getElementById('toast');
            const invalidOutcome = {
              errorStyle: cancelToast.classList.contains('err'),
              text: cancelToast.textContent.trim()
            };
            for (let i = 0; i < 100 &&
                 document.querySelectorAll('.btn-cancel-order').length !== 2; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            document.querySelectorAll('.btn-cancel-order')[1].click();
            for (let i = 0; i < 100 &&
                 (cancelCalls < 2 || Object.keys(Trader.state._cancelBusy).length); i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const validOutcome = {
              okStyle: cancelToast.classList.contains('ok'),
              text: cancelToast.textContent.trim()
            };
            const sizeInput = document.getElementById('ticket-usdt');
            const originalSize = sizeInput.value;
            sizeInput.value = '100';
            holdPreview = true;
            document.getElementById('order-form').dispatchEvent(new Event('submit', {
              bubbles: true, cancelable: true
            }));
            for (let i = 0; i < 100 && previewCalls < 1; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            const previewDuring = {
              allCancelDisabled: Array.from(document.querySelectorAll('.btn-cancel-order'))
                .every((item) => item.disabled),
              closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                .every((item) => item.disabled),
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              armDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
                .every((item) => item.disabled),
              killswitchDisabled: document.getElementById('btn-killswitch').disabled
            };
            document.querySelectorAll('.btn-cancel-order')[1].click();
            await Promise.resolve();
            releasePreview(new Response(JSON.stringify({detail: 'synthetic preview stop'}), {
              status: 400, headers: {'Content-Type': 'application/json'}
            }));
            for (let i = 0; i < 100 && Trader.state.orderBusy; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
            sizeInput.value = originalSize;
            return {
              cancelCalls,
              during,
              invalidOutcome,
              validOutcome,
              previewDuring,
              previewCalls,
              after: {
                allCancelEnabled: Array.from(document.querySelectorAll('.btn-cancel-order'))
                  .every((item) => !item.disabled),
                entryDisabled: document.getElementById('btn-send-order').disabled,
                closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                  .some((item) => item.disabled),
                stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
                killswitchDisabled: document.getElementById('btn-killswitch').disabled
              }
            };
          } finally {
            window.fetch = originalFetch;
            Trader.state.previewToken = originalToken;
            Trader.state.account = originalAccount;
            Trader.renderPositions(originalAccount || {positions: []});
            document.getElementById('btn-orders-refresh').click();
            const toast = document.getElementById('toast');
            if (toast) toast.classList.add('hidden');
          }
        }"""
    )
    assert cancel_busy["cancelCalls"] == 2
    assert cancel_busy["during"] == {
        "ownAriaBusy": "true",
        "ownLabel": "Cancelling…",
        "allCancelDisabled": True,
        "entryDisabled": True,
        "closeDisabled": True,
        "stopDisabled": True,
        "armDisabled": True,
        "killswitchDisabled": True,
        "previewCalls": 0,
        "confirmCalls": 0,
        "closeCalls": 0,
    }
    assert cancel_busy["invalidOutcome"] == {
        "errorStyle": True,
        "text": (
            "⚠ Cancel outcome unknown — check open orders on the exchange before "
            "acting again. Do not retry blindly."
        ),
    }
    assert cancel_busy["validOutcome"] == {
        "okStyle": True,
        "text": "Order cancelled",
    }
    assert cancel_busy["previewDuring"] == {
        "allCancelDisabled": True,
        "closeDisabled": True,
        "stopDisabled": True,
        "armDisabled": True,
        "killswitchDisabled": True,
    }
    assert cancel_busy["previewCalls"] == 1
    assert cancel_busy["cancelCalls"] == 2
    assert cancel_busy["after"] == {
        "allCancelEnabled": True,
        "entryDisabled": False,
        "closeDisabled": False,
        "stopDisabled": False,
        "killswitchDisabled": False,
    }
    print("PASS invalid 2xx cancel response cannot claim a cancellation")
    print("PASS cancel request is mutually exclusive with every money action")

    await page.evaluate(
        """() => {
          const probeAccount = {
            equity_usdt: 12500, available_usdt: 12000,
            positions: [{
              symbol: 'BTC', side: 'long', hold_vol: 0.01,
              entry_price: 64000, leverage: 2, liquidate_price: 50000,
              unrealized_pnl: 0, contract_size: 1
            }]
          };
          const probeOrders = {
            orders: [
              {orderId: 901, symbol: 'BTC', side: 'buy', vol: 0.01, price: 63000},
              {orderId: 902, symbol: 'BTC', side: 'buy', vol: 0.01, price: 62000}
            ],
            stop_orders: []
          };
          const originalFetch = window.fetch.bind(window);
          const originalConfirm = window.confirm;
          window.__mutationHttpProbe = {
            phase: '', accountResolvers: [], orderResolvers: [],
            closeCalls: 0, stopCalls: 0, cancelCalls: 0,
            probeAccount, probeOrders, originalFetch, originalConfirm,
            restore() {
              window.fetch = originalFetch;
              window.confirm = originalConfirm;
              delete window.__mutationHttpProbe;
            }
          };
          window.fetch = (input, opts = {}) => {
            const probe = window.__mutationHttpProbe;
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname === '/api/account') {
              if (probe.phase === 'close' || probe.phase === 'stop') {
                return new Promise((resolve) => probe.accountResolvers.push(resolve));
              }
              return Promise.resolve(new Response(JSON.stringify(probeAccount), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/open') {
              if (probe.phase) {
                return new Promise((resolve) => probe.orderResolvers.push(resolve));
              }
              return Promise.resolve(new Response(JSON.stringify(probeOrders), {
                status: 200, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/close') {
              probe.closeCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({detail: 'gateway'}), {
                status: 502, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/modify-sl') {
              probe.stopCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({detail: 'unavailable'}), {
                status: 503, headers: {'Content-Type': 'application/json'}
              }));
            }
            if (url.pathname === '/api/orders/cancel') {
              probe.cancelCalls += 1;
              return Promise.resolve(new Response(JSON.stringify({detail: 'timeout'}), {
                status: 408, headers: {'Content-Type': 'application/json'}
              }));
            }
            return originalFetch(input, opts);
          };
          window.confirm = () => true;
          Trader.state.account = probeAccount;
          Trader.renderPositions(probeAccount);
        }"""
    )
    try:
        await page.evaluate(
            """() => {
              window.__mutationHttpProbe.phase = 'close';
              document.querySelector('.cp-close-btn').click();
            }"""
        )
        await page.wait_for_function(
            """() => {
              const p = window.__mutationHttpProbe;
              return p.closeCalls === 1 && !Trader.state.closeBusy &&
                !!Trader.state.mutationOutcomeUnknown.close &&
                p.accountResolvers.length > 0 && p.orderResolvers.length > 0;
            }"""
        )
        close_http_unknown = await page.evaluate(
            """() => {
              document.querySelector('.cp-close-btn').click();
              const toast = document.getElementById('toast');
              return {
                calls: window.__mutationHttpProbe.closeCalls,
                closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
                  .every((item) => item.disabled),
                stopEnabled: !document.querySelector('.cp-sl-edit-btn').disabled,
                killEnabled: !document.getElementById('btn-killswitch').disabled,
                entryDisabled: document.getElementById('btn-send-order').disabled,
                errorStyle: toast.classList.contains('err'),
                text: toast.textContent.trim()
              };
            }"""
        )
        assert close_http_unknown["calls"] == 1
        assert close_http_unknown["closeDisabled"]
        assert close_http_unknown["stopEnabled"]
        assert close_http_unknown["killEnabled"]
        assert close_http_unknown["entryDisabled"]
        assert close_http_unknown["errorStyle"]
        assert "close outcome unknown after server error" in (
            close_http_unknown["text"].lower()
        )

        await page.evaluate(
            """() => {
              const p = window.__mutationHttpProbe;
              p.phase = '';
              p.accountResolvers.splice(0).forEach((resolve) => resolve(
                new Response(JSON.stringify(p.probeAccount), {
                  status: 200, headers: {'Content-Type': 'application/json'}
                })
              ));
              p.orderResolvers.splice(0).forEach((resolve) => resolve(
                new Response(JSON.stringify(p.probeOrders), {
                  status: 200, headers: {'Content-Type': 'application/json'}
                })
              ));
            }"""
        )
        await page.wait_for_function(
            """() => !Trader.state.mutationOutcomeUnknown.close &&
              !document.querySelector('.cp-close-btn').disabled"""
        )

        await page.evaluate(
            """() => {
              const p = window.__mutationHttpProbe;
              p.phase = 'stop';
              document.querySelector('.cp-sl-edit-btn').click();
              document.querySelector('.cp-sl-input').value = '63000';
              document.querySelector('.cp-sl-set-btn').click();
            }"""
        )
        await page.wait_for_function(
            """() => {
              const p = window.__mutationHttpProbe;
              return p.stopCalls === 1 && !Trader.state.slBusy &&
                !!Trader.state.mutationOutcomeUnknown.sl &&
                p.accountResolvers.length > 0 && p.orderResolvers.length > 0;
            }"""
        )
        stop_http_unknown = await page.evaluate(
            """() => {
              const toast = document.getElementById('toast');
              return {
                stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
                closeEnabled: !document.querySelector('.cp-close-btn').disabled,
                killEnabled: !document.getElementById('btn-killswitch').disabled,
                entryDisabled: document.getElementById('btn-send-order').disabled,
                errorStyle: toast.classList.contains('err'),
                text: toast.textContent.trim()
              };
            }"""
        )
        assert stop_http_unknown["stopDisabled"]
        assert stop_http_unknown["closeEnabled"]
        assert stop_http_unknown["killEnabled"]
        assert stop_http_unknown["entryDisabled"]
        assert stop_http_unknown["errorStyle"]
        assert "stop change outcome unknown after server error" in (
            stop_http_unknown["text"].lower()
        )

        await page.evaluate(
            """() => {
              const p = window.__mutationHttpProbe;
              p.phase = '';
              p.accountResolvers.splice(0).forEach((resolve) => resolve(
                new Response(JSON.stringify(p.probeAccount), {
                  status: 200, headers: {'Content-Type': 'application/json'}
                })
              ));
              p.orderResolvers.splice(0).forEach((resolve) => resolve(
                new Response(JSON.stringify(p.probeOrders), {
                  status: 200, headers: {'Content-Type': 'application/json'}
                })
              ));
            }"""
        )
        await page.wait_for_function(
            """() => !Trader.state.mutationOutcomeUnknown.sl &&
              !document.querySelector('.cp-sl-edit-btn').disabled"""
        )

        await page.evaluate("document.getElementById('btn-orders-refresh').click()")
        await page.wait_for_function(
            "document.querySelectorAll('.btn-cancel-order').length === 2"
        )
        await page.evaluate(
            """() => {
              window.__mutationHttpProbe.phase = 'cancel';
              document.querySelector('.btn-cancel-order[data-oid="901"]').click();
            }"""
        )
        await page.wait_for_function(
            """() => {
              const p = window.__mutationHttpProbe;
              return p.cancelCalls === 1 &&
                !!Trader.state.mutationOutcomeUnknown.cancels['901'] &&
                p.orderResolvers.length > 0 &&
                Object.keys(Trader.state._cancelBusy).length === 0;
            }"""
        )
        cancel_http_unknown = await page.evaluate(
            """() => {
              document.querySelector('.btn-cancel-order[data-oid="901"]').click();
              const toast = document.getElementById('toast');
              return {
                calls: window.__mutationHttpProbe.cancelCalls,
                targetDisabled: document.querySelector(
                  '.btn-cancel-order[data-oid="901"]'
                ).disabled,
                otherEnabled: !document.querySelector(
                  '.btn-cancel-order[data-oid="902"]'
                ).disabled,
                closeEnabled: !document.querySelector('.cp-close-btn').disabled,
                stopEnabled: !document.querySelector('.cp-sl-edit-btn').disabled,
                killEnabled: !document.getElementById('btn-killswitch').disabled,
                entryDisabled: document.getElementById('btn-send-order').disabled,
                errorStyle: toast.classList.contains('err'),
                text: toast.textContent.trim()
              };
            }"""
        )
        assert cancel_http_unknown["calls"] == 1
        assert cancel_http_unknown["targetDisabled"]
        assert cancel_http_unknown["otherEnabled"]
        assert cancel_http_unknown["closeEnabled"]
        assert cancel_http_unknown["stopEnabled"]
        assert cancel_http_unknown["killEnabled"]
        assert cancel_http_unknown["entryDisabled"]
        assert cancel_http_unknown["errorStyle"]
        assert "cancel outcome unknown after server error" in (
            cancel_http_unknown["text"].lower()
        )

        await page.evaluate(
            """() => {
              const p = window.__mutationHttpProbe;
              p.phase = '';
              p.orderResolvers.splice(0).forEach((resolve) => resolve(
                new Response(JSON.stringify(p.probeOrders), {
                  status: 200, headers: {'Content-Type': 'application/json'}
                })
              ));
            }"""
        )
        await page.wait_for_function(
            """() => !Trader.state.mutationOutcomeUnknown.cancels['901'] &&
              !document.querySelector('.btn-cancel-order[data-oid="901"]').disabled"""
        )
    finally:
        await page.evaluate(
            """() => {
              if (window.__mutationHttpProbe) window.__mutationHttpProbe.restore();
              Trader.state.mutationOutcomeUnknown.close = null;
              Trader.state.mutationOutcomeUnknown.sl = null;
              Object.keys(Trader.state.mutationOutcomeUnknown.cancels).forEach(
                (key) => delete Trader.state.mutationOutcomeUnknown.cancels[key]
              );
            }"""
        )
        await page.evaluate("document.getElementById('btn-orders-refresh').click()")
    print("PASS HTTP 408/5xx mutation outcomes stay blocked until fresh reconciliation")
    print("PASS unknown mutation latches keep independent protective actions available")

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
                JSON.stringify({supported: true, fills: [], error: null}),
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

    malformed_fills = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const confirmedFills = [{
            symbol: 'BTC', px: 60050, sz: 0.15, side: 'buy',
            time: 1710000005500, dir: 'Open Long'
          }];
          Trader.state.fills = confirmedFills;
          Trader.state._fillsStaleWarned = false;
          const toast = document.getElementById('toast');
          if (toast) toast.classList.add('hidden');
          window.__malformedFills = {calls: 0};
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/fills') return originalFetch(input, opts);
            window.__malformedFills.calls += 1;
            return Promise.resolve(new Response(JSON.stringify({
              supported: true,
              fills: {unexpected: []},
              error: null
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          };
          try {
            document.querySelector('[data-tab="positions"]').click();
            document.querySelector('[data-tab="trades"]').click();
            while (window.__malformedFills.calls < 1) {
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
            delete window.__malformedFills;
          }
        }"""
    )
    assert malformed_fills["preserved"], (
        "Malformed fills response replaced the last confirmed trade history"
    )
    assert malformed_fills["visibleWarning"], (
        "Malformed fills response was not visibly marked stale"
    )
    print("PASS malformed fills response preserves and marks trade history stale")

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

    r_sample_labels = await page.evaluate(
        """() => {
          const stats = {
            totals: {proposals: 25},
            overall: {
              sample: 25, win_rate: 0.6, win_rate_ci95: [0.4, 0.75],
              avg_realized_rrr: -1, realized_r_sample: 1,
              avg_realized_rrr_net: -0.8, net_sample: 1,
              low_sample: false
            },
            by_confidence: {
              high: {
                sample: 25, win_rate: 0.6, win_rate_ci95: [0.4, 0.75],
                avg_realized_rrr: 2, realized_r_sample: 1,
                low_sample: false
              }
            },
            by_action: {}, by_provider: {}, by_setup: {}, by_regime: {}, caveats: []
          };
          Trader.renderJournal(stats, []);
          const journalMetric = Array.from(
            document.querySelectorAll('#journal-body .journal-stat')
          ).find((node) => node.textContent.includes('realized R'));
          const journalGroup = document.querySelector(
            '#journal-body .journal-breakdown tbody td:last-child'
          );
          Trader.renderCalibration(stats);
          const calibrationMetric = Array.from(
            document.querySelectorAll('#calibration-body .journal-stat')
          ).find((node) => node.textContent.includes('realized R'));
          const calibrationGroup = document.querySelector(
            '#calibration-body .journal-breakdown tbody td:last-child'
          );
          return {
            journalMetric: journalMetric && journalMetric.textContent,
            journalGroup: journalGroup && journalGroup.textContent,
            calibrationMetric: calibrationMetric && calibrationMetric.textContent,
            calibrationGroup: calibrationGroup && calibrationGroup.textContent
          };
        }"""
    )
    assert "n=1" in (r_sample_labels["journalMetric"] or "")
    assert "n=1" in (r_sample_labels["journalGroup"] or "")
    assert (r_sample_labels["calibrationMetric"] or "").count("n=1") == 2
    assert "n=1" in (r_sample_labels["calibrationGroup"] or "")
    print("PASS realized-R averages show their own sample counts")

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

    protection_coverage = await page.evaluate(
        """async () => {
          const originalFetch = window.fetch.bind(window);
          const originalOrders = Trader.state.openOrders;
          const originalSymbol = Trader.state.symbol;
          const probeAccount = {
            equity_usdt: 12500,
            available_usdt: 12000,
            positions: [{
              symbol: 'BTC', side: 'long', hold_vol: 2,
              entry_price: 100, leverage: 2, liquidate_price: 50,
              unrealized_pnl: 0, contract_size: 1
            }]
          };
          window.fetch = (input, opts = {}) => {
            const raw = input && input.url ? input.url : input;
            const url = new URL(String(raw), location.href);
            if (url.pathname !== '/api/account') return originalFetch(input, opts);
            return Promise.resolve(new Response(JSON.stringify(probeAccount), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
          };
          try {
            Trader.state.symbol = 'BTC';
            Trader.state.openOrders = {
              orders: [],
              stop_orders: [{
                orderId: 8, symbol: 'BTC', stopLossPrice: 95, reduceOnly: true
              }],
              stops_error: null,
              error: null
            };
            Trader.state._positionsFp = null;
            await Trader.loadAccount();
            const unknownBanner = document.querySelector('.cp-sl-status');
            const unknownRail = document.querySelector('.ir-shield');
            const unknown = {
              bannerText: unknownBanner ? unknownBanner.textContent.trim() : '',
              bannerUnknown: !!unknownBanner && unknownBanner.classList.contains('cp-sl-unknown'),
              bannerGreen: !!unknownBanner && unknownBanner.classList.contains('cp-sl-ok'),
              railText: unknownRail ? unknownRail.textContent.trim() : '',
              railWarn: !!unknownRail && unknownRail.classList.contains('warn'),
              railGreen: !!unknownRail && unknownRail.classList.contains('ok')
            };

            Trader.state.openOrders = {
              orders: [],
              stop_orders: [{
                orderId: 9, symbol: 'BTC', stopLossPrice: 95,
                reduceOnly: true, vol: 1
              }],
              stops_error: null,
              error: null
            };
            Trader.state._positionsFp = null;
            await Trader.loadAccount();
            const partialBanner = document.querySelector('.cp-sl-status');
            const partialRail = document.querySelector('.ir-shield');
            return {
              unknown,
              partial: {
                bannerText: partialBanner ? partialBanner.textContent.trim() : '',
                railText: partialRail ? partialRail.textContent.trim() : '',
                railDanger: !!partialRail && partialRail.classList.contains('danger'),
                railGreen: !!partialRail && partialRail.classList.contains('ok')
              }
            };
          } finally {
            window.fetch = originalFetch;
            Trader.state.symbol = originalSymbol;
            Trader.state.openOrders = originalOrders;
            Trader.state._positionsFp = null;
            await Trader.loadAccount();
          }
        }"""
    )
    unknown_coverage = protection_coverage["unknown"]
    assert "SL COVERAGE UNKNOWN" in unknown_coverage["bannerText"], protection_coverage
    assert unknown_coverage["bannerUnknown"], protection_coverage
    assert not unknown_coverage["bannerGreen"], protection_coverage
    assert "SL COVERAGE UNKNOWN" in unknown_coverage["railText"], protection_coverage
    assert unknown_coverage["railWarn"], protection_coverage
    assert not unknown_coverage["railGreen"], protection_coverage

    partial_coverage = protection_coverage["partial"]
    assert "PARTIAL SL COVERAGE" in partial_coverage["bannerText"], protection_coverage
    assert "remainder unprotected" in partial_coverage["bannerText"], protection_coverage
    assert "PARTIAL SL" in partial_coverage["railText"], protection_coverage
    assert partial_coverage["railDanger"], protection_coverage
    assert not partial_coverage["railGreen"], protection_coverage
    print("PASS protection coverage unknown and partial states stay explicit")

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

    for selector in ("#btn-toggle-zones", "#btn-toggle-ai-lines"):
        toggle = page.locator(selector)
        await expect(toggle).to_have_attribute("aria-pressed", "true")
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-pressed", "false")
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-pressed", "true")

    for selected, unselected in [
        ('.side-btn[data-side="long"]', '.side-btn[data-side="short"]'),
        ('.type-btn[data-type="market"]', '.type-btn[data-type="limit"]'),
        (
            '.size-mode-btn[data-size-mode="position"]',
            '.size-mode-btn[data-size-mode="margin"]',
        ),
        ('.sltp-mode-btn[data-mode="price"]', '.sltp-mode-btn[data-mode="pct"]'),
        (
            '.trigger-mode-btn[data-trigger="auto"]',
            '.trigger-mode-btn[data-trigger="manual"]',
        ),
    ]:
        await expect(page.locator(selected)).to_have_attribute("aria-pressed", "true")
        await expect(page.locator(unselected)).to_have_attribute(
            "aria-pressed", "false"
        )

    await page.locator('.side-btn[data-side="short"]').click()
    await expect(page.locator('.side-btn[data-side="short"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    await expect(page.locator('.side-btn[data-side="long"]')).to_have_attribute(
        "aria-pressed", "false"
    )
    await page.locator('.side-btn[data-side="long"]').click()
    await page.locator('.size-mode-btn[data-size-mode="margin"]').click()
    await expect(
        page.locator('.size-mode-btn[data-size-mode="margin"]')
    ).to_have_attribute("aria-pressed", "true")
    await expect(
        page.locator('.size-mode-btn[data-size-mode="position"]')
    ).to_have_attribute("aria-pressed", "false")
    await page.locator('.size-mode-btn[data-size-mode="position"]').click()

    await page.locator("#btn-ki-keys").click()
    await expect(page.locator("#ki-provider")).to_be_focused()
    await expect(page.locator("#ki-model")).to_have_value("claude-sonnet-5")
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
    await expect(page.locator('[data-trigger="manual"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    await expect(page.locator('[data-trigger="auto"]')).to_have_attribute(
        "aria-pressed", "false"
    )
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
    await page.evaluate(
        """snapshot => {
          Trader.state.account = snapshot;
          Trader.renderPositions(snapshot);
        }""",
        account,
    )
    await page.locator("#btn-confirm-live").click()
    await expect(page.locator("#confirm-modal")).to_have_attribute("aria-busy", "true")
    await expect(page.locator("#btn-confirm-cancel")).to_be_disabled()
    order_busy_controls = await page.evaluate(
        """() => ({
          closeDisabled: Array.from(document.querySelectorAll('.cp-close-btn'))
            .every((item) => item.disabled),
          stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
          armDisabled: Array.from(document.querySelectorAll('.cp-arm-btn'))
            .every((item) => item.disabled),
          killswitchDisabled: document.getElementById('btn-killswitch').disabled
        })"""
    )
    assert order_busy_controls == {
        "closeDisabled": True,
        "stopDisabled": True,
        "armDisabled": True,
        "killswitchDisabled": True,
    }
    await page.keyboard.press("Escape")
    await page.locator("#confirm-modal .modal-backdrop").dispatch_event("click")
    await expect(page.locator("#confirm-modal")).to_be_visible()
    assert await page.evaluate("Trader.state._ttlTimer === null")
    await expect(page.locator("#confirm-modal")).to_be_hidden()
    assert sum(path == "/api/orders/confirm" for _, path in calls) == 1
    assert await page.evaluate(
        """() => Array.from(document.querySelectorAll(
          '.cp-close-btn, .cp-sl-edit-btn, #btn-killswitch'
        )).every((item) => !item.disabled)"""
    )
    assert await page.evaluate(
        """() => Array.from(document.querySelectorAll('.cp-arm-btn')).every(
          (item) => item.disabled && item.textContent.includes('Unknown')
        )"""
    )
    await expect(page.locator('[data-trigger="auto"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    await expect(page.locator('[data-trigger="manual"]')).to_have_attribute(
        "aria-pressed", "false"
    )
    assert await page.evaluate(
        "Trader.state.tradeMarkers.BTC.sl === 61000 && Trader.state.tradeMarkers.BTC.tp === 68000 && Trader.state.tradeMarkers.BTC.manual"
    )
    assert await page.locator("main").evaluate("el => !el.inert")
    assert await page.locator(".cp-reeval-btn").evaluate("button => !button.disabled")
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "true"
    )
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "title",
        "External position review requires INCLUDE_ACCOUNT_IN_LLM=true; alternatively select local Ollama",
    )
    llm_posts = sum(
        method == "POST" and path == "/api/llm" for method, path in calls
    )
    assert llm["get_started"].is_set()
    await page.locator("#llm-select").select_option("ollama")
    await expect(page.locator("#llm-select")).to_be_disabled()
    await expect(page.locator("#llm-select")).to_have_attribute("aria-busy", "true")
    await page.evaluate(
        """() => {
          const select = document.getElementById('llm-select');
          select.value = 'claude';
          select.dispatchEvent(new Event('change', {bubbles: true}));
        }"""
    )
    await expect(page.locator("#llm-select")).to_be_enabled()
    await expect(page.locator("#llm-select")).to_have_attribute("aria-busy", "false")
    await expect(page.locator("#llm-select")).to_have_value("ollama")
    llm["release_get"].set()
    await asyncio.sleep(0.1)
    await expect(page.locator("#llm-select")).to_have_value("ollama")
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "false"
    )
    assert await page.evaluate(
        "Trader.state.health.position_reevaluation_allowed === true"
    )
    await page.locator("#llm-select").select_option("claude")
    await expect(page.locator("#llm-select")).to_be_disabled()
    await expect(page.locator("#llm-select")).to_be_enabled()
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "true"
    )
    assert await page.evaluate(
        "Trader.state.health.position_reevaluation_allowed === false"
    )
    assert (
        sum(method == "POST" and path == "/api/llm" for method, path in calls)
        == llm_posts + 2
    )
    print(
        "PASS AI provider switch is singleflight, rejects stale startup status, "
        "and immediately refreshes position-review policy"
    )
    malformed_posts = sum(
        method == "POST" and path == "/api/llm" for method, path in calls
    )
    llm["omit_capability"] = True
    await page.locator("#llm-select").select_option("ollama")
    await expect(page.locator("#llm-select")).to_be_enabled()
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "true"
    )
    assert await page.evaluate(
        "Trader.state.health.position_reevaluation_allowed === false"
    )
    assert (
        sum(method == "POST" and path == "/api/llm" for method, path in calls)
        == malformed_posts + 1
    )
    llm["omit_capability"] = False
    await page.locator("#llm-select").select_option("claude")
    await expect(page.locator("#llm-select")).to_be_enabled()
    llm["response_provider"] = "unknown-provider"
    await page.locator("#llm-select").select_option("ollama")
    await expect(page.locator("#llm-select")).to_be_enabled()
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "true"
    )
    await expect(page.locator("#toast")).to_contain_text(
        "Could not switch AI provider: invalid server response"
    )
    llm["response_provider"] = None
    await page.locator("#llm-select").select_option("claude")
    await expect(page.locator("#llm-select")).to_be_enabled()
    llm["invalid_provider_row"] = True
    await page.locator("#llm-select").select_option("ollama")
    await expect(page.locator("#llm-select")).to_be_enabled()
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "true"
    )
    await expect(page.locator("#toast")).to_contain_text(
        "Could not switch AI provider: invalid server response"
    )
    llm["invalid_provider_row"] = False
    await page.locator("#llm-select").select_option("claude")
    await expect(page.locator("#llm-select")).to_be_enabled()
    print(
        "PASS malformed rows or unknown provider status fail position review closed"
    )
    await page.evaluate(
        """async () => {
          const button = document.querySelector('.cp-reeval-btn');
          Trader.state.reevalBusy[button.dataset.reevalKey] = true;
          await Trader.loadHealth();
        }"""
    )
    await expect(page.locator(".cp-reeval-btn")).to_be_disabled()
    await page.evaluate(
        """async () => {
          const button = document.querySelector('.cp-reeval-btn');
          Trader.state.reevalBusy[button.dataset.reevalKey] = false;
          await Trader.loadHealth();
        }"""
    )
    assert await page.locator(".cp-reeval-btn").evaluate("button => !button.disabled")
    review_calls = sum(path == "/api/reevaluate" for _, path in calls)
    await page.locator(".cp-reeval-btn").focus()
    await expect(page.locator(".cp-reeval-btn")).to_be_focused()
    await page.keyboard.press("Enter")
    await expect(page.locator("#toast")).to_contain_text(
        "Position review requires INCLUDE_ACCOUNT_IN_LLM=true"
    )
    assert sum(path == "/api/reevaluate" for _, path in calls) == review_calls
    health["position_reevaluation_allowed"] = True
    await page.evaluate("() => Trader.loadHealth()")
    await expect(page.locator(".cp-reeval-btn")).to_be_enabled()
    await expect(page.locator(".cp-reeval-btn")).to_have_attribute(
        "aria-disabled", "false"
    )
    injection_action = 'HOLD" id="reeval-action-injection'

    async def inject_reevaluation_action(route):
        await route.fulfill(
            json={
                "reevaluation": {
                    "action": injection_action,
                    "confidence": "low",
                    "reason": "Synthetic browser-boundary probe",
                }
            }
        )

    await page.route("**/api/reevaluate", inject_reevaluation_action)
    try:
        await page.locator(".cp-reeval-btn").click()
        await expect(page.locator(".cp-reeval-action")).to_have_class(
            "cp-reeval-action reeval-action-unknown"
        )
        assert await page.locator("#reeval-action-injection").count() == 0
    finally:
        await page.unroute("**/api/reevaluate", inject_reevaluation_action)
        await page.evaluate(
            """() => {
              Object.keys(Trader.state.reevalResults).forEach(function (key) {
                delete Trader.state.reevalResults[key];
              });
              Trader.renderPositions(Trader.state.account);
            }"""
        )
    print("PASS untrusted position-review action cannot inject DOM attributes")

    selector_probe = await page.evaluate(
        """async (symbol) => {
          const originalWatchlist = Trader.state.watchlist.slice();
          const originalData = Trader.state.overviewData[symbol];
          const originalLast = Trader.state._miniLast;
          try {
            Trader.state.watchlist.push(symbol);
            Trader.state.overviewData[symbol] = {
              symbol: symbol, last_price: 1, change_pct: 0, candles: []
            };
            Trader.state._miniLast = Date.now();
            document.querySelector('.symbol-tab[data-view="overview"]').click();
            await new Promise((resolve) => requestAnimationFrame(resolve));
            let tile = Array.from(document.querySelectorAll('.mini-tile')).find(
              (node) => node.getAttribute('data-symbol') === symbol
            );
            const firstPrice = tile && tile.querySelector('.mini-price').textContent;
            Trader.state.overviewData[symbol] = {
              symbol: symbol, last_price: 2, change_pct: 0, candles: []
            };
            document.querySelector('.symbol-tab[data-view="overview"]').click();
            await new Promise((resolve) => requestAnimationFrame(resolve));
            tile = Array.from(document.querySelectorAll('.mini-tile')).find(
              (node) => node.getAttribute('data-symbol') === symbol
            );
            return {
              tileFound: Boolean(tile),
              firstPrice: firstPrice,
              secondPrice: tile && tile.querySelector('.mini-price').textContent,
            };
          } finally {
            Trader.state.watchlist.splice(
              0, Trader.state.watchlist.length, ...originalWatchlist
            );
            if (originalData === undefined) delete Trader.state.overviewData[symbol];
            else Trader.state.overviewData[symbol] = originalData;
            Trader.state._miniLast = originalLast;
            Trader.state._gridStructFp = '';
            const chartTab = document.querySelector('.symbol-tab[data-symbol]');
            if (chartTab) chartTab.click();
          }
        }""",
        'BROKEN"]#SELECTOR',
    )
    assert selector_probe["tileFound"]
    assert selector_probe["firstPrice"] != selector_probe["secondPrice"]
    print("PASS exchange symbols cannot break dynamic DOM lookup selectors")
    await page.locator("#btn-send-order").focus()
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

    account_seen = asyncio.Event()
    orders_seen = asyncio.Event()
    release_account = asyncio.Event()
    release_orders = asyncio.Event()
    persisted_orders = {
        "orders": [
            {"orderId": 901, "symbol": "BTC", "side": "buy", "vol": 0.01},
            {"orderId": 902, "symbol": "BTC", "side": "buy", "vol": 0.01},
        ],
        "stop_orders": [],
    }

    async def hold_reload_account(route):
        account_seen.set()
        await release_account.wait()
        await route.fulfill(json=account)

    async def hold_reload_orders(route):
        orders_seen.set()
        await release_orders.wait()
        await route.fulfill(json=persisted_orders)

    await page.route("**/api/account", hold_reload_account)
    await page.route("**/api/orders/open", hold_reload_orders)
    try:
        await page.evaluate(
            """() => localStorage.setItem(
              'obsidian_unknown_trade_outcomes',
              JSON.stringify({
                version: 1,
                confirm: 'reload-confirm',
                close: 'reload-close',
                sl: 'reload-sl',
                cancels: {'901': 'reload-cancel'}
              })
            )"""
        )
        await page.reload()
        await asyncio.wait_for(account_seen.wait(), timeout=5)
        await asyncio.wait_for(orders_seen.wait(), timeout=5)
        persisted_before_reads = await page.evaluate(
            """() => ({
              confirm: Trader.state.confirmOutcomeUnknown,
              close: Trader.state.mutationOutcomeUnknown.close.id,
              stop: Trader.state.mutationOutcomeUnknown.sl.id,
              cancel: Trader.state.mutationOutcomeUnknown.cancels['901'].id,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              stillStored: localStorage.getItem(
                'obsidian_unknown_trade_outcomes'
              ) !== null
            })"""
        )
        assert persisted_before_reads == {
            "confirm": True,
            "close": "reload-close",
            "stop": "reload-sl",
            "cancel": "reload-cancel",
            "entryDisabled": True,
            "stillStored": True,
        }

        release_account.set()
        await page.wait_for_function(
            """() => Trader.state.account &&
              document.querySelector('.cp-close-btn') &&
              Trader.state.mutationOutcomeUnknown.close.accountReady &&
              Trader.state.mutationOutcomeUnknown.sl.accountReady"""
        )
        account_only_controls = await page.evaluate(
            """() => ({
              closeDisabled: document.querySelector('.cp-close-btn').disabled,
              stopDisabled: document.querySelector('.cp-sl-edit-btn').disabled,
              killEnabled: !document.getElementById('btn-killswitch').disabled,
              entryDisabled: document.getElementById('btn-send-order').disabled,
              confirmLatched: Trader.state.confirmOutcomeUnknown
            })"""
        )
        assert account_only_controls == {
            "closeDisabled": True,
            "stopDisabled": True,
            "killEnabled": True,
            "entryDisabled": True,
            "confirmLatched": True,
        }

        release_orders.set()
        await page.wait_for_function(
            """() => !Trader.state.confirmOutcomeUnknown &&
              !Trader.state.mutationOutcomeUnknown.close &&
              !Trader.state.mutationOutcomeUnknown.sl &&
              !Trader.state.mutationOutcomeUnknown.cancels['901'] &&
              localStorage.getItem('obsidian_unknown_trade_outcomes') === null"""
        )
    finally:
        release_account.set()
        release_orders.set()
        await page.unroute("**/api/account", hold_reload_account)
        await page.unroute("**/api/orders/open", hold_reload_orders)
        await page.evaluate(
            "localStorage.removeItem('obsidian_unknown_trade_outcomes')"
        )
    print("PASS unknown trade outcomes survive reload until fresh reconciliation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    asyncio.run(run(parser.parse_args().capture_only))
