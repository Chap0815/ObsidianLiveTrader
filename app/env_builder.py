"""Single source of truth for building and patching the project's ``.env``.

Pure-python (no FastAPI import) so it can be shared by the web setup route
(``app/main.py``), the CLI wizard (``scripts/setup_wizard.py``) and the
bootstrap step in ``scripts/launch.py``.

Design goals:
- ONE builder — kills the two drifting builders (main.build_env_content vs
  setup_wizard._write_env) and the PORT 8788-vs-8787 bug.
- Whitelist-only writes for the post-setup key patcher (never TRADING_ENABLED,
  HOST, EXCHANGE or exchange creds).
- Injection-safe values (no control chars / embedded newlines) and atomic
  temp+os.replace writes (no torn file that could drop a safety line).
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

# ── Whitelists ──────────────────────────────────────────────────────────────
# Provider -> (api_key_var, model_var). Ollama has no key.
LLM_KEY_VARS: dict[str, tuple[str | None, str]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    "xai": ("XAI_API_KEY", "XAI_MODEL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL"),
    "ollama": (None, "OLLAMA_MODEL"),
}

DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-5",  # NOT the stale sonnet id
    "xai": "grok-4",
    "openai": "gpt-5.1",
    "ollama": "llama3.1",
}

# Vars the authed patch endpoint (POST /api/settings/llm-key) may touch.
# Nothing else — no HOST/PORT/TRADING_ENABLED/EXCHANGE/keys for other subsystems.
SETTINGS_LLM_WRITABLE: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "XAI_API_KEY",
        "XAI_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OLLAMA_MODEL",
    }
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_HEX64 = re.compile(r"0x[0-9a-fA-F]{64}")
_HEX40 = re.compile(r"0x[0-9a-fA-F]{40}")


# ── Value sanitation (injection guard — reuse everywhere) ───────────────────
def sanitize_env_value(name: str, raw) -> str:
    """Strip; reject control chars / newlines.

    A value with an embedded newline could inject extra .env lines
    (e.g. ``TRADING_ENABLED=true``), so any control char is a hard error.
    """
    v = str(raw if raw is not None else "").strip()
    if any(ord(ch) < 32 or ch == "\x7f" for ch in v):
        raise ValueError(f"{name} enthält ungültige Steuerzeichen")
    return v


# ── Answer normalization (shared by web + CLI) ──────────────────────────────
def normalize_answers(payload: dict) -> dict:
    """Validate + coerce the raw setup payload into a canonical answer dict.

    Raises ValueError on bad input. Same rules for web JSON and CLI.
    """
    p = payload or {}

    def _san(name: str, key: str) -> str:
        return sanitize_env_value(name, p.get(key))

    ex = str(p.get("exchange") or "hl-testnet").strip()
    if ex not in ("hl-testnet", "hl-mainnet", "mexc"):
        raise ValueError("Ungültige Börsen-Auswahl")
    is_mexc = ex == "mexc"

    if ex == "hl-mainnet":
        confirm = _san("Mainnet-Bestätigung", "mainnet_confirm")
        if confirm != "MAINNET":
            raise ValueError(
                "Hyperliquid MAINNET erfordert die getippte Bestätigung 'MAINNET'"
            )

    hl_key = _san("Private Key", "hl_private_key")
    hl_addr = _san("Wallet-Adresse", "hl_account_address")
    mexc_key = _san("MEXC API Key", "mexc_api_key")
    mexc_sec = _san("MEXC API Secret", "mexc_api_secret")

    if is_mexc:
        if not (mexc_key and mexc_sec):
            raise ValueError("MEXC API Key und Secret werden benötigt")
    else:
        if not _HEX64.fullmatch(hl_key):
            raise ValueError(
                "Hyperliquid Private Key muss 0x + 64 Hex-Zeichen sein "
                "(API/Agent-Wallet-Key aus der Hyperliquid-UI)"
            )
        if hl_addr and not _HEX40.fullmatch(hl_addr):
            raise ValueError("Wallet-Adresse muss 0x + 40 Hex-Zeichen sein")

    llm = str(p.get("llm_provider") or "claude").strip().lower()
    if llm not in ("claude", "xai", "openai", "ollama", "none"):
        raise ValueError("Ungültiger KI-Anbieter")
    llm_key = _san("KI API Key", "llm_api_key")
    if llm in ("claude", "xai", "openai") and not llm_key:
        raise ValueError(f"API Key für {llm} fehlt")
    model = _san("KI-Modell", "model")

    include_account = bool(p.get("include_account_in_llm", False))

    risk_profile = str(p.get("risk_profile") or "balanced").strip().lower()
    if risk_profile not in ("conservative", "balanced", "free", "custom"):
        raise ValueError("Ungültiges Risiko-Profil")

    def _num(name: str, key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(p.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"{name} ist keine Zahl") from None
        if not (lo <= v <= hi):
            raise ValueError(f"{name} muss zwischen {lo} und {hi} liegen")
        return v

    answers: dict = {
        "exchange": ex,
        "is_mexc": is_mexc,
        "hl_private_key": hl_key,
        "hl_account_address": hl_addr,
        "mexc_api_key": mexc_key,
        "mexc_api_secret": mexc_sec,
        "llm_provider": llm,
        "llm_api_key": llm_key,
        "model": model,
        "include_account_in_llm": include_account,
        "risk_profile": risk_profile,
    }

    if risk_profile == "custom":
        answers["max_risk_pct"] = _num("MAX_RISK_PCT", "max_risk_pct", 1.0, 0.1, 50)
        answers["max_leverage"] = int(
            _num("MAX_LEVERAGE", "max_leverage", 20, 1, 125)
        )
        answers["min_rrr"] = _num("MIN_RRR", "min_rrr", 2.0, 1, 10)
        answers["max_notional_pct_of_equity"] = _num(
            "MAX_NOTIONAL_PCT_OF_EQUITY",
            "max_notional_pct_of_equity",
            1000.0,
            0,
            100000,
        )
    # warning threshold — always kept (custom + presets)
    answers["max_notional_usdt"] = _num(
        "MAX_NOTIONAL_USDT", "max_notional_usdt", 500, 0, 1_000_000
    )

    host = _san("Host", "host") or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("HOST muss loopback sein (127.0.0.1 / localhost / ::1)")
    answers["host"] = host

    try:
        port = int(p.get("port", 8787) or 8787)
    except (TypeError, ValueError):
        raise ValueError("PORT ist keine Zahl") from None
    if not (1024 <= port <= 65535):
        raise ValueError("PORT muss zwischen 1024 und 65535 liegen")
    answers["port"] = port

    return answers


# ── Builder entry points ────────────────────────────────────────────────────
def build_minimal_env(
    *, host: str = "127.0.0.1", port: int = 8787, token: str | None = None
) -> str:
    """Bootstrap .env written by launch.py before the browser opens.

    Safe posture; SETUP_COMPLETE=false keeps /setup open. No exchange/LLM keys
    exist yet, so all readiness flags stay false and the open /setup window is
    keyless.
    """
    if host not in _LOOPBACK_HOSTS:
        host = "127.0.0.1"
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8787
    if not (1024 <= port <= 65535):
        port = 8787
    tok = (token or "").strip() or secrets.token_urlsafe(24)
    lines = [
        f"# Bootstrap — finish setup at http://127.0.0.1:{port}/setup",
        "SETUP_COMPLETE=false",
        "HOST=127.0.0.1",
        f"PORT={port}",
        "EXCHANGE=hyperliquid",
        "HL_TESTNET=true",
        "LLM_PROVIDER=claude",
        f"LOCAL_API_TOKEN={tok}",
        "TRADING_ENABLED=false",
        "INCLUDE_ACCOUNT_IN_LLM=false",
        "ALLOW_MANUAL_TRIGGER=false",
        "ALLOW_UNPROTECTED_ENTRY=false",
        "REQUIRE_LOOPBACK_WHEN_ARMED=true",
        "STRICT_AVAILABLE_MARGIN=true",
        "RISK_PROFILE=conservative",
        "",
    ]
    return "\n".join(lines)


def build_full_env(answers: dict) -> str:
    """Complete .env from a NORMALIZED answer dict (call normalize_answers first).

    Writes SETUP_COMPLETE=true. Honors the RISK_PROFILE preset semantics: for a
    preset only ``RISK_PROFILE`` is written (so ``_apply_risk_profile`` fills the
    limits); raw MAX_* lines are written ONLY for a "custom" profile.
    """
    a = answers or {}
    is_mexc = a.get("is_mexc", a.get("exchange") == "mexc")
    hl_testnet = a.get("exchange") != "hl-mainnet"
    llm = str(a.get("llm_provider") or "claude").lower()
    provider_for_env = "claude" if llm == "none" else llm
    token = secrets.token_urlsafe(24)
    default_symbol = "BTC_USDT" if is_mexc else "BTC"

    # Per-provider key + model. Chosen provider gets its key; others empty.
    def _key_for(prov: str) -> str:
        return a.get("llm_api_key", "") if llm == prov else ""

    def _model_for(prov: str) -> str:
        if llm == prov and a.get("model"):
            return a["model"]
        return DEFAULT_MODELS[prov]

    lines: list[str] = [
        "# Generiert vom Setup-Assistenten — Werte jederzeit hier änderbar.",
        "SETUP_COMPLETE=true",
        "HOST=127.0.0.1",
        f"PORT={a.get('port', 8787)}",
        "",
        f"EXCHANGE={'mexc' if is_mexc else 'hyperliquid'}",
        f"HL_TESTNET={'true' if hl_testnet else 'false'}",
        f"HL_PRIVATE_KEY={a.get('hl_private_key', '')}",
        f"HL_ACCOUNT_ADDRESS={a.get('hl_account_address', '')}",
        f"MEXC_API_KEY={a.get('mexc_api_key', '')}",
        f"MEXC_API_SECRET={a.get('mexc_api_secret', '')}",
        "MEXC_BASE_URL=https://contract.mexc.com",
        "",
        "# KI (Hot-Swap im UI möglich)",
        f"LLM_PROVIDER={provider_for_env}",
        f"ANTHROPIC_API_KEY={_key_for('claude')}",
        f"ANTHROPIC_MODEL={_model_for('claude')}",
        f"XAI_API_KEY={_key_for('xai')}",
        f"XAI_MODEL={_model_for('xai')}",
        f"OPENAI_API_KEY={_key_for('openai')}",
        f"OPENAI_MODEL={_model_for('openai')}",
        "OLLAMA_BASE_URL=http://127.0.0.1:11434/v1",
        f"OLLAMA_MODEL={_model_for('ollama')}",
        f"INCLUDE_ACCOUNT_IN_LLM={'true' if a.get('include_account_in_llm') else 'false'}",
        "",
        "# Risiko-Gates (serverseitig erzwungen)",
    ]

    risk_profile = str(a.get("risk_profile") or "balanced").lower()
    if risk_profile == "custom":
        # Base preset + explicit overrides (the raw numbers win via model_fields_set).
        lines += [
            "RISK_PROFILE=balanced",
            f"MAX_RISK_PCT={a.get('max_risk_pct', 1.0)}",
            f"MAX_LEVERAGE={a.get('max_leverage', 20)}",
            f"MIN_RRR={a.get('min_rrr', 2.0)}",
            # RRR below MIN_RRR is a warning, not a hard block — the trader decides
            # (SL still mandatory; risk-% / notional / leverage stay hard).
            "STRICT_RRR=false",
            f"MAX_NOTIONAL_PCT_OF_EQUITY={a.get('max_notional_pct_of_equity', 1000.0)}",
        ]
    else:
        # Preset only — leave the MAX_* lines out so _apply_risk_profile fills them.
        lines.append(f"RISK_PROFILE={risk_profile}")

    lines += [
        f"MAX_NOTIONAL_USDT={a.get('max_notional_usdt', 500)}",
        "RISK_SLIPPAGE_PCT=0.05",
        "MAX_PRICE_DRIFT_PCT=0.5",
        "MARKET_ENTRY_SLIPPAGE_PCT=0.15",
        "ALLOW_CROSS_MARGIN=false",
        "ALLOW_UNPROTECTED_ENTRY=false",
        "AUTO_FLATTEN_IF_SL_UNVERIFIED=true",
        "",
        "# Sicherheit — Trading bleibt aus, bis DU es hier einschaltest",
        "TRADING_ENABLED=false",
        f"LOCAL_API_TOKEN={token}",
        "REQUIRE_LOOPBACK_WHEN_ARMED=true",
        "STRICT_AVAILABLE_MARGIN=true",
        "ALLOW_MANUAL_TRIGGER=false",
        "",
        f"DEFAULT_SYMBOL={default_symbol}",
        "PREVIEW_TOKEN_TTL_SECONDS=60",
        "DATABASE_PATH=data/trader.db",
        "KLINE_LIMIT_HINT=500",
        "",
    ]
    return "\n".join(lines)


# ── Atomic patcher (post-setup key writes) ──────────────────────────────────
def patch_env_vars(
    env_path: Path, updates: dict[str, str], *, allowed: set[str]
) -> None:
    """In-place update of ONLY whitelisted vars, preserving every other line,
    comment and order. Atomic. Raises ValueError on a non-whitelisted key or a
    value with control chars.
    """
    env_path = Path(env_path)
    clean: dict[str, str] = {}
    for k, v in (updates or {}).items():
        if k not in allowed:
            raise ValueError(f"{k} ist nicht beschreibbar")
        clean[k] = sanitize_env_value(k, v)

    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = text.split("\n")

    remaining = dict(clean)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    if remaining:
        footer = "# --- updated by settings (KI-Keys) ---"
        # trim a single trailing empty line for tidy append
        if lines and lines[-1] == "":
            lines.pop()
        if footer not in lines:
            lines.append("")
            lines.append(footer)
        for k, v in remaining.items():
            lines.append(f"{k}={v}")
        lines.append("")

    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    os.replace(tmp, env_path)
