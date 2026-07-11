#!/usr/bin/env python3
"""Interaktiver Einrichtungsassistent für .env (Hyperliquid / MEXC).

  py -3 scripts/setup_wizard.py
  py -3 scripts/setup_wizard.py --force   # immer starten, auch wenn schon eingerichtet

Wird vom Launcher aufgerufen, wenn Keys fehlen oder --setup gesetzt ist.
"""

from __future__ import annotations

import argparse
import getpass
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
MARKER_PATH = ROOT / "data" / ".setup_done"

# Reuse ensure_env helpers
sys.path.insert(0, str(ROOT / "scripts"))
from ensure_env import ensure_env, _parse_env, _read  # noqa: E402


def _banner() -> None:
    print()
    print("=" * 56)
    print("  Local Futures Trader — Einrichtungsassistent")
    print("=" * 56)
    print("  Werte landen nur in lokaler .env (nicht online).")
    print("  Enter = bestehenden Wert behalten / Default nehmen.")
    print()


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    hint = f" [{_mask(default) if secret and default else default}]" if default else ""
    try:
        if secret:
            # getpass hides input; show default hint only
            raw = getpass.getpass(f"{prompt}{hint}: ")
        else:
            raw = input(f"{prompt}{hint}: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAbgebrochen.")
        raise SystemExit(1)
    raw = (raw or "").strip()
    return raw if raw else default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    d = "J/n" if default else "j/N"
    raw = _ask(f"{prompt} ({d})", "")
    if not raw:
        return default
    return raw.lower() in ("j", "ja", "y", "yes", "1", "true")


def _mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "****"
    return v[:4] + "…" + v[-4:]


def _set_keys(env: dict[str, str], updates: dict[str, str]) -> None:
    for k, v in updates.items():
        env[k] = v


def _write_env(env: dict[str, str]) -> None:
    """Write .env preserving comments/order from example when possible."""
    example_text = _read(EXAMPLE_PATH) if EXAMPLE_PATH.is_file() else ""
    lines_out: list[str] = []
    seen: set[str] = set()

    if example_text:
        for line in example_text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                seen.add(k)
                val = env.get(k, "")
                lines_out.append(f"{k}={val}")
            else:
                lines_out.append(line)
    for k, v in env.items():
        if k not in seen:
            lines_out.append(f"{k}={v}")
            seen.add(k)

    ENV_PATH.write_text("\n".join(lines_out) + "\n", encoding="utf-8", newline="\n")
    print(f"\nGespeichert: {ENV_PATH}")


def needs_wizard(env: dict[str, str]) -> bool:
    """True if exchange credentials look incomplete."""
    ex = (env.get("EXCHANGE") or "hyperliquid").strip().lower()
    if ex in ("hyperliquid", "hl"):
        return not bool((env.get("HL_PRIVATE_KEY") or "").strip())
    return not (
        (env.get("MEXC_API_KEY") or "").strip()
        and (env.get("MEXC_API_SECRET") or "").strip()
    )


