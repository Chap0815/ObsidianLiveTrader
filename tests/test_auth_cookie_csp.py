"""F-19 (HttpOnly session cookie auth).

HTTP-level TDD via FastAPI TestClient. The dashboard must set an HttpOnly
session cookie carrying the local auth token (so the token leaves the DOM),
and private endpoints must accept that cookie as a credential while still
accepting the X-Local-Token header as a fallback.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture
def tokened_app(monkeypatch):
    """App with a configured LOCAL_API_TOKEN and a rendered (non-setup) dashboard."""
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token")
    monkeypatch.setenv("EXCHANGE", "mexc")
    get_settings.cache_clear()

    import app.main as main

    # .env exists in the repo, but make the setup gate deterministic for tests.
    monkeypatch.setattr(main, "_setup_needed", lambda: False)
    try:
        yield main.app
    finally:
        get_settings.cache_clear()


# ── F-19 ────────────────────────────────────────────────────────────────────


def test_dashboard_sets_httponly_session_cookie(tokened_app):
    with TestClient(tokened_app) as tc:
        r = tc.get("/")
    assert r.status_code == 200, r.text
    set_cookie = r.headers.get("set-cookie", "")
    low = set_cookie.lower()
    assert "local_auth=" in low, set_cookie
    assert "httponly" in low, set_cookie
    assert "samesite=strict" in low, set_cookie
    assert "path=/" in low, set_cookie
    assert "secure" not in low, set_cookie
    # The raw token must NOT be embedded in the HTML body any more.
    assert "LOCAL_API_TOKEN" not in r.text
    assert "test-token" not in r.text


def test_dashboard_marks_auth_cookie_secure_over_https(tokened_app):
    with TestClient(tokened_app, base_url="https://testserver") as tc:
        r = tc.get("/")

    assert r.status_code == 200, r.text
    assert "secure" in r.headers.get("set-cookie", "").lower()


def test_private_endpoint_accepts_cookie_only(tokened_app):
    with TestClient(tokened_app) as tc:
        # Establish the cookie via the dashboard, then call a private endpoint
        # with NO X-Local-Token header — the cookie alone must authenticate.
        tc.get("/")
        r = tc.get("/api/llm")
    assert r.status_code == 200, r.text


def test_private_endpoint_header_fallback_still_works(tokened_app):
    with TestClient(tokened_app) as tc:
        # Fresh client, no cookie jar entry — the header token must still work.
        r = tc.get("/api/llm", headers={"X-Local-Token": "test-token"})
    assert r.status_code == 200, r.text


def test_private_endpoint_rejects_missing_credential(tokened_app):
    with TestClient(tokened_app) as tc:
        r = tc.get("/api/llm")  # no cookie, no header
    assert r.status_code == 401, r.text
    # The cookie is an equally valid credential (see above), so the 401
    # detail must not read as if only the header is accepted.
    detail = r.json()["detail"]
    assert "x-local-token" in detail.lower()
    assert "cookie" in detail.lower()


def test_private_endpoint_rejects_wrong_cookie(tokened_app):
    with TestClient(tokened_app) as tc:
        tc.cookies.set("local_auth", "wrong-token")
        r = tc.get("/api/llm")
    assert r.status_code == 401, r.text


def test_dashboard_clears_stale_auth_cookie_after_token_removal(
    tokened_app, monkeypatch
):
    with TestClient(tokened_app) as tc:
        tc.get("/")
        monkeypatch.setenv("LOCAL_API_TOKEN", "")
        get_settings.cache_clear()

        response = tc.get("/")

    cookie = response.headers.get("set-cookie", "").lower()
    assert "local_auth=" in cookie
    assert "max-age=0" in cookie


def test_setup_redirect_clears_stale_auth_cookie(tokened_app, monkeypatch):
    import app.main as main

    with TestClient(tokened_app) as tc:
        tc.get("/")
        monkeypatch.setattr(main, "_setup_needed", lambda: True)

        response = tc.get("/", follow_redirects=False)

    assert response.status_code == 303
    cookie = response.headers.get("set-cookie", "").lower()
    assert "local_auth=" in cookie
    assert "max-age=0" in cookie
