"""Market scanner: output parsing, context building, endpoint wiring."""

import pytest

from app.llm.scanner import ScanResult, build_scan_contexts, parse_scan_results
from app.models import Candle


def test_parse_scan_results_valid_sorted_capped():
    text = """```json
    {"results": [
      {"symbol": "ETH", "bias": "short", "score": 6, "setup": "pullback", "reason": "x"},
      {"symbol": "BTC", "bias": "long", "score": 8.5, "setup": "breakout", "reason": "y", "key_level": 100000},
      {"symbol": "A", "bias": "long", "score": 5}, {"symbol": "B", "bias": "long", "score": 5},
      {"symbol": "C", "bias": "long", "score": 5}, {"symbol": "D", "bias": "long", "score": 5},
      {"symbol": "E", "bias": "long", "score": 5}
    ]}
    ```"""
    out = parse_scan_results(text)
    assert len(out) == 6  # capped
    assert out[0].symbol == "BTC" and out[0].score == 8.5  # sorted desc
    assert out[1].symbol == "ETH"
    assert isinstance(out[0], ScanResult)


def test_parse_scan_results_drops_invalid_rows():
    text = '{"results": [{"symbol": "OK", "bias": "long", "score": 7}, {"symbol": "BAD", "bias": "sideways", "score": 9}, {"score": 11, "bias": "long", "symbol": "X"}]}'
    out = parse_scan_results(text)
    assert [r.symbol for r in out] == ["OK"]


def test_parse_scan_results_empty():
    assert parse_scan_results('{"results": []}') == []


@pytest.mark.asyncio
async def test_build_scan_contexts_survives_single_coin_failure():
    class FakeClient:
        async def klines(self, symbol, interval, limit_hint=120):
            if symbol == "BROKEN":
                raise RuntimeError("exchange down for this coin")
            base = 100.0 if symbol == "BTC" else 10.0
            return [
                Candle(
                    time=(1_700_000_000 + i * 900) * 1000,
                    open=base, high=base + 1, low=base - 1, close=base + 0.5,
                    vol=5,
                )
                for i in range(40)
            ]

    overview = [
        {"symbol": "BTC", "volume24": 1e9, "funding": 0.0001, "last": 100.5},
        {"symbol": "BROKEN", "volume24": 5e8, "funding": 0.0, "last": 1.0},
        {"symbol": "SOL", "volume24": 4e8, "funding": -0.0002, "last": 10.5},
    ]
    contexts, errors = await build_scan_contexts(FakeClient(), overview, "15m", "1H")
    assert [c["symbol"] for c in contexts] == ["BTC", "SOL"]
    assert len(errors) == 1 and "BROKEN" in errors[0]
    ctx = contexts[0]
    assert "recent_candles" not in ctx["ltf"]  # compact: no candle arrays
    assert "read" in ctx["ltf"] and "structure" in ctx["ltf"]
    assert "funding_rate" not in ctx  # L-08: raw rate dropped, extreme/annualized suffice


# --- F-22: results must be restricted to the actually-scanned symbol set ---
def test_parse_scan_results_filters_hallucinated_symbol():
    """A symbol the LLM invents (not among the coins it was actually given)
    must never reach the UI, even if it otherwise looks valid."""
    text = (
        '{"results": ['
        '{"symbol": "BTC", "bias": "long", "score": 8},'
        '{"symbol": "NOTREAL", "bias": "short", "score": 9}'
        "]}"
    )
    out = parse_scan_results(text, allowed_symbols={"BTC", "ETH"})
    assert [r.symbol for r in out] == ["BTC"]


def test_parse_scan_results_allowlist_is_case_insensitive():
    text = '{"results": [{"symbol": "btc", "bias": "long", "score": 8}]}'
    out = parse_scan_results(text, allowed_symbols={"BTC"})
    assert [r.symbol for r in out] == ["btc"]


def test_parse_scan_results_no_allowlist_keeps_legacy_behavior():
    """Backward-compat: omitting allowed_symbols does not filter anything."""
    text = '{"results": [{"symbol": "ANYTHING", "bias": "long", "score": 8}]}'
    out = parse_scan_results(text)
    assert [r.symbol for r in out] == ["ANYTHING"]


