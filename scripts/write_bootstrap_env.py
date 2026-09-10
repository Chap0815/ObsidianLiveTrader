#!/usr/bin/env python3
"""Write a minimal, SAFE bootstrap .env — only if none exists.

Runs with the project's venv python (so it can import app.env_builder) and keeps
the ONE builder: it delegates to app.env_builder.build_minimal_env. Never
overwrites an existing .env.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.env_builder import build_minimal_env, restrict_env_permissions  # noqa: E402

ENV_PATH = ROOT / ".env"


def _port_arg(raw: str) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not (1024 <= port <= 65535):
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def write_bootstrap_env(path: Path, *, port: int) -> bool:
    """Publish a complete bootstrap file exactly once without overwriting."""
    path = Path(path)
    if path.exists():
        return False

    port = _port_arg(str(port))
    content = build_minimal_env(port=port)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        restrict_env_permissions(tmp)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)

    restrict_env_permissions(path)
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Write minimal bootstrap .env")
    p.add_argument("--port", type=_port_arg, default=8787)
    args = p.parse_args()

    if not write_bootstrap_env(ENV_PATH, port=args.port):
        print(".env already exists — bootstrap is not needed.")
        return
    print(
        f"Bootstrap .env created. Complete setup in your browser: "
        f"http://127.0.0.1:{args.port}/setup"
    )


if __name__ == "__main__":
    main()
