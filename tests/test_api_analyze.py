"""POST /api/analyze response shape — provider/model exposure (Task 37/U2-03).

Mirrors the /api/analyze test pattern in test_analyze_cache.py: mocks the
market snapshot builder and the LLM call. Without this, a silent fallback
(configured LLM_PROVIDER has no key -> auto-resolved to a different provider,
see Settings.resolved_llm_provider) is invisible to the trader — only a
startup log line hints at it. The response must expose the RESOLVED
provider/model actually used, plus a fallback flag that is True only when
that resolution silently deviated from the .env-configured default (a
deliberate hot-swap via the KI dropdown is NOT a fallback).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.config import get_settings
from app.models import TradeProposal


def _mock_snap(symbol="BTC_USDT"):
    return {
        "symbol": symbol,
        "last_price": 100_000.0,
        "funding": {},
        "contract": {"apiAllowed": True, "contractSize": 0.0001},
        "ltf": {"tf": "15m", "candles": [], "indicators": {}, "structure": {}},
        "htf": {"tf": "1H", "candles": [], "indicators": {}, "structure": {}},
    }


def _proposal(action="BUY") -> TradeProposal:
    return TradeProposal(
        htf_trend="bullish",
        ltf_trend="bullish",
        action=action,
        entry_price=100_000.0 if action != "STAY_OUT" else None,
        stop_loss=99_000.0 if action != "STAY_OUT" else None,
        tp1=102_000.0 if action != "STAY_OUT" else None,
        rrr=2.0 if action != "STAY_OUT" else None,
        recommended_leverage="5x",
        rationale="test proposal",
        position_sizing_note="high conviction, full risk budget",
    )


def _patched(analyze_mock, symbol="BTC_USDT"):
    return (
        patch("app.main.build_market_snapshot", new=AsyncMock(return_value=MagicMock())),
        patch("app.main.snapshot_to_api_dict", return_value=_mock_snap(symbol)),
        patch("app.main.analyze_with_llm", new=analyze_mock),
        patch("app.main.build_llm_context", return_value={"symbol": symbol}),
    )


def test_analyze_response_provider_model_and_fallback_flag(monkeypatch):
    """Task 37/U2-03: the analyze response must expose the RESOLVED
    provider/model actually used (not silently omitted), and the
    `provider_fallback` flag must be True ONLY when the resolution silently
    deviated from the .env-configured provider — never for the normal
    configured-and-ready case, and never for a deliberate hot-swap via the
    KI dropdown (llm_override)."""
    from fastapi.testclient import TestClient

    from app.main import app

    analyze_mock = AsyncMock(return_value=_proposal())
    client = MagicMock()
    client.account_snapshot = AsyncMock(
        return_value={"equity_usdt": 1000.0, "available_usdt": 900.0, "positions": []}
    )

    # --- Case 1: configured provider (claude) IS ready -> no fallback ------
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("XAI_API_KEY", "test-xai")
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        tc.app.state.llm_override = None
        p1, p2, p3, p4 = _patched(analyze_mock)
        try:
            with p1, p2, p3, p4:
                r1 = tc.post(
                    "/api/analyze", json={"symbol": "BTC_USDT", "tf": "15m", "htf": "1H"}
                )
            assert r1.status_code == 200, r1.text
            body1 = r1.json()
            assert body1["provider"] == "claude"
            assert body1["model"]  # non-empty resolved model string
            assert body1["provider_fallback"] is False

            # --- Case 2: hot-swap to xai via the dropdown -> resolved
            # provider changes, but this is DELIBERATE, not a fallback.
            tc.app.state.llm_override = "xai"
            with p1, p2, p3, p4:
                r2 = tc.post(
                    "/api/analyze", json={"symbol": "ETH_USDT", "tf": "15m", "htf": "1H"}
                )
            assert r2.status_code == 200, r2.text
            body2 = r2.json()
            assert body2["provider"] == "xai"
            assert body2["provider_fallback"] is False
        finally:
            tc.app.state.llm_override = None

    get_settings.cache_clear()

    # --- Case 3: LLM_PROVIDER=xai configured but NO xai key -> silently
    # auto-resolves to the ready claude key (Settings.resolved_llm_provider)
    # AND must be flagged as a fallback (this was previously invisible).
    # NOTE: an explicit empty string (not delenv) is required — a real repo
    # .env file also sets XAI_API_KEY, and pydantic-settings falls back to
    # that dotenv value once the OS env var is merely absent.
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        tc.app.state.llm_override = None
        p1, p2, p3, p4 = _patched(analyze_mock)
        try:
            with p1, p2, p3, p4:
                r3 = tc.post(
                    "/api/analyze", json={"symbol": "SOL_USDT", "tf": "15m", "htf": "1H"}
                )
            assert r3.status_code == 200, r3.text
            body3 = r3.json()
            assert body3["provider"] == "claude"  # resolved, NOT the configured "xai"
            assert body3["provider_fallback"] is True
        finally:
            tc.app.state.llm_override = None

    get_settings.cache_clear()

    # --- Case 4: LLM_PROVIDER set to an ALIAS ("grok") with a valid xai key.
    # resolved_llm_provider maps grok->xai, so the resolved provider is "xai"
    # and NO fallback happened — the badge must NOT cry "fallback" just because
    # the raw .env string ("grok") differs from the canonical resolved name.
    monkeypatch.setenv("LLM_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "test-xai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    get_settings.cache_clear()

    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.analyze_cache = {}
        tc.app.state.llm_override = None
        p1, p2, p3, p4 = _patched(analyze_mock)
        try:
            with p1, p2, p3, p4:
                r4 = tc.post(
                    "/api/analyze", json={"symbol": "XRP_USDT", "tf": "15m", "htf": "1H"}
                )
            assert r4.status_code == 200, r4.text
            body4 = r4.json()
            assert body4["provider"] == "xai"
            assert body4["provider_fallback"] is False  # alias != fallback
        finally:
            tc.app.state.llm_override = None

    get_settings.cache_clear()