@pytest.mark.asyncio
async def test_scan_with_llm_restricts_to_context_symbols(monkeypatch):
    """End-to-end: scan_with_llm must pass the context's own symbol set as
    the allowlist so a hallucinated symbol never survives even if the LLM
    text-parsing path is exercised for real."""
    import app.llm.scanner as scanner_mod
    from app.config import Settings

    async def fake_anthropic_text(
        system, user, model, settings, timeout=120.0, *, provider_label="Claude"
    ):
        return (
            '{"results": ['
            '{"symbol": "BTC", "bias": "long", "score": 8},'
            '{"symbol": "MADE_UP", "bias": "short", "score": 9}'
            "]}"
        )

    monkeypatch.setattr(scanner_mod, "_anthropic_text", fake_anthropic_text)
    settings = Settings(anthropic_api_key="k")
    contexts = [{"symbol": "BTC"}, {"symbol": "ETH"}]
    results, model = await scanner_mod.scan_with_llm(contexts, settings)
    assert [r.symbol for r in results] == ["BTC"]


# --- B2: absolute score floor + analyzer non-negotiables in the payload ----


def test_parse_scan_results_absolute_min_score_floor():
    """min_score is a hard absolute gate: a score-5 coin is dropped when the
    floor is 6, so the deep analyzer never gets marginal carry-through."""
    text = (
        '{"results": ['
        '{"symbol": "A", "bias": "long", "score": 7},'
        '{"symbol": "B", "bias": "long", "score": 5}'
        "]}"
    )
    out = parse_scan_results(text, min_score=6.0)
    assert [r.symbol for r in out] == ["A"]


def test_parse_scan_results_default_has_no_floor():
    text = '{"results": [{"symbol": "B", "bias": "long", "score": 5}]}'
    assert [r.symbol for r in parse_scan_results(text)] == ["B"]


def test_scanner_min_score_is_five():
    """L-02: the floor is decoupled from the against-daily score cap (6) so
    the 5-6 band actually survives instead of being starved out."""
    from app.llm.scanner import SCANNER_MIN_SCORE

    assert SCANNER_MIN_SCORE == 5.0
    text = '{"results": [{"symbol": "MID", "bias": "long", "score": 5.5}]}'
    out = parse_scan_results(text, min_score=SCANNER_MIN_SCORE)
    assert [r.symbol for r in out] == ["MID"]


@pytest.mark.asyncio
async def test_scanner_context_omits_raw_funding_rate():
    """L-08: the raw funding rate is token ballast the prompt never reads —
    only funding_extreme/funding_annualized (the actual tiebreaker inputs)
    belong in the per-coin context sent to the LLM."""
    from app.analysis.context import clear_daily_cache

    clear_daily_cache()

    class FakeClient:
        async def klines(self, symbol, interval, limit_hint=120):
            base = 100.0
            return [
                Candle(
                    time=(1_700_000_000 + i * 900) * 1000,
                    open=base, high=base + 1, low=base - 1, close=base + 0.5, vol=5,
                )
                for i in range(60)
            ]

    overview = [{"symbol": "NOFUND", "volume24": 1e9, "funding": 0.0005, "last": 100.5}]
    contexts, errors = await build_scan_contexts(FakeClient(), overview, "15m", "1H")
    assert errors == []
    ctx = contexts[0]
    assert "funding_rate" not in ctx
    assert "funding_extreme" in ctx
    assert "funding_annualized" in ctx


@pytest.mark.asyncio
async def test_build_scan_contexts_adds_funding_extreme_and_daily_regime():
    from app.analysis.context import clear_daily_cache

    clear_daily_cache()

    class FakeClient:
        async def klines(self, symbol, interval, limit_hint=120):
            base = 100.0
            return [
                Candle(
                    time=(1_700_000_000 + i * 900) * 1000,
                    open=base, high=base + 1, low=base - 1, close=base + 0.5, vol=5,
                )
                for i in range(60)
            ]

    overview = [{"symbol": "SCANX", "volume24": 1e9, "funding": 0.0005, "last": 100.5}]
    contexts, errors = await build_scan_contexts(FakeClient(), overview, "15m", "1H")
    assert errors == []
    ctx = contexts[0]
    assert ctx["funding_extreme"] == "crowded_long"  # 0.0005 > 0.0001 threshold
    assert "funding_annualized" in ctx
    assert "daily_stack" in ctx  # 1D regime anchor now fed to the screener


def test_scanner_prompt_carries_analyzer_non_negotiables():
    from app.llm.scanner import SCANNER_SYSTEM_PROMPT

    p = SCANNER_SYSTEM_PROMPT
    assert "No-chase" in p or "over-stretch" in p
    assert "RRR" in p
    assert "daily_stack" in p
    assert "score >= 5" in p  # L-02: floor decoupled from the against-daily cap (6)


