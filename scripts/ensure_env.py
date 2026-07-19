#!/usr/bin/env python3
"""Create or refresh local .env from .env.example (never overwrites secrets).

Usage (project root):
  py -3 scripts/ensure_env.py
  py -3 scripts/ensure_env.py --merge   # add missing keys from example only
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

# Keys that are safe to auto-fill if empty (not secrets the user must choose)
AUTO_FILL_EMPTY = {
    "LOCAL_API_TOKEN": lambda: secrets.token_urlsafe(24),
}


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        out[k.strip()] = v.strip()
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def ensure_env(*, merge: bool = True, auto_token: bool = True) -> dict[str, str]:
    if not EXAMPLE_PATH.is_file():
        raise SystemExit(f"Missing template: {EXAMPLE_PATH}")

    example = _read(EXAMPLE_PATH)
    created = False

    if not ENV_PATH.is_file():
        ENV_PATH.write_text(example, encoding="utf-8", newline="\n")
        created = True
        print(f"Created {ENV_PATH.name} from .env.example")
    else:
        print(f"{ENV_PATH.name} already exists")

    if merge or created:
        current_text = _read(ENV_PATH)
        current = _parse_env(current_text)
        example_map = _parse_env(example)
        missing = [k for k in example_map if k not in current]
        lines = current_text.splitlines()
        if missing:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("# --- added by ensure_env (missing keys from .env.example) ---")
            for k in missing:
                lines.append(f"{k}={example_map[k]}")
            ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            print(f"Merged {len(missing)} missing key(s): {', '.join(missing)}")
            current_text = _read(ENV_PATH)
            current = _parse_env(current_text)

        if auto_token:
            changed = False
            new_lines: list[str] = []
            for line in current_text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    key = k.strip()
                    val = v.strip()
                    if key in AUTO_FILL_EMPTY and not val:
                        fill = AUTO_FILL_EMPTY[key]()
                        new_lines.append(f"{key}={fill}")
                        changed = True
                        print(f"Auto-filled empty {key}")
                        continue
                new_lines.append(line)
            if changed:
                ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")
                current = _parse_env(_read(ENV_PATH))

        # W3-02: SETUP_COMPLETE MUST be present. A .env without it defaults the
        # marker to false (fail-safe), which would re-open the UNAUTHENTICATED
        # /setup save — and that save OVERWRITES the entire hand-maintained
        # .env. Idempotent: add it only if the key is absent (never touch or
        # duplicate an existing value, incl. a deliberate SETUP_COMPLETE=false
        # bootstrap). The generic merge above already covers this once the key
        # ships in .env.example; this is the explicit, template-independent guard.
        current_text = _read(ENV_PATH)
        if "SETUP_COMPLETE" not in _parse_env(current_text):
            lines = current_text.splitlines()
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("SETUP_COMPLETE=true")
            ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            print("Added missing SETUP_COMPLETE=true (W3-02 setup lock)")
            current = _parse_env(_read(ENV_PATH))

    # B-08: .env traegt echte Secrets (auto-generierter LOCAL_API_TOKEN) —
    # ACL wie bei allen anderen Schreibpfaden verengen (best-effort).
    try:
        sys.path.insert(0, str(ROOT))
        from app.env_builder import restrict_env_permissions

        restrict_env_permissions(ENV_PATH)
    except Exception:
        pass

    # Status (never print secret values)
    current = _parse_env(_read(ENV_PATH))
    checks = [
        ("EXCHANGE", current.get("EXCHANGE", "mexc")),
        ("HL_TESTNET", current.get("HL_TESTNET", "")),
        ("HL_PRIVATE_KEY", bool(current.get("HL_PRIVATE_KEY"))),
        ("HL_ACCOUNT_ADDRESS", bool(current.get("HL_ACCOUNT_ADDRESS"))),
        ("MEXC_API_KEY", bool(current.get("MEXC_API_KEY"))),
        ("MEXC_API_SECRET", bool(current.get("MEXC_API_SECRET"))),
        ("LLM_PROVIDER", current.get("LLM_PROVIDER") or "claude"),
        (
            "ANTHROPIC_API_KEY",
            bool(current.get("ANTHROPIC_API_KEY") or current.get("CLAUDE_API_KEY")),
        ),
        ("XAI_API_KEY", bool(current.get("XAI_API_KEY"))),
        ("TRADING_ENABLED", current.get("TRADING_ENABLED", "false")),
        ("HOST", current.get("HOST", "")),
        ("PORT", current.get("PORT", "")),
    ]
    print("--- .env status ---")
    for k, v in checks:
        if isinstance(v, bool):
            print(f"  {k}: {'set' if v else 'EMPTY — fill in .env'}")
        else:
            print(f"  {k}: {v}")
    print(
        "Note: TRADING_ENABLED should stay false until spike/tests are OK. "
        "Never commit .env."
    )
    return current


def main() -> None:
    p = argparse.ArgumentParser(description="Ensure .env exists from .env.example")
    p.add_argument(
        "--no-merge",
        action="store_true",
        help="Do not add missing keys if .env already exists",
    )
    p.add_argument(
        "--no-auto-token",
        action="store_true",
        help="Do not auto-generate LOCAL_API_TOKEN when empty",
    )
    args = p.parse_args()
    ensure_env(merge=not args.no_merge, auto_token=not args.no_auto_token)


if __name__ == "__main__":
    main()
    sys.exit(0)
