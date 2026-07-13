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
    assert ctx["funding_rate"] == 0.0001


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

    async def fake_anthropic_text(system, user, model, settings, timeout=120.0):
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
    assert "score >= 6" in p  # absolute bar raised from 5 -> 6


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
