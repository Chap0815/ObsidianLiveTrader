"""Static guard for the SEALED front-end store (store.js `Object.seal(state)`).

Root cause of the 2026-07-24 "charts don't load" outage: `updateChartPriceFormat`
wrote `state._chartPricePrecision`, a property that was never declared in the
sealed `store.js` state object. `Object.seal` makes such a write throw
`TypeError: Cannot add property ..., object is not extensible` AT RUNTIME — which
`node --check` (syntax only) and the pytest backend suite never see, and which the
browser-smoke missed because the `?v=r22` cache-buster was never bumped (stale JS).

This test closes that whole class: EVERY `state.<name> = ...` write across the
front-end JS must have a matching top-level key declared in store.js. If a future
change adds a state write without declaring the key, this fails in CI instead of
silently breaking the chart in production.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _declared_state_keys() -> set[str]:
    """Top-level keys declared in `var state = { ... }` in store.js."""
    src = (_STATIC / "store.js").read_text(encoding="utf-8")
    start = src.index("var state = {")
    end = src.index("Object.seal(state)")
    block = src[start:end]
    # keys are `  name:` at the start of an object-literal line (skip nested
    # object contents by only taking lines whose key sits at 2-space indent).
    keys = set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*:", block, re.MULTILINE))
    assert "symbol" in keys and "candleSeries" in keys, "store.js parse failed"
    return keys


def _written_state_keys() -> dict[str, list[str]]:
    """Every `state.<name> = ...` assignment across the front-end JS files.

    Returns {key: [file:line, ...]} so a failure points at the offending write.
    Excludes store.js itself (it defines the object) and compound reads/compares.
    """
    written: dict[str, list[str]] = {}
    pat = re.compile(r"\bstate\.([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")
    for js in sorted(_STATIC.glob("*.js")):
        if js.name == "store.js":
            continue
        for i, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            for m in pat.finditer(line):
                written.setdefault(m.group(1), []).append(f"{js.name}:{i}")
    return written


def test_every_state_write_is_declared_in_sealed_store():
    declared = _declared_state_keys()
    written = _written_state_keys()
    undeclared = {k: locs for k, locs in written.items() if k not in declared}
    assert not undeclared, (
        "front-end writes to state properties NOT declared in the sealed store.js "
        "(Object.seal makes these throw at runtime and break the page):\n"
        + "\n".join(f"  state.{k}  <-  {', '.join(locs)}" for k, locs in sorted(undeclared.items()))
    )


def test_chart_price_precision_is_declared():
    """Direct regression for the specific field that caused the outage."""
    assert "_chartPricePrecision" in _declared_state_keys()


def test_reevaluate_frontend_keys_requests_by_symbol_and_side():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert 'reevalResultHtml(p.symbol, sideVal)' in src
    assert 'actionEl.getAttribute("data-side")' in src
    assert 'side: sideKey' in src


def test_cached_reevaluation_is_scoped_to_current_position_snapshot():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "function _reevalPositionFingerprint(sym, side)" in src
    assert "p.position_id" in src
    assert "p.entry_price" in src
    assert "p.hold_vol" in src
    assert "entry.reviewFingerprint !== currentFingerprint" in src


def test_late_reevaluation_result_is_discarded_after_position_change():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "const reviewFingerprint = _reevalReviewFingerprint" in src
    assert "function cacheReevalResult(value)" in src
    assert "currentFingerprint !== reviewFingerprint" in src


def test_cached_reevaluation_is_scoped_to_timeframes():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "function _reevalReviewFingerprint(sym, side)" in src
    assert "String(state.tf" in src
    assert "String(state.htf" in src
    assert "entry.reviewFingerprint !== currentFingerprint" in src


def test_cached_reevaluation_is_scoped_to_selected_provider():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert 'const providerSelect = $("llm-select")' in src
    assert "providerSelect.value" in src
    assert "currentFingerprint !== reviewFingerprint" in src


def test_visible_ai_status_and_chart_copy_is_english():
    app_src = (_STATIC / "app.js").read_text(encoding="utf-8")
    store_src = (_STATIC / "store.js").read_text(encoding="utf-8")
    base_src = (_STATIC.parent / "templates" / "base.html").read_text(
        encoding="utf-8"
    )

    for untranslated in (
        '"KI Entry"',
        '"KI SL -1R"',
        '"KI TP1"',
        '"KI TP2"',
        '"KI TP3"',
        '"Bewerte…"',
        "KI bewertet Position…",
        '"Analysiere…"',
        '"KI") + " analysiert…"',
    ):
        assert untranslated not in app_src
    for untranslated_rail_label in (
        ">Unrealisiert<",
        ">Schutz<",
        ">Liq-Distanz<",
        "SL-Bereich",
        "TP-Bereich",
    ):
        assert untranslated_rail_label not in app_src
    for english_copy in (
        '"AI Entry"',
        '"AI SL -1R"',
        '"AI TP1"',
        '"AI TP2"',
        '"AI TP3"',
        '"Reviewing…"',
        "AI is reviewing the position…",
        '"Analyzing…"',
        '"AI") + " is analyzing…"',
        'providerId || "AI"',
    ):
        assert english_copy in app_src
    for english_rail_label in (
        ">Unrealized PnL<",
        ">Protection<",
        ">Liq. distance<",
        "SL zone",
        "TP zone",
    ):
        assert english_rail_label in app_src
    assert 'llmLabel: "AI"' in store_src
    assert "store.js?v={{ asset_version|default('r29') }}" in base_src
    assert "trade-math.js?v={{ asset_version|default('r36') }}" in base_src
    assert "app.js?v={{ asset_version|default('r44') }}" in base_src


def test_calibration_table_headers_are_english():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "<th>Win rate</th>" in src
    assert "<th>Avg gross R</th>" in src
    assert "<th>Win-Rate</th>" not in src
    assert "<th>Ø R brutto</th>" not in src


def test_trade_history_net_header_is_english():
    src = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "<th>Net</th>" in src
    assert "<th>Netto</th>" not in src
