#!/usr/bin/env python3
"""Headless setup assistant (.env) — fallback for browser setup.

Teilt sich EINEN .env-Builder und dasselbe Fragen-Set mit dem Web-Setup
(app.env_builder). Keine zweite Wahrheit mehr.

  py -3 scripts/setup_wizard.py            # startet den Assistenten
  py -3 scripts/setup_wizard.py --force    # auch bei vorhandener .env
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.env_builder import (  # noqa: E402
    DEFAULT_MODELS,
    build_full_env,
    normalize_answers,
    restrict_env_permissions,
)

ENV_PATH = ROOT / ".env"


def _banner() -> None:
    print()
    print("=" * 56)
    print("  Local Futures Trader — Terminal Setup Assistant")
    print("=" * 56)
    print("  Values are stored only in the local .env file, never online.")
    print("  Trading stays DISARMED until you set TRADING_ENABLED=true.")
    print()


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    hint = f" [{default}]" if default and not secret else ""
    try:
        raw = getpass.getpass(f"{prompt}{hint}: ") if secret else input(f"{prompt}{hint}: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        raise SystemExit(1)
    raw = (raw or "").strip()
    return raw if raw else default


def _ask_yes(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    raw = _ask(f"{prompt} ({d})", "")
    if not raw:
        return default
    return raw.lower() in ("y", "yes", "1", "true")


def collect_answers() -> dict:
    """Interactively collect the SAME payload the web form posts."""
    _banner()
    payload: dict = {}

    # 1) Börse
    print("1) Exchange")
    print("   [1] Hyperliquid TESTNET  (recommended for practice; test funds)")
    print("   [2] Hyperliquid MAINNET  (real funds)")
    print("   [3] MEXC Futures         (API key; real funds)")
    choice = _ask("Selection", "1")
    if choice == "3":
        payload["exchange"] = "mexc"
    elif choice == "2":
        payload["exchange"] = "hl-mainnet"
        print("   MAINNET uses real funds. Type 'MAINNET' to confirm.")
        payload["mainnet_confirm"] = _ask("Confirmation")
    else:
        payload["exchange"] = "hl-testnet"

    if payload["exchange"] in ("hl-testnet", "hl-mainnet"):
        print()
        print("2) Hyperliquid keys (agent/API wallet — cannot withdraw)")
        pk = _ask("HL_PRIVATE_KEY (0x… 64 Hex)", secret=True)
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk
        addr = _ask("HL_ACCOUNT_ADDRESS (main wallet; often needed with an agent key)")
        if addr and not addr.startswith("0x"):
            addr = "0x" + addr
        payload["hl_private_key"] = pk
        payload["hl_account_address"] = addr
    else:
        print()
        print("2) MEXC API keys (trade only; no withdrawals)")
        payload["mexc_api_key"] = _ask("MEXC_API_KEY", secret=True)
        payload["mexc_api_secret"] = _ask("MEXC_API_SECRET", secret=True)

    # 3) KI
    print()
    print("3) AI provider")
    print("   [1] Claude  [2] Grok (xAI)  [3] OpenAI/Codex  [4] Ollama (local)  [5] none")
    pmap = {"1": "claude", "2": "xai", "3": "openai", "4": "ollama", "5": "none"}
    provider = pmap.get(_ask("Selection", "1"), "claude")
    payload["llm_provider"] = provider
    if provider in ("claude", "xai", "openai"):
        payload["llm_api_key"] = _ask(f"API key for {provider}", secret=True)
        payload["model"] = _ask("Model", DEFAULT_MODELS[provider])
    elif provider == "ollama":
        payload["model"] = _ask("Ollama model", DEFAULT_MODELS["ollama"])
    payload["include_account_in_llm"] = _ask_yes(
        "Send account data (equity/positions) to the AI?", default=False
    )

    # 4) Risiko
    print()
    print("4) Risk profile")
    print("   [1] conservative  [2] balanced  [3] free  [4] custom")
    rmap = {"1": "conservative", "2": "balanced", "3": "free", "4": "custom"}
    rp = rmap.get(_ask("Selection", "1"), "conservative")
    payload["risk_profile"] = rp
    if rp == "custom":
        payload["max_risk_pct"] = _ask("MAX_RISK_PCT (%)", "1.0")
        payload["max_leverage"] = _ask("MAX_LEVERAGE", "20")
        payload["min_rrr"] = _ask("MIN_RRR", "2.0")
        payload["max_notional_pct_of_equity"] = _ask(
            "MAX_NOTIONAL_PCT_OF_EQUITY (hard cap, % equity; 0=off)", "1000"
        )
    payload["max_notional_usdt"] = _ask("MAX_NOTIONAL_USDT (warning threshold)", "500")

    # 5) Erweitert
    print()
    payload["port"] = _ask("PORT", "8787")
    payload["host"] = "127.0.0.1"
    return payload


def _maybe_test_provider(payload: dict) -> None:
    provider = payload.get("llm_provider")
    if provider in (None, "none"):
        return
    if not _ask_yes("Test the AI key now (read-only ping)?", default=False):
        return
    try:
        import asyncio

        from app.llm.probe import probe_provider

        result = asyncio.run(
            probe_provider(
                provider,
                api_key=str(payload.get("llm_api_key") or ""),
                model=str(payload.get("model") or ""),
                ollama_base_url="http://127.0.0.1:11434/v1",
            )
        )
        status = "OK" if result.get("ok") else "ERROR"
        print(f"   [{status}] {result.get('detail')} ({result.get('latency_ms')} ms)")
    except Exception as e:  # pragma: no cover - best effort
        print(f"   Test skipped: {e}")


def _write_full_env(content: str) -> None:
    # B3-04/B3-02: unvorhersehbarer O_EXCL-tmp im .env-Verzeichnis (kein
    # fester Name, kein Symlink-Pre-Create) + ACL-Haertung VOR os.replace,
    # damit nie ein Secret-Fenster mit geerbten Rechten entsteht.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(ENV_PATH.parent), prefix=".env.setup-", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        restrict_env_permissions(tmp)  # haerten, bevor es die echte .env wird
        os.replace(tmp, ENV_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    restrict_env_permissions(ENV_PATH)  # belt-and-suspenders nach replace
    print(f"\nSaved: {ENV_PATH}")


def run_wizard(*, force: bool = False) -> None:
    if ENV_PATH.exists() and not force:
        print(f"{ENV_PATH} already exists. Run again with --force to replace it.")
        return

    payload = collect_answers()
    try:
        answers = normalize_answers(payload)
    except ValueError as e:
        print(f"\nInvalid input: {e}")
        raise SystemExit(1) from e

    _maybe_test_provider(payload)
    content = build_full_env(answers)
    if not _ask_yes("Save this configuration?", default=True):
        print("Not saved.")
        raise SystemExit(0)
    _write_full_env(content)
    print("Done. Start with: start.bat")
    print("Trading stays DISARMED until you set TRADING_ENABLED=true.")


def main() -> None:
    p = argparse.ArgumentParser(description="Setup assistant (.env)")
    p.add_argument("--force", action="store_true", help="Replace an existing .env")
    args = p.parse_args()
    run_wizard(force=args.force)


if __name__ == "__main__":
    main()