def run_wizard(*, force: bool = False) -> dict[str, str]:
    ensure_env(merge=True, auto_token=True)
    env = _parse_env(_read(ENV_PATH))

    if not force and not needs_wizard(env) and MARKER_PATH.is_file():
        print("Einrichtung scheint komplett (.env + Marker).")
        print("Erneut: py -3 scripts/setup_wizard.py --force")
        return env

    _banner()

    # --- Exchange ---
    print("1) Börse")
    print("   [1] Hyperliquid TESTNET  (empfohlen zum Üben, Spielgeld)")
    print("   [2] Hyperliquid MAINNET  (echtes Geld)")
    print("   [3] MEXC Futures         (API-Key, echtes Geld)")
    choice = _ask("Auswahl", "1")
    if choice == "3":
        exchange = "mexc"
        hl_testnet = "true"
    elif choice == "2":
        exchange = "hyperliquid"
        hl_testnet = "false"
    else:
        exchange = "hyperliquid"
        hl_testnet = "true"

    _set_keys(
        env,
        {
            "EXCHANGE": exchange,
            "HL_TESTNET": hl_testnet,
            "HOST": "127.0.0.1",
            "TRADING_ENABLED": "false",
        },
    )

    if exchange == "hyperliquid":
        print()
        print("2) Hyperliquid Keys")
        if hl_testnet == "true":
            print("   UI / Faucet: https://app.hyperliquid-testnet.xyz")
            print("   → Wallet verbinden → Test-USDC vom Faucet")
            print("   → API Wallet / Agent Key erzeugen (kann nicht withdrawen)")
        else:
            print("   UI: https://app.hyperliquid.xyz")
            print("   → API Wallet erzeugen — VORSICHT Mainnet = echtes Geld")
        print()
        pk = _ask(
            "HL_PRIVATE_KEY (0x… Agent/API-Key)",
            env.get("HL_PRIVATE_KEY", ""),
            secret=True,
        )
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk
        addr = _ask(
            "HL_ACCOUNT_ADDRESS (Main-Wallet, oft nötig bei Agent-Key)",
            env.get("HL_ACCOUNT_ADDRESS", ""),
        )
        if addr and not addr.startswith("0x"):
            addr = "0x" + addr
        sym = _ask("Default-Symbol (z.B. BTC, ETH, SOL)", env.get("DEFAULT_SYMBOL") or "BTC")
        _set_keys(
            env,
            {
                "HL_PRIVATE_KEY": pk,
                "HL_ACCOUNT_ADDRESS": addr,
                "DEFAULT_SYMBOL": sym.split("_")[0].upper(),
            },
        )
    else:
        print()
        print("2) MEXC API Keys (Trade only, KEIN Withdraw)")
        print("   https://www.mexc.com/user/openapi")
        key = _ask("MEXC_API_KEY", env.get("MEXC_API_KEY", ""), secret=True)
        sec = _ask("MEXC_API_SECRET", env.get("MEXC_API_SECRET", ""), secret=True)
        sym = _ask(
            "Default-Symbol (z.B. BTC_USDT)",
            env.get("DEFAULT_SYMBOL") or "BTC_USDT",
        )
        _set_keys(
            env,
            {
                "MEXC_API_KEY": key,
                "MEXC_API_SECRET": sec,
                "DEFAULT_SYMBOL": sym.upper(),
            },
        )

    # --- LLM (Claude default) ---
    print()
    print("3) Claude API (Analyse im UI) — https://console.anthropic.com/")
    print("   Key beginnt oft mit sk-ant-… — komplett einfügen.")
    _set_keys(env, {"LLM_PROVIDER": env.get("LLM_PROVIDER") or "claude"})
    if _ask_yes(
        "ANTHROPIC_API_KEY / Claude-Key jetzt setzen?",
        default=bool(env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_API_KEY")),
    ):
        claude = _ask(
            "ANTHROPIC_API_KEY",
            env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_API_KEY") or "",
            secret=True,
        )
        model = _ask(
            "ANTHROPIC_MODEL",
            env.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514",
        )
        _set_keys(
            env,
            {
                "LLM_PROVIDER": "claude",
                "ANTHROPIC_API_KEY": claude,
                "ANTHROPIC_MODEL": model,
            },
        )
    else:
        print("   Übersprungen — Analyse-Button braucht später einen Key.")

    # --- Risk defaults ---
    print()
    print("4) Risiko-Defaults (kannst du später in .env ändern)")
    max_lev = _ask("MAX_LEVERAGE", env.get("MAX_LEVERAGE") or "20")
    max_risk = _ask("MAX_RISK_PCT (% Equity)", env.get("MAX_RISK_PCT") or "1.0")
    max_not = _ask("MAX_NOTIONAL_USDT", env.get("MAX_NOTIONAL_USDT") or "500")
    port = _ask("PORT", env.get("PORT") or "8787")
    _set_keys(
        env,
        {
            "MAX_LEVERAGE": max_lev,
            "MAX_RISK_PCT": max_risk,
            "MAX_NOTIONAL_USDT": max_not,
            "PORT": port,
            "ALLOW_UNPROTECTED_ENTRY": "false",
            "STRICT_RRR": env.get("STRICT_RRR") or "true",
        },
    )

    # Local token
    if not (env.get("LOCAL_API_TOKEN") or "").strip():
        env["LOCAL_API_TOKEN"] = secrets.token_urlsafe(24)
        print("LOCAL_API_TOKEN auto-generiert.")

    # Trading always start disarmed
    env["TRADING_ENABLED"] = "false"

    print()
    print("5) Zusammenfassung")
    print(f"   EXCHANGE          = {env.get('EXCHANGE')}")
    if env.get("EXCHANGE") == "hyperliquid":
        print(f"   HL_TESTNET        = {env.get('HL_TESTNET')}")
        print(f"   HL_PRIVATE_KEY    = {_mask(env.get('HL_PRIVATE_KEY', '')) or '(leer)'}")
        print(f"   HL_ACCOUNT_ADDRESS= {_mask(env.get('HL_ACCOUNT_ADDRESS', '')) or '(leer)'}")
    else:
        print(f"   MEXC_API_KEY      = {_mask(env.get('MEXC_API_KEY', '')) or '(leer)'}")
    print(f"   DEFAULT_SYMBOL    = {env.get('DEFAULT_SYMBOL')}")
    print(
        f"   ANTHROPIC_API_KEY = "
        f"{'gesetzt' if (env.get('ANTHROPIC_API_KEY') or env.get('CLAUDE_API_KEY')) else '(leer)'}"
    )
    print(f"   LLM_PROVIDER      = {env.get('LLM_PROVIDER') or 'claude'}")
    print(f"   MAX_LEVERAGE      = {env.get('MAX_LEVERAGE')}")
    print(f"   MAX_RISK_PCT      = {env.get('MAX_RISK_PCT')}")
    print(f"   PORT              = {env.get('PORT')}")
    print(f"   TRADING_ENABLED   = false  (Absicht — erst nach Test scharf)")
    print()

    if not _ask_yes("So speichern und fortfahren?", default=True):
        print("Nicht gespeichert.")
        raise SystemExit(0)

    _write_env(env)
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text("ok\n", encoding="utf-8")

    # Optional connectivity check
    if env.get("EXCHANGE") == "hyperliquid" and env.get("HL_PRIVATE_KEY"):
        if _ask_yes("Kurzen Hyperliquid-Test ausführen (Meta/Mid/Account)?", default=True):
            _run_hl_check(env)
    elif env.get("EXCHANGE") == "mexc" and env.get("MEXC_API_KEY"):
        print("MEXC: nach Start Chart prüfen; Spike: scripts/mexc_spike.py")

    print()
    print("Fertig. Start: .\\start.bat   oder   py -3 scripts/launch.py")
    print("Trading bleibt DISARMED bis du TRADING_ENABLED=true setzt.")
    return env


def _run_hl_check(env: dict[str, str]) -> None:
    try:
        # Load env into process for get_settings
        import os

        for k, v in env.items():
            os.environ[k] = v
        # Clear settings cache if imported
        try:
            from app.config import get_settings

            get_settings.cache_clear()
        except Exception:
            pass
        import asyncio
        from app.hyperliquid.client import HyperliquidClient

        async def _go() -> None:
            c = HyperliquidClient(
                private_key=env.get("HL_PRIVATE_KEY", ""),
                account_address=env.get("HL_ACCOUNT_ADDRESS", ""),
                testnet=(env.get("HL_TESTNET", "true").lower() == "true"),
            )
            try:
                await c.ping()
                coin = (env.get("DEFAULT_SYMBOL") or "BTC").split("_")[0]
                t = await c.ticker(coin)
                print(f"   OK public: {coin} mid={t.last_price}")
                if env.get("HL_PRIVATE_KEY"):
                    snap = await c.account_snapshot()
                    print(
                        f"   OK account: equity≈{snap.get('equity_usdt')} "
                        f"available≈{snap.get('available_usdt')}"
                    )
            finally:
                await c.aclose()

        asyncio.run(_go())
    except Exception as e:
        print(f"   Check fehlgeschlagen: {e}")
        print("   Keys/Netz prüfen — App startet trotzdem.")


def main() -> None:
    p = argparse.ArgumentParser(description="Einrichtungsassistent (.env)")
    p.add_argument(
        "--force",
        action="store_true",
        help="Assistent auch starten, wenn schon eingerichtet",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Nur prüfen ob Wizard nötig (exit 0=ok, 2=needs setup)",
    )
    args = p.parse_args()
    ensure_env(merge=True, auto_token=True)
    env = _parse_env(_read(ENV_PATH))
    if args.check_only:
        raise SystemExit(2 if needs_wizard(env) else 0)
    run_wizard(force=args.force)


if __name__ == "__main__":
    main()
