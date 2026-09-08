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
  py -3 scripts/launch.py --setup-cli
  py -3 scripts/launch.py --skip-setup
  py -3 scripts/launch.py --no-browser
  py -3 scripts/launch.py --skip-install
"""

from __future__ import annotations

import sys

# W3-03: Versions-Gate MUSS als allererstes laufen — noch vor jedem Import,
# der auf einem zu alten Python (3.9/3.10) crashen könnte. Nur `sys` wird
# dafür gebraucht (stdlib, seit jeher vorhanden). Erst danach folgen die
# restlichen Imports.


def _check_python_version(version_info: tuple | None = None) -> None:
    """Bricht mit einer klaren deutschen Meldung ab, falls Python < 3.11.

    `version_info` ist injizierbar für Tests; im Normalbetrieb wird
    `sys.version_info` verwendet.
    """
    vi = version_info if version_info is not None else sys.version_info
    if tuple(vi[:2]) < (3, 11):
        found = f"{vi[0]}.{vi[1]}"
        msg = (
            "ERROR: This project requires Python 3.11 or newer "
            f"(found Python {found}).\n"
            "Install a current Python version: "
            "https://www.python.org/downloads/\n"
            "Windows tip: if 'python' opens the Microsoft Store, it is only a "
            "Store placeholder. Install Python 3.11+ separately and ensure "
            "that 'py -3' or 'python' points to it."
        )
        print(msg)
        raise SystemExit(1)


_check_python_version()

import argparse  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

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
    error_hint: str | None = None,
) -> int:
    print(">", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if check and r.returncode != 0:
        if error_hint:
            print(f"ERROR: {error_hint} (exit code {r.returncode})")
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

    print("Creating the project .venv (one-time, isolated from system Python)…")
    print(f"  System Python is used only for: python -m venv  →  {_system_python()}")
    # System interpreter is ONLY allowed for creating the venv module
    _run(
        [_system_python(), "-m", "venv", str(VENV_DIR)],
        error_hint=(
            "Could not create .venv — check your Python installation and "
            "write access to the project folder."
        ),
    )
    if not vpy.is_file():
        raise SystemExit(f"venv python missing after create: {vpy}")
    if not _is_project_venv(vpy):
        raise SystemExit(f"venv path check failed: {vpy}")
    print(f"venv created: {vpy}")
    return vpy


def _select_requirements_file() -> Path | None:
    """W3-05: `requirements.lock` (exakte Pins) bevorzugen, sonst Fallback auf
    `requirements.txt` (offene Ranges)."""
    lock = ROOT / "requirements.lock"
    if lock.is_file():
        return lock
    txt = ROOT / "requirements.txt"
    if txt.is_file():
        return txt
    return None


def _deps_marker_state(req: Path) -> str:
    """Marker-Inhalt: Dateiname + mtime des TATSÄCHLICH benutzten Files, damit
    ein Wechsel lock<->txt immer neu installiert (nicht nur eine mtime, sonst
    würde ein neu aufgetauchtes requirements.lock übersehen, solange die
    marker-mtime jünger als beide Dateien ist)."""
    return f"{req.name}:{req.stat().st_mtime}"


def ensure_deps(vpy: Path, *, skip: bool) -> None:
    if skip:
        print("Skip dependency install")
        return
    if not _is_project_venv(vpy):
        raise SystemExit(
            f"SAFETY: pip is allowed only in the project .venv, not: {vpy}"
        )

    marker = VENV_DIR / ".deps_installed"
    req = _select_requirements_file()
    if req is None:
        print("No requirements.lock/requirements.txt — skip install")
        return

    state = _deps_marker_state(req)
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == state:
        print(f"Dependencies already installed in .venv (marker up to date: {req.name})")
        return

    print(f"Installing requirements ({req.name}) in .venv only (not globally)…")
    env = _venv_env()
    pip_error_hint = (
        "pip install failed — check your connection or proxy. For an offline "
        "retry without installing, use '--skip-install'."
    )
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
        error_hint=pip_error_hint,
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
        error_hint=pip_error_hint,
    )
    marker.write_text(state, encoding="utf-8")
    print("Dependencies are ready in the project .venv.")


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


def bootstrap_env(vpy: Path, *, port: int) -> None:
    """Write a minimal safe .env (via the shared builder) if none exists.

    Canonical first-run path: the browser opens at /setup and the user finishes
    setup there. No interactive CLI wizard runs by default.
    """
    if (ROOT / ".env").is_file():
        return
    code = run_with_venv(vpy, "scripts/write_bootstrap_env.py", "--port", str(port))
    if code != 0:
        raise SystemExit(code)


def run_setup_cli(vpy: Path) -> None:
    """Headless fallback: the CLI wizard shares the same builder + questions."""
    code = run_with_venv(vpy, "scripts/setup_wizard.py", "--force")
    if code != 0:
        raise SystemExit(code)


def read_setup_complete() -> bool:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return False
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == "SETUP_COMPLETE":
            return v.strip().lower() in ("1", "true", "yes", "on")
    return False


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


def read_trading_banner() -> str:
    """Return an honest, secret-free trading state for launcher output."""
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "TRADING_ENABLED":
                armed = value.strip().lower() in ("1", "true", "yes", "on")
                return "ARMED — order submission is enabled" if armed else "DISARMED (safe default)"
    return "DISARMED (safe default)"


def _port_in_use(host: str, port: int) -> bool:
    """W3-11: advisory pre-check — a closed/free port must never block a
    legitimate start. Only a clear 'something is listening' (connect
    succeeds) counts as in-use; anything inconclusive (DNS error, timeout,
    OS error) returns False so the normal startup proceeds."""
    check_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex((check_host, port))
            return result == 0
    except OSError:
        return False


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Launch Local Futures Trader")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--skip-install", action="store_true")
    # W3-10: expliziter Alias — vorher matchte "--setup" nur zufällig per
    # argparse-Präfix-Abkürzung auf "--setup-cli"; sobald ein zweites
    # "--setup-*"-Flag dazukäme, würde das brechen ("ambiguous option").
    p.add_argument(
        "--setup",
        "--setup-cli",
        dest="setup_cli",
        action="store_true",
        help="Run the terminal setup assistant instead of browser setup",
    )
    p.add_argument("--skip-setup", action="store_true", help="Skip setup bootstrap")
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for the bootstrap .env (default 8787). Set this on first "
        "launch if the port is occupied; changes made in /setup apply on the "
        "next launch because a running server cannot rebind.",
    )
    p.add_argument("--reload", action="store_true", help="uvicorn --reload (dev)")
    return p


def main() -> None:
    p = _build_arg_parser()
    args = p.parse_args()

    os.chdir(ROOT)
    print("=" * 56)
    print("  Local Futures Trader — Launcher")
    print("=" * 56)
    print(f"  Project: {ROOT}")
    print(f"  System Python (venv creation only): {_system_python()}")
    print(f"  Project .venv:                    {_venv_python()}")
    print()

    # 1) venv first — all later steps use venv only
    vpy = ensure_venv(_venv_python())
    # 2) deps only into venv
    ensure_deps(vpy, skip=args.skip_install)
    # 3) first-run bootstrap: write a minimal safe .env (browser finishes setup)
    if not args.skip_setup:
        if args.setup_cli:
            run_setup_cli(vpy)
        else:
            bootstrap_env(vpy, port=(args.port or 8787))

    host, port = read_port_host()
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: HOST={host} is not loopback — forcing 127.0.0.1")
        host = "127.0.0.1"

    # Open the browser at /setup while setup is incomplete, else the dashboard.
    setup_open = not read_setup_complete()
    url = f"http://127.0.0.1:{port}{'/setup' if setup_open else ''}"
    print()
    print(f"Exchange: {read_exchange_banner()}")
    print(f"Server:   {url}")
    print(f"Python:   {vpy}  (venv only)")
    print(f"Trading:  {read_trading_banner()}")
    if setup_open:
        print("Next:     complete the guided setup in your browser")
    print("Ctrl+C to stop.")
    print()

    # W3-11: Port-Pre-Check — der häufigste "Fehler" ist gar keiner: der
    # Nutzer hat den Server schon laufen (z. B. zweiter Doppelklick auf
    # start.bat) und würde sonst einen englischen uvicorn-Bind-Traceback
    # sehen. Rein advisory: bei Unklarheit wird normal weitergestartet.
    if _port_in_use(host, port):
        print(f"Already running on port {port} — opening the browser; no second server needed.")
        print(f"  URL: {url}")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        raise SystemExit(0)

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