def test_scanner_prompt_treats_against_daily_as_cap_not_reject():
    """F1: the scanner must match the analyzer — against-daily is a score cap
    (the deep stage still trades it low), NOT a hard reject."""
    from app.llm.scanner import SCANNER_SYSTEM_PROMPT

    p = SCANNER_SYSTEM_PROMPT
    assert "Against the daily regime is NOT a hard reject" in p
    assert "CAP its score at 6" in p
    # the two true deep-stage vetoes remain the only hard rejects
    assert "ONLY hard rejects" in p


@pytest.mark.asyncio
async def test_build_scan_contexts_threads_oi_read_when_present():
    """I4: when the overview row carries OI, an oi_read positioning label is
    threaded into the screener payload; it is absent (graceful) when OI is
    null, as it always is on MEXC / HL cold-start."""
    from app.analysis.context import clear_daily_cache

    clear_daily_cache()

    class FakeClient:
        async def klines(self, symbol, interval, limit_hint=120):
            # rising closes so the ~1h price direction reads 'up'
            return [
                Candle(
                    time=(1_700_000_000 + i * 900) * 1000,
                    open=100.0 + i, high=101.0 + i, low=99.0 + i,
                    close=100.5 + i, vol=5,
                )
                for i in range(40)
            ]

    overview = [
        {"symbol": "WITHOI", "volume24": 1e9, "funding": 0.0, "last": 140.0,
         "open_interest": 5000.0, "oi_change_pct_1h": 4.0},
        {"symbol": "NOOI", "volume24": 9e8, "funding": 0.0, "last": 140.0},
    ]
    contexts, errors = await build_scan_contexts(FakeClient(), overview, "15m", "1H")
    assert errors == []
    by_sym = {c["symbol"]: c for c in contexts}
    assert by_sym["WITHOI"]["oi_read"] == "price_up_oi_up_real_trend"
    assert "oi_read" not in by_sym["NOOI"]  # graceful when OI absent


@pytest.mark.asyncio
async def test_build_scan_contexts_fetches_timeframes_concurrently():
    """O4: a coin's ltf/htf/daily fetches run concurrently (asyncio.gather),
    so for a single coin more than one klines call is in flight at once."""
    import asyncio

    from app.analysis.context import clear_daily_cache

    clear_daily_cache()

    class FakeClient:
        def __init__(self):
            self.inflight = 0
            self.max_inflight = 0

        async def klines(self, symbol, interval, limit_hint=120):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                await asyncio.sleep(0.02)  # hold the call open to observe overlap
                return [
                    Candle(
                        time=(1_700_000_000 + i * 900) * 1000,
                        open=100.0, high=101.0, low=99.0, close=100.5, vol=5,
                    )
                    for i in range(60)
                ]
            finally:
                self.inflight -= 1

    client = FakeClient()
    overview = [{"symbol": "ONE", "volume24": 1e9, "funding": 0.0, "last": 100.5}]
    contexts, errors = await build_scan_contexts(
        client, overview, "15m", "1H", concurrency=1
    )
    assert errors == []
    assert contexts[0]["symbol"] == "ONE"
    # Sequential would peak at 1 in-flight; gather overlaps the 3 fetches.
    assert client.max_inflight >= 2


@pytest.mark.asyncio
async def test_build_scan_contexts_isolates_error_with_gather():
    """O4: with the gathered fetches, a failing coin is still isolated (its
    error is captured, the healthy coins still produce contexts)."""
    from app.analysis.context import clear_daily_cache

    clear_daily_cache()

    class FakeClient:
        async def klines(self, symbol, interval, limit_hint=120):
            if symbol == "BROKEN":
                raise RuntimeError("exchange down for this coin")
            return [
                Candle(
                    time=(1_700_000_000 + i * 900) * 1000,
                    open=100.0, high=101.0, low=99.0, close=100.5, vol=5,
                )
                for i in range(60)
            ]

    overview = [
        {"symbol": "OKX", "volume24": 1e9, "funding": 0.0, "last": 100.5},
        {"symbol": "BROKEN", "volume24": 5e8, "funding": 0.0, "last": 1.0},
    ]
    contexts, errors = await build_scan_contexts(FakeClient(), overview, "15m", "1H")
    assert [c["symbol"] for c in contexts] == ["OKX"]
    assert len(errors) == 1 and "BROKEN" in errors[0]


# --- S2-04: scanner rationale must survive the analyzer handoff sanitizer --


