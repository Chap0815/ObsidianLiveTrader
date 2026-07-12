"""F-20 (strict Content-Security-Policy).

The dashboard must carry a CSP with script-src 'self' and NO 'unsafe-inline'
in script-src, plus the style/font/connect allowances the page needs. The
setup wizard keeps one legitimate inline <script>, delivered via a per-response
nonce so script-src can stay 'self' + nonce (still no unsafe-inline).
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.security import build_csp


def _directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part == name or part.startswith(name + " "):
            return part
    return ""


@pytest.fixture
def dashboard_app(monkeypatch):
    monkeypatch.setenv("EXCHANGE", "mexc")
    get_settings.cache_clear()
    import app.main as main

    monkeypatch.setattr(main, "_setup_needed", lambda: False)
    try:
        yield main.app
    finally:
        get_settings.cache_clear()


def test_dashboard_has_strict_csp(dashboard_app):
    with TestClient(dashboard_app) as tc:
        r = tc.get("/")
    assert r.status_code == 200, r.text
    csp = r.headers.get("content-security-policy")
    assert csp, "CSP header missing on dashboard"
    # script-src must be exactly 'self' with NO unsafe-inline (the key control).
    assert _directive(csp, "script-src") == "script-src 'self'"
    assert "'unsafe-inline'" not in _directive(csp, "script-src")
    # style/font/connect allowances the page actually needs.
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in _directive(csp, "font-src")
    assert "connect-src 'self'" in csp
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_setup_page_csp_uses_nonce_no_unsafe_inline(monkeypatch):
    """The setup wizard's inline <script> is nonce'd; script-src never gains
    'unsafe-inline'."""
    get_settings.cache_clear()
    import app.main as main

    monkeypatch.setattr(main, "_setup_needed", lambda: True)
    with TestClient(main.app) as tc:
        r = tc.get("/setup")
    assert r.status_code == 200, r.text
    csp = r.headers.get("content-security-policy")
    assert csp, "CSP header missing on setup page"
    script_src = _directive(csp, "script-src")
    assert script_src.startswith("script-src 'self' 'nonce-")
    assert "'unsafe-inline'" not in script_src
    # The nonce in the header must match the nonce attribute in the rendered page.
    import re

    m = re.search(r"'nonce-([^']+)'", script_src)
    assert m, script_src
    nonce = m.group(1)
    assert f'nonce="{nonce}"' in r.text
    get_settings.cache_clear()


def test_build_csp_helper_shape():
    plain = build_csp()
    assert "script-src 'self'" in plain
    assert "'nonce-" not in plain
    nonced = build_csp(script_nonce="abc123")
    assert "script-src 'self' 'nonce-abc123'" in nonced
    assert "'unsafe-inline'" not in _directive(nonced, "script-src")
