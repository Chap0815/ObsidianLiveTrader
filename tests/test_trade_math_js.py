"""Behavioral parity checks for the browser-side protection classifier."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TRADE_MATH = ROOT / "app" / "static" / "trade-math.js"
APP_JS = ROOT / "app" / "static" / "app.js"
UTILS_JS = ROOT / "app" / "static" / "utils.js"


def _call(function_name: str, *args: object) -> object:
    script = """
const fs = require("fs");
const vm = require("vm");
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
const fn = globalThis[process.argv[2]];
const args = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(fn(...args)));
"""
    result = subprocess.run(
        ["node", "-e", script, str(TRADE_MATH), function_name, json.dumps(args)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _call_utils(function_name: str, *args: object) -> object:
    script = """
const fs = require("fs");
const vm = require("vm");
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
const fn = globalThis[process.argv[2]];
const args = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(fn(...args)));
"""
    result = subprocess.run(
        ["node", "-e", script, str(UTILS_JS), function_name, json.dumps(args)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    return json.loads(result.stdout)


def test_frontend_trading_status_distinguishes_testnet_and_real_funds():
    assert _call_utils(
        "tradingStatusLabel",
        {
            "trading_enabled": True,
            "exchange": "hyperliquid",
            "hl_testnet": True,
            "live_trading": False,
            "exchange_configured": True,
        },
    ) == "ARMED · TESTNET"
    for exchange in ("hyperliquid", "mexc"):
        assert _call_utils(
            "tradingStatusLabel",
            {
                "trading_enabled": True,
                "exchange": exchange,
                "hl_testnet": False if exchange == "hyperliquid" else None,
                "live_trading": True,
                "exchange_configured": True,
            },
        ) == "ARMED · REAL FUNDS"


def test_preview_ttl_accepts_only_server_contract_integers():
    for value in (1, 60, 3600):
        assert _call_utils("previewTtlSeconds", value) == value

    for value in (None, True, False, 0, -1, 1.5, 3601, "60", "<img src=x>", {}):
        assert _call_utils("previewTtlSeconds", value) is None


def test_fill_response_contract_rejects_malformed_or_contradictory_payloads():
    assert _call_utils(
        "isValidFillsResponse",
        {"supported": True, "fills": [], "error": None},
    ) is True
    assert _call_utils(
        "isValidFillsResponse",
        {"supported": False, "fills": [], "error": None},
    ) is True
    for invalid in (
        None,
        [],
        {"supported": "true", "fills": [], "error": None},
        {"supported": True, "fills": {}, "error": None},
        {"supported": True, "fills": [], "error": {}},
        {"supported": False, "fills": [{"side": "buy"}], "error": None},
    ):
        assert _call_utils("isValidFillsResponse", invalid) is False


def test_frontend_news_urls_require_public_credential_free_web_hosts():
    assert (
        _call_utils("safeExternalNewsUrl", "https://example.com/article")
        == "https://example.com/article"
    )
    for unsafe in (
        None,
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "http://127.0.0.1/private",
        "http://127.1/private",
        "http://0x7f.1/private",
        "http://[::1]/private",
        "http://localhost/private",
        "https://user:password@example.com/article",
        "https://example.com:8443/article",
        "https://single-label/article",
        "https://example.com/has whitespace",
        "https://example.com/" + "a" * 2_048,
    ):
        assert _call_utils("safeExternalNewsUrl", unsafe) == ""


def test_confirm_modal_uses_validated_ttl_for_token_and_countdown():
    app_source = APP_JS.read_text(encoding="utf-8")

    assert "const ttl = previewTtlSeconds(preview.expires_in_seconds);" in app_source
    assert "preview.ok === true && previewToken !== null && ttl !== null" in app_source
    assert "const canConfirm = okGates && ttl !== null && executionReady;" in app_source
    assert "let left = ttl;" in app_source
    assert "preview.expires_in_seconds || 60" not in app_source


def test_preview_response_contract_accepts_valid_pass_and_gate_rejection():
    summary = {"symbol": "BTC", "side": "long"}
    gate = {"errors": [], "warnings": []}
    assert _call_utils(
        "isValidPreviewResponse",
        {
            "ok": True,
            "token": "synthetic-token",
            "expires_in_seconds": 60,
            "summary": summary,
            "gate": gate,
            "errors": [],
            "warnings": [],
        },
    ) is True
    assert _call_utils(
        "isValidPreviewResponse",
        {
            "ok": False,
            "token": None,
            "summary": summary,
            "gate": {"errors": ["blocked"], "warnings": []},
            "errors": ["blocked"],
            "warnings": [],
        },
    ) is True


def test_preview_response_contract_rejects_malformed_success_payloads():
    base = {
        "ok": True,
        "token": "synthetic-token",
        "expires_in_seconds": 60,
        "summary": {"symbol": "BTC"},
        "gate": {"errors": [], "warnings": []},
        "errors": [],
        "warnings": [],
    }
    mutations = [
        {"ok": "true"},
        {"token": {}},
        {"expires_in_seconds": "60"},
        {"summary": []},
        {"gate": []},
        {"errors": "blocked"},
        {"warnings": ["safe", {"not": "text"}]},
    ]
    for mutation in mutations:
        assert _call_utils("isValidPreviewResponse", {**base, **mutation}) is False


def test_run_preview_opens_modal_for_valid_gate_rejection_only():
    app_source = APP_JS.read_text(encoding="utf-8")
    run_preview_source = app_source.split("async function runPreview()", 1)[1].split(
        "/** C3-03/T3-08", 1
    )[0]

    assert "if (!res.ok || data.ok === false)" not in run_preview_source
    assert "if (!isValidPreviewResponse(data))" in run_preview_source
    assert "openConfirmModal(data, returnFocus);" in run_preview_source


def test_frontend_trading_status_fails_visibly_on_ambiguous_network():
    assert _call_utils(
        "tradingStatusLabel",
        {
            "trading_enabled": True,
            "exchange": "hyperliquid",
            "hl_testnet": True,
            "live_trading": True,
            "exchange_configured": True,
        },
    ) == "ARMED · VERIFY NETWORK"
    assert _call_utils("tradingStatusLabel", None) == "DISARMED"


def test_frontend_trading_status_surfaces_unconfigured_exchange():
    assert _call_utils(
        "tradingStatusLabel",
        {
            "trading_enabled": True,
            "exchange": "hyperliquid",
            "hl_testnet": True,
            "live_trading": False,
            "exchange_configured": False,
        },
    ) == "ARMED · EXCHANGE NOT READY"


def test_frontend_exchange_network_label_is_explicit_and_fail_closed():
    assert _call_utils(
        "exchangeNetworkLabel", {"exchange": "hyperliquid", "hl_testnet": True}
    ) == "HYPERLIQUID · TESTNET"
    assert _call_utils(
        "exchangeNetworkLabel", {"exchange": "hyperliquid", "hl_testnet": False}
    ) == "HYPERLIQUID · MAINNET"
    assert _call_utils(
        "exchangeNetworkLabel", {"exchange": "mexc", "hl_testnet": None}
    ) == "MEXC · LIVE VENUE"
    assert _call_utils(
        "exchangeNetworkLabel", {"exchange": "hyperliquid", "hl_testnet": None}
    ) == "HYPERLIQUID · NETWORK UNKNOWN"
    assert _call_utils("exchangeNetworkLabel", None) == "—"


def test_frontend_position_review_requires_literal_server_capability():
    assert _call_utils(
        "canReviewPosition", {"position_reevaluation_allowed": True}
    ) is True
    for health in (
        {"position_reevaluation_allowed": False},
        {"position_reevaluation_allowed": "true"},
        {},
        [],
        None,
    ):
        assert _call_utils("canReviewPosition", health) is False


def _classify(order: dict[str, object]) -> dict[str, float | None]:
    result = _call("classifyTriggers", order, "long", 100, False)
    assert isinstance(result, dict)
    return result


def test_frontend_invalid_explicit_protection_does_not_fall_back():
    assert _classify(
        {"stopLossPrice": True, "orderType": "Stop", "triggerPrice": 95.0}
    ) == {"sl": None, "tp": None}


def test_frontend_combined_explicit_protection_returns_sl_and_tp():
    assert _classify(
        {"stopLossPrice": 95.0, "takeProfitPrice": 110.0}
    ) == {"sl": 95.0, "tp": 110.0}


def test_frontend_rejects_non_string_order_label():
    assert _classify({"orderType": ["Stop"], "triggerPrice": 95.0}) == {
        "sl": None,
        "tp": None,
    }


def test_frontend_protection_validates_all_order_label_aliases():
    assert _classify(
        {"orderType": "Stop", "tpsl": "tp", "triggerPrice": 95.0}
    ) == {"sl": None, "tp": None}
    assert _classify({"tpsl": "sl", "triggerPrice": 105.0}) == {
        "sl": 105.0,
        "tp": None,
    }
    assert _classify({"type": "tp", "triggerPrice": 95.0}) == {
        "sl": None,
        "tp": 95.0,
    }
    assert _classify({"type": 1, "triggerPrice": 95.0}) == {
        "sl": None,
        "tp": None,
    }


def test_frontend_uses_most_protective_sl_for_multiple_orders():
    assert _call("mostProtectiveSl", [118.0, 105.0], "long") == 118.0
    assert _call("mostProtectiveSl", [112.0, 130.0], "short") == 112.0
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "mostProtectiveSl(slCandidates" in app_source


def test_frontend_hyperliquid_symbol_match_rejects_cross_quote_pairs():
    assert _call("symbolsMatch", "BTC_USDT", "BTC_USDC", True) is False
    assert _call("symbolsMatch", "BTC", "BTC_USDC", True) is True
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "return symbolsMatch(na, nb, isHyperliquid);" in app_source


def test_exchange_identity_fallback_does_not_parse_visible_network_label():
    app_source = APP_JS.read_text(encoding="utf-8")

    assert "label.dataset.exchange" in app_source
    assert 'lbl.textContent.trim().toLowerCase()' not in app_source
    # Definition plus symMatch, isHlExchange, markerKey and the strict
    # position-management response identity validator.
    assert app_source.count("currentExchangeId()") == 5


def test_frontend_protection_filters_opposite_hedge_side():
    assert _classify({"positionType": 2, "stopLossPrice": 105.0}) == {
        "sl": None,
        "tp": None,
    }


def test_frontend_protection_requires_valid_reduce_only_aliases():
    rejected = (
        {"reduceOnly": False, "stopLossPrice": 95.0},
        {"reduce_only": "true", "stopLossPrice": 95.0},
        {"reduceOnly": True, "reduce_only": False, "stopLossPrice": 95.0},
    )
    for order in rejected:
        assert _classify(order) == {"sl": None, "tp": None}

    assert _classify({"reduceOnly": True, "stopLossPrice": 95.0}) == {
        "sl": 95.0,
        "tp": None,
    }


def test_trade_zone_uses_most_protective_sl():
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "mostProtectiveSl(zoneSlCandidates" in app_source


def test_chart_trigger_rejects_invalid_explicit_price_without_fallback():
    assert _call(
        "classifyChartTrigger",
        {"stopLossPrice": True, "orderType": "Stop", "triggerPrice": 95.0},
    ) == {"sl": None, "tp": None, "trigger": None}
    assert _call("classifyChartTrigger", {"triggerPrice": 95.0}) == {
        "sl": None,
        "tp": None,
        "trigger": 95.0,
    }
    app_source = APP_JS.read_text(encoding="utf-8")
    assert app_source.count("classifyChartTrigger(s)") == 3


def test_combined_trigger_row_keeps_sl_cancel_warning_and_both_prices():
    assert _call(
        "classifyChartTrigger",
        {"stopLossPrice": 95.0, "takeProfitPrice": 110.0},
    ) == {"sl": 95.0, "tp": 110.0, "trigger": None}
    app_source = APP_JS.read_text(encoding="utf-8")
    assert 'isSl && isTp ? "SL / TP ACTIVE"' in app_source
    assert "(isSl ? \"1\" : \"0\")" in app_source
    assert 'posKv("SL", fmt(c.sl, 4))' in app_source
    assert 'posKv("TP", fmt(c.tp, 4))' in app_source


def test_frontend_rejects_boolean_protection_volume():
    assert _call("positiveFiniteNumber", True) is None
    assert _call("positiveFiniteNumber", "2.5") == 2.5
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "const v = positiveFiniteNumber(" in app_source
    assert "const svolRaw = positiveFiniteNumber(" in app_source


def test_persisted_trade_marker_rejects_invalid_protection_prices():
    assert _call(
        "normalizeTradeMarker",
        {"sl": True, "tp": {"price": 110}, "manual": True},
    ) == {"sl": None, "tp": None, "manual": True}
    assert _call(
        "normalizeTradeMarker", {"sl": "95.5", "tp": "110", "manual": True}
    ) == {"sl": 95.5, "tp": 110, "manual": True}
    app_source = APP_JS.read_text(encoding="utf-8")
    assert app_source.count("normalizeTradeMarker(") == 6


def test_persisted_trade_marker_requires_literal_manual_boolean():
    assert _call("normalizeTradeMarker", {"sl": 95, "manual": "false"}) == {
        "sl": 95,
        "tp": None,
        "manual": False,
    }
    assert _call("normalizeTradeMarker", {"sl": 95, "manual": True}) == {
        "sl": 95,
        "tp": None,
        "manual": True,
    }


def test_persisted_trade_marker_keeps_valid_entry_time_for_zone_anchor():
    now_ms = 1_750_000_000_000
    assert (
        _call("tradeMarkerTime", {"entryMs": now_ms - 10_000}, "entryMs", now_ms)
        == now_ms - 10_000
    )
    app_source = APP_JS.read_text(encoding="utf-8")
    assert 'tradeMarkerTime(rawMk, "entryMs", Date.now())' in app_source
    assert "barOpenTimeSec(markerEntryMs" in app_source


def test_persisted_trade_marker_rejects_invalid_or_future_merge_time():
    now_ms = 1_750_000_000_000
    for invalid in (True, "Infinity", now_ms + 1, 2**53):
        assert _call("tradeMarkerTime", {"ts": invalid}, "ts", now_ms) is None
    assert _call("tradeMarkerTime", {"ts": str(now_ms)}, "ts", now_ms) == now_ms
    app_source = APP_JS.read_text(encoding="utf-8")
    assert 'tradeMarkerTime(incoming, "ts", nowMs)' in app_source
    assert 'tradeMarkerTime(a, "ts", nowMs)' in app_source
    assert 'tradeMarkerTime(mk, "ts", nowMs)' in app_source


def test_frontend_fill_direction_requires_exact_documented_label():
    assert _call("classifyFillDir", "Open Long") == "open"
    assert _call("classifyFillDir", "Close Short") == "close"
    assert _call("classifyFillDir", "Long > Short") == "liq"
    assert _call("classifyFillDir", "Liquidated Long") == "liq"
    for invalid in (None, ["Open Long"], "Reopen Long", "not close", "Liquidated Long later"):
        assert _call("classifyFillDir", invalid) is None
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "if (cls == null) return;" in app_source


def test_frontend_zone_anchor_uses_latest_proven_flat_open_epoch():
    now_ms = 1_750_000_000_000
    fills = [
        {"dir": "Open Long", "start_position": 0, "time": now_ms - 500_000},
        {"dir": "Open Long", "start_position": 2, "time": now_ms - 200_000},
        {"dir": "Open Short", "start_position": 0, "time": now_ms - 100_000},
        {"dir": "Open Long", "start_position": 0, "time": now_ms - 50_000},
        {"dir": "Open Long", "start_position": 0, "time": now_ms + 300_001},
    ]
    assert _call("currentPositionEntryFillTime", fills, "long", now_ms) == (
        now_ms - 50_000
    )
    assert _call("currentPositionEntryFillTime", fills, "short", now_ms) == (
        now_ms - 100_000
    )
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "currentPositionEntryFillTime(matchingFills, side, Date.now())" in app_source


def test_frontend_zone_anchor_rejects_coercible_non_numeric_flat_state():
    now_ms = 1_750_000_000_000
    for invalid in (False, "", "0", None, [0]):
        fills = [{"dir": "Open Long", "start_position": invalid, "time": now_ms}]
        assert _call("currentPositionEntryFillTime", fills, "long", now_ms) is None


def test_frontend_fill_side_does_not_default_unknown_to_sell():
    assert _call("normalizeFillSide", "buy") == "buy"
    assert _call("normalizeFillSide", "sell") == "sell"
    for invalid in (None, False, "", "SELL", " sell ", ["sell"]):
        assert _call("normalizeFillSide", invalid) is None
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "const side = normalizeFillSide(f.side);" in app_source
    assert "if (side == null) return;" in app_source


def test_frontend_fill_record_requires_exact_execution_side():
    now_ms = 1_750_000_000_000
    valid = {
        "symbol": "BTC",
        "side": "buy",
        "sz": 0.25,
        "px": 65_000,
        "time": now_ms,
        "dir": "Open Long",
        "fee": None,
        "closed_pnl": "",
    }
    assert _call("normalizeFillRecord", valid, now_ms) == {
        **valid,
        "fee": 0,
        "closed_pnl": 0,
    }
    for invalid_side in (None, False, "", "SELL", " sell ", ["sell"]):
        assert (
            _call(
                "normalizeFillRecord", {**valid, "side": invalid_side}, now_ms
            )
            is None
        )
    app_source = APP_JS.read_text(encoding="utf-8")
    assert "normalizeFillRecord(fill, fillsNow)" in app_source


def test_frontend_fill_record_rejects_untrusted_financial_geometry():
    now_ms = 1_750_000_000_000
    valid = {
        "side": "sell",
        "sz": 0.25,
        "px": 65_000,
        "time": now_ms,
        "fee": 0.1,
        "closed_pnl": -2.5,
    }
    invalid_fields = (
        ("sz", -0.25),
        ("sz", True),
        ("sz", "Infinity"),
        ("px", 0),
        ("px", False),
        ("px", "NaN"),
        ("time", now_ms + 300_001),
        ("time", 1.5),
        ("fee", True),
        ("fee", "Infinity"),
        ("closed_pnl", {}),
        ("closed_pnl", "NaN"),
    )
    for field, value in invalid_fields:
        assert _call("normalizeFillRecord", {**valid, field: value}, now_ms) is None


def test_frontend_fill_record_rejects_known_direction_side_contradiction():
    now_ms = 1_750_000_000_000
    valid = {
        "side": "buy",
        "sz": 0.25,
        "px": 65_000,
        "time": now_ms,
        "fee": 0.1,
        "closed_pnl": 0,
    }
    contradictions = (
        ("sell", "Open Long"),
        ("sell", "Close Short"),
        ("buy", "Open Short"),
        ("buy", "Close Long"),
        ("buy", "Liquidated Long"),
        ("sell", "Short > Long"),
    )
    for side, direction in contradictions:
        assert (
            _call(
                "normalizeFillRecord",
                {**valid, "side": side, "dir": direction},
                now_ms,
            )
            is None
        )
    assert (
        _call(
            "normalizeFillRecord",
            {**valid, "side": "buy", "dir": "Open Long"},
            now_ms,
        )["dir"]
        == "Open Long"
    )
