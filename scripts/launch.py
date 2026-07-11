#!/usr/bin/env python3
"""Setup + optional wizard + launch Local Futures Trader.

WICHTIG — Python-Isolation:
  - Die System-/Haupt-Python dient NUR dazu, die Projekt-.venv zu erzeugen.
  - pip install, Assistent, uvicorn laufen AUSSCHLIESSLICH mit
    .venv\\Scripts\\python.exe (bzw. .venv/bin/python).
  - Es wird NIEMALS global / user-site installiert.

Usage:
  py -3 scripts/launch.py
  py -3 scripts/launch.py --setup
  py -3 scripts/launch.py --skip-setup
  py -3 scripts/launch.py --no-browser
  py -3 scripts/launch.py --skip-install
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"


def _system_python() -> str:
    """Interpreter, der den Launcher gestartet hat (nur für `python -m venv`)."""
    return sys.executable


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _is_project_venv(python_exe: Path) -> bool:
    """True only if this interpreter lives inside THIS project's .venv."""
    try:
        resolved = python_exe.resolve()
        venv_root = VENV_DIR.resolve()
        return venv_root in resolved.parents or resolved.parent == venv_root
    except OSError:
        return False


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> int:
    print(">", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if check and r.returncode != 0:
        raise SystemExit(r.returncode)
    return r.returncode


def _venv_env() -> dict[str, str]:
    """Env for child processes: force virtualenv for pip, no user installs."""
    e = os.environ.copy()
    e["VIRTUAL_ENV"] = str(VENV_DIR.resolve())
    e["PIP_REQUIRE_VIRTUALENV"] = "1"
    e["PIP_USER"] = "0"
    e["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    # Prefer venv scripts on PATH for this process tree
    if os.name == "nt":
        scripts = str(VENV_DIR / "Scripts")
    else:
        scripts = str(VENV_DIR / "bin")
    e["PATH"] = scripts + os.pathsep + e.get("PATH", "")
    # Drop Python user-site influence
    e["PYTHONNOUSERSITE"] = "1"
    return e


def ensure_venv(vpy: Path) -> Path:
    if vpy.is_file() and _is_project_venv(vpy):
        print(f"venv OK (project-local only): {vpy}")
        return vpy

    if vpy.is_file() and not _is_project_venv(vpy):
        raise SystemExit(
            f"Refusing to use unexpected Python (not project .venv): {vpy}"
        )

    print("Erzeuge Projekt-.venv (einmalig, isoliert von System-Python) …")
    print(f"  System-Python nur für: python -m venv  →  {_system_python()}")
    # System interpreter is ONLY allowed for creating the venv module
    _run([_system_python(), "-m", "venv", str(VENV_DIR)])
    if not vpy.is_file():
        raise SystemExit(f"venv python missing after create: {vpy}")
    if not _is_project_venv(vpy):
        raise SystemExit(f"venv path check failed: {vpy}")
    print(f"venv erstellt: {vpy}")
    return vpy


def ensure_deps(vpy: Path, *, skip: bool) -> None:
    if skip:
        print("Skip dependency install")
        return
    if not _is_project_venv(vpy):
        raise SystemExit(
            f"SICHERHEIT: pip nur im Projekt-.venv erlaubt, nicht: {vpy}"
        )

    marker = VENV_DIR / ".deps_installed"
    req = ROOT / "requirements.txt"
    if not req.is_file():
        print("No requirements.txt — skip install")
        return
    if marker.is_file() and marker.stat().st_mtime >= req.stat().st_mtime:
        print("Dependencies already installed in .venv (marker up to date)")
        return

    print("Installiere requirements NUR in .venv (nicht global) …")
    env = _venv_env()
    # Explicit: python.exe from .venv + -m pip + --require-virtualenv
    _run(
        [
            str(vpy),
            "-m",
            "pip",
            "install",
            "--require-virtualenv",
            "--upgrade",
            "pip",
        ],
        env=env,
    )
    _run(
        [
            str(vpy),
            "-m",
            "pip",
            "install",
            "--require-virtualenv",
            "-r",
            str(req),
        ],
        env=env,
    )
    marker.write_text("ok\n", encoding="utf-8")
    print("pip fertig — nur .venv betroffen.")


def run_with_venv(vpy: Path, script_rel: str, *args: str) -> int:
    """Run a project script with the venv interpreter (not system Python)."""
    if not _is_project_venv(vpy):
        raise SystemExit(f"Refusing non-venv interpreter: {vpy}")
    script = ROOT / script_rel
    return _run(
        [str(vpy), str(script), *args],
        env=_venv_env(),
        check=False,
    )


def ensure_env(vpy: Path) -> None:
    code = run_with_venv(vpy, "scripts/ensure_env.py")
    if code != 0:
        raise SystemExit(code)


def maybe_run_wizard(vpy: Path, *, force: bool, skip: bool) -> None:
    if skip:
        print("Setup-Assistent übersprungen (--skip-setup)")
        return
    if force:
        code = run_with_venv(vpy, "scripts/setup_wizard.py", "--force")
        if code != 0:
            raise SystemExit(code)
        return
    code = run_with_venv(vpy, "scripts/setup_wizard.py", "--check-only")
    if code == 2:
        print()
        print("Exchange-Keys fehlen noch — starte Einrichtungsassistent …")
        print()
        code = run_with_venv(vpy, "scripts/setup_wizard.py")
        if code != 0:
            raise SystemExit(code)
    elif code == 0:
        print("Einrichtung OK (Keys vorhanden). Assistent: launch.py --setup")
    else:
        # ensure_env noise may still exit 0; non-0/2 is error
        print(f"Hinweis: setup_wizard --check-only exit {code}")


def read_port_host() -> tuple[str, int]:
    env_path = ROOT / ".env"
    host = "127.0.0.1"
    port = 8787
    if not env_path.is_file():
        return host, port
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "HOST" and v:
            host = v
        if k == "PORT" and v.isdigit():
            port = int(v)
    return host, port


def read_exchange_banner() -> str:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return "exchange=?"
    data: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    ex = data.get("EXCHANGE", "mexc")
    if ex == "hyperliquid":
        net = (
            "TESTNET"
            if data.get("HL_TESTNET", "true").lower() == "true"
            else "MAINNET"
        )
        return f"Hyperliquid {net}"
    return "MEXC"


def main() -> None:
    p = argparse.ArgumentParser(description="Launch Local Futures Trader")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--skip-install", action="store_true")
    p.add_argument("--setup", action="store_true", help="Immer Einrichtungsassistent")
    p.add_argument("--skip-setup", action="store_true", help="Kein Wizard")
    p.add_argument("--reload", action="store_true", help="uvicorn --reload (dev)")
    args = p.parse_args()

    os.chdir(ROOT)
    print("=" * 56)
    print("  Local Futures Trader — Launcher")
    print("=" * 56)
    print(f"  Project: {ROOT}")
    print(f"  System-Python (nur venv-Create): {_system_python()}")
    print(f"  Projekt-venv:                   {_venv_python()}")
    print()

    # 1) venv first — all later steps use venv only
    vpy = ensure_venv(_venv_python())
    # 2) deps only into venv
    ensure_deps(vpy, skip=args.skip_install)
    # 3) env + wizard via venv python
    ensure_env(vpy)
    maybe_run_wizard(vpy, force=args.setup, skip=args.skip_setup)

    host, port = read_port_host()
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: HOST={host} is not loopback — forcing 127.0.0.1")
        host = "127.0.0.1"

    url = f"http://127.0.0.1:{port}"
    print()
    print(f"Exchange: {read_exchange_banner()}")
    print(f"Server:   {url}")
    print(f"Python:   {vpy}  (venv only)")
    print("Trading:  DISARMED until TRADING_ENABLED=true in .env")
    print("Ctrl+C to stop.")
    print()

    if not args.no_browser:

        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        import threading

        threading.Thread(target=_open, daemon=True).start()

    # Server also only via venv
    cmd = [
        str(vpy),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if args.reload:
        cmd.append("--reload")
    raise SystemExit(subprocess.call(cmd, cwd=str(ROOT), env=_venv_env()))


if __name__ == "__main__":
    main()
