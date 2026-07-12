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
