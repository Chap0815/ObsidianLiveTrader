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
