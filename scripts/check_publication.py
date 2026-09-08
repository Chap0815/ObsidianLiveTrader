"""Check the tracked publication boundary without opening private/runtime files."""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath


def forbidden_path(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    parts = [part.lower() for part in path.parts]
    if not parts or path.is_absolute() or ".." in parts:
        return True
    if name == ".env.example":
        return False
    private_dirs = {
        ".git", ".venv", "venv", "data", "__pycache__", ".superpowers",
        ".pytest_cache", ".ruff_cache", "node_modules", "test-results",
        "playwright-report",
    }
    if any(part in private_dirs for part in parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    leaf = parts[-1]
    return (
        leaf in {"agents.md", "new_session_prompt.md", "project.md", "agenda.md"}
        or name.replace("\\", "/") == "docs/HARDENING_LOG.md"
        or (parts[0] == "docs" and leaf.startswith("audit") and leaf.endswith(".md"))
        or parts[:2] == ["docs", "superpowers"]
        or leaf.startswith("id_rsa")
        or leaf.endswith((
            ".key", ".pem", ".secret", ".db", ".db-wal", ".db-shm",
            ".sqlite", ".sqlite3", ".log", ".bak", ".bundle", ".lnk", ".pyc",
        ))
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    names = [p.decode("utf-8") for p in tracked.split(b"\0") if p]
    blocked = [name for name in names if forbidden_path(name)]
    required = {
        "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md", "SUPPORT.md", "THIRD_PARTY_NOTICES.md",
        "LICENSES/PolyForm-Noncommercial-1.0.0.md",
        "LICENSES/Lightweight-Charts-Apache-2.0.txt",
        "LICENSES/Lightweight-Charts-NOTICE.txt", "LICENSES/IBM-Plex-OFL-1.1.txt",
        ".github/CODEOWNERS", ".github/workflows/checks.yml",
    }
    missing = sorted(required.difference(names))
    for name in blocked:
        print(f"Private/runtime path is tracked: {name}")
    for name in missing:
        print(f"Required publication file is not tracked: {name}")
    if blocked or missing:
        return 1
    print(f"Publication boundary passed: {len(names)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