def test_scanner_verdict_keeps_reason():
    """_sanitize_scanner_verdict must pass the screener's own rationale
    through (truncated ~120 chars) so the analyzer sees WHY the coin was
    flagged, not just bias/setup/score."""
    from app.llm.client import _sanitize_scanner_verdict

    long_reason = "bounced off daily support with bullish RSI divergence " * 5
    out = _sanitize_scanner_verdict(
        {"bias": "long", "setup": "pullback", "score": 7, "reason": long_reason}
    )
    assert out["reason"] == long_reason.strip()[:120]
    assert len(out["reason"]) <= 120

    out2 = _sanitize_scanner_verdict(
        {"bias": "short", "setup": "reversal", "score": 6, "reason": "RSI overbought at resistance"}
    )
    assert out2["reason"] == "RSI overbought at resistance"

    # absent/blank reason -> key simply omitted, no crash
    out3 = _sanitize_scanner_verdict({"bias": "long", "setup": "breakout", "score": 8})
    assert "reason" not in out3


# --- S2-06: /api/scan must carry a scanned_at timestamp for staleness UI ---


def test_scan_response_has_scanned_at(monkeypatch):
    import time as _time

    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    import app.llm.scanner as scanner_mod
    from app.analysis.context import clear_daily_cache
    from app.config import Settings, get_settings
    from app.main import app

    clear_daily_cache()
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(exchange="mexc", mexc_api_key="k", mexc_api_secret="s"),
    )
    get_settings.cache_clear()

    client = MagicMock()
    client.market_overview = AsyncMock(
        return_value=[{"symbol": "BTC", "volume24": 1e9, "funding": 0.0, "last": 100.0}]
    )
    client.klines = AsyncMock(
        return_value=[
            Candle(
                time=(1_700_000_000 + i * 900) * 1000,
                open=100.0, high=101.0, low=99.0, close=100.5, vol=5,
            )
            for i in range(60)
        ]
    )

    async def fake_scan_with_llm(contexts, settings):
        return [], "test-model"

    monkeypatch.setattr(scanner_mod, "scan_with_llm", fake_scan_with_llm)

    before = _time.time()
    with TestClient(app) as tc:
        tc.app.state.mexc = client
        tc.app.state.exchange = client
        r = tc.post("/api/scan", json={"tf": "15m", "htf": "1H"})
    after = _time.time()

    assert r.status_code == 200, r.text
    data = r.json()
    assert "scanned_at" in data
    assert isinstance(data["scanned_at"], (int, float))
    assert before <= data["scanned_at"] <= after


def test_parse_scan_results_salvages_truncated_json():
    """Model cut off at max_tokens mid-array: keep the complete objects."""
    from app.llm.scanner import parse_scan_results

    # Two complete entries, third truncated mid-string (like the real report)
    truncated = (
        '{ "results": [ '
        '{ "symbol": "AAVE", "bias": "long", "score": 7, "setup": "pullback", '
        '"reason": "bounced off support, MACD flipped", "key_level": 90.48 }, '
        '{ "symbol": "BTC", "bias": "short", "score": 6, "setup": "reversal", '
        '"reason": "RSI overbought at resistance", "key_level": 62000 }, '
        '{ "symbol": "SOL", "bias": "long", "score": 5, "setup": "breakout", '
        '"reason": "reclai'  # <-- cut off here, no closing brace
    )
    out = parse_scan_results(truncated)
    syms = [r.symbol for r in out]
    assert "AAVE" in syms and "BTC" in syms  # complete ones survive
    assert "SOL" not in syms  # truncated one dropped, no crash
    assert out[0].symbol == "AAVE"  # highest score first


# --- Task 15 (P2-03/S2-03): scanner Anthropic body must disable thinking ---


@pytest.mark.asyncio
async def test_scanner_anthropic_thinking_disabled(monkeypatch):
    """On Sonnet 5 (and the current Opus/Sonnet 4.6+ family), omitting the
    `thinking` field leaves adaptive thinking ON by default, which eats into
    the scanner's max_tokens budget and can truncate the JSON answer before
    it's written. The scanner call must explicitly disable it, and the
    max_tokens cap is raised 4096->6000 for headroom consistent with the
    OpenAI-compat scanner path."""
    import json as _json

    import app.llm.scanner as scanner_mod
    from app.config import Settings

    class _FakeResp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = _json.dumps(payload)

        def json(self):
            return self._payload

    class _CapturingClient:
        posted_bodies: list[dict] = []

        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            _CapturingClient.posted_bodies.append(json)
            return _FakeResp({"content": [{"type": "text", "text": '{"results": []}'}]})

    _CapturingClient.posted_bodies = []
    monkeypatch.setattr(scanner_mod.httpx, "AsyncClient", _CapturingClient)

    settings = Settings(anthropic_api_key="k")
    await scanner_mod._anthropic_text("sys", "user", "claude-sonnet-5", settings)

    body = _CapturingClient.posted_bodies[-1]
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 6000
