#!/usr/bin/env python3
"""Headless Einrichtungsassistent (.env) — Fallback zum Browser-Setup.

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
    print("  Local Futures Trader — Einrichtungsassistent (Terminal)")
    print("=" * 56)
    print("  Werte landen nur in lokaler .env (nicht online).")
    print("  Trading bleibt DISARMED bis du TRADING_ENABLED=true setzt.")
    print()


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    hint = f" [{default}]" if default and not secret else ""
    try:
        raw = getpass.getpass(f"{prompt}{hint}: ") if secret else input(f"{prompt}{hint}: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAbgebrochen.")
        raise SystemExit(1)
    raw = (raw or "").strip()
    return raw if raw else default


def _ask_yes(prompt: str, default: bool = False) -> bool:
    d = "J/n" if default else "j/N"
    raw = _ask(f"{prompt} ({d})", "")
    if not raw:
        return default
    return raw.lower() in ("j", "ja", "y", "yes", "1", "true")


def collect_answers() -> dict:
    """Interactively collect the SAME payload the web form posts."""
    _banner()
    payload: dict = {}

    # 1) Börse
    print("1) Börse")
    print("   [1] Hyperliquid TESTNET  (empfohlen zum Üben, Spielgeld)")
    print("   [2] Hyperliquid MAINNET  (echtes Geld)")
    print("   [3] MEXC Futures         (API-Key, echtes Geld)")
    choice = _ask("Auswahl", "1")
    if choice == "3":
        payload["exchange"] = "mexc"
    elif choice == "2":
        payload["exchange"] = "hl-mainnet"
        print("   MAINNET = echtes Geld. Zum Bestätigen 'MAINNET' tippen.")
        payload["mainnet_confirm"] = _ask("Bestätigung")
    else:
        payload["exchange"] = "hl-testnet"

    if payload["exchange"] in ("hl-testnet", "hl-mainnet"):
        print()
        print("2) Hyperliquid Keys (Agent/API-Wallet — kann NICHT withdrawen)")
        pk = _ask("HL_PRIVATE_KEY (0x… 64 Hex)", secret=True)
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk
        addr = _ask("HL_ACCOUNT_ADDRESS (Main-Wallet, oft nötig bei Agent-Key)")
        if addr and not addr.startswith("0x"):
            addr = "0x" + addr
        payload["hl_private_key"] = pk
        payload["hl_account_address"] = addr
    else:
        print()
        print("2) MEXC API Keys (Trade only, KEIN Withdraw)")
        payload["mexc_api_key"] = _ask("MEXC_API_KEY", secret=True)
        payload["mexc_api_secret"] = _ask("MEXC_API_SECRET", secret=True)

    # 3) KI
    print()
    print("3) KI-Anbieter")
    print("   [1] Claude  [2] Grok (xAI)  [3] OpenAI/Codex  [4] Ollama (lokal)  [5] keiner")
    pmap = {"1": "claude", "2": "xai", "3": "openai", "4": "ollama", "5": "none"}
    provider = pmap.get(_ask("Auswahl", "1"), "claude")
    payload["llm_provider"] = provider
    if provider in ("claude", "xai", "openai"):
        payload["llm_api_key"] = _ask(f"API Key für {provider}", secret=True)
        payload["model"] = _ask("Modell", DEFAULT_MODELS[provider])
    elif provider == "ollama":
        payload["model"] = _ask("Ollama-Modell", DEFAULT_MODELS["ollama"])
    payload["include_account_in_llm"] = _ask_yes(
        "Account-Daten (Equity/Positionen) an die KI senden?", default=False
    )

    # 4) Risiko
    print()
    print("4) Risiko-Profil")
    print("   [1] conservative  [2] balanced  [3] free  [4] custom")
    rmap = {"1": "conservative", "2": "balanced", "3": "free", "4": "custom"}
    rp = rmap.get(_ask("Auswahl", "1"), "conservative")
    payload["risk_profile"] = rp
    if rp == "custom":
        payload["max_risk_pct"] = _ask("MAX_RISK_PCT (%)", "1.0")
        payload["max_leverage"] = _ask("MAX_LEVERAGE", "20")
        payload["min_rrr"] = _ask("MIN_RRR", "2.0")
        payload["max_notional_pct_of_equity"] = _ask(
            "MAX_NOTIONAL_PCT_OF_EQUITY (harter Cap, % Equity; 0=aus)", "1000"
        )
    payload["max_notional_usdt"] = _ask("MAX_NOTIONAL_USDT (Warnschwelle)", "500")

    # 5) Erweitert
    print()
    payload["port"] = _ask("PORT", "8787")
    payload["host"] = "127.0.0.1"
    return payload


def _maybe_test_provider(payload: dict) -> None:
    provider = payload.get("llm_provider")
    if provider in (None, "none"):
        return
    if not _ask_yes("KI-Key jetzt testen (read-only Ping)?", default=False):
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
        status = "OK" if result.get("ok") else "FEHLER"
        print(f"   [{status}] {result.get('detail')} ({result.get('latency_ms')} ms)")
    except Exception as e:  # pragma: no cover - best effort
        print(f"   Test übersprungen: {e}")


def _write_full_env(content: str) -> None:
    tmp = ENV_PATH.with_name(".env.setup-tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, ENV_PATH)
    restrict_env_permissions(ENV_PATH)  # B-08: never world-/group-readable
    print(f"\nGespeichert: {ENV_PATH}")


def run_wizard(*, force: bool = False) -> None:
    if ENV_PATH.exists() and not force:
        print(f"{ENV_PATH} existiert bereits. Erneut mit --force.")
        return

    payload = collect_answers()
    try:
        answers = normalize_answers(payload)
    except ValueError as e:
        print(f"\nUngültige Eingabe: {e}")
        raise SystemExit(1) from e

    _maybe_test_provider(payload)
    content = build_full_env(answers)
    if not _ask_yes("So speichern?", default=True):
        print("Nicht gespeichert.")
        raise SystemExit(0)
    _write_full_env(content)
    print("Fertig. Start: py -3 scripts/launch.py")
    print("Trading bleibt DISARMED bis du TRADING_ENABLED=true setzt.")


def main() -> None:
    p = argparse.ArgumentParser(description="Einrichtungsassistent (.env)")
    p.add_argument("--force", action="store_true", help="Auch bei vorhandener .env")
    args = p.parse_args()
    run_wizard(force=args.force)


if __name__ == "__main__":
    main()
