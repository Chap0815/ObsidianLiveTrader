#!/usr/bin/env python3
"""Write a minimal, SAFE bootstrap .env — only if none exists.

Runs with the project's venv python (so it can import app.env_builder) and keeps
the ONE builder: it delegates to app.env_builder.build_minimal_env. Never
overwrites an existing .env.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.env_builder import build_minimal_env, restrict_env_permissions  # noqa: E402

ENV_PATH = ROOT / ".env"


def main() -> None:
    p = argparse.ArgumentParser(description="Write minimal bootstrap .env")
    p.add_argument("--port", type=int, default=8787)
    args = p.parse_args()

    if ENV_PATH.exists():
        print(".env already exists — bootstrap is not needed.")
        return
    ENV_PATH.write_text(
        build_minimal_env(port=args.port), encoding="utf-8", newline="\n"
    )
    restrict_env_permissions(ENV_PATH)  # B-08: never world-/group-readable
    print(
        f"Bootstrap .env created. Complete setup in your browser: "
        f"http://127.0.0.1:{args.port}/setup"
    )


if __name__ == "__main__":
    main()
