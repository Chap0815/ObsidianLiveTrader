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
import hashlib  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
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
    """Identify the selected requirements file by name and exact content."""
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    return f"{req.name}:sha256:{digest}"


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


_LAUNCH_ENV_KEYS = frozenset(
    {"SETUP_COMPLETE", "HOST", "PORT", "EXCHANGE", "HL_TESTNET", "TRADING_ENABLED"}
)
_ENV_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
_ENV_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "f"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ENV_INTEGER_RE = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)*(?:\.0+)?\Z")


def _parse_env_scalar(raw: str) -> str:
    value = raw.strip()
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1].isspace():
            value = value[:index].rstrip()
            break
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        value = value[1:-1]
    return value


def _read_env_assignments() -> dict[str, str]:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return {}
    data: dict[str, str] = {}
    with env_path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            assignment = line.strip()
            if not assignment or assignment.startswith("#") or "=" not in assignment:
                continue
            if (
                assignment.startswith("export")
                and len(assignment) > len("export")
                and assignment[len("export")].isspace()
            ):
                assignment = assignment[len("export") :].lstrip()
            key, _, value = assignment.partition("=")
            key = key.strip().upper()
            if key in _LAUNCH_ENV_KEYS:
                data[key] = _parse_env_scalar(value)
    return data


def read_setup_complete() -> bool:
    configured = _read_env_assignments().get("SETUP_COMPLETE")
    if configured is None or configured.lower() in _ENV_FALSE_VALUES:
        return False
    if configured.lower() in _ENV_TRUE_VALUES:
        return True
    raise SystemExit("ERROR: SETUP_COMPLETE must be a valid boolean in .env")


def _parse_env_port(configured: str) -> int:
    if not _ENV_INTEGER_RE.fullmatch(configured):
        raise SystemExit("ERROR: PORT must be an integer from 1024 to 65535 in .env")
    integer_text = configured.replace("_", "").partition(".")[0]
    try:
        port = int(integer_text)
    except ValueError:
        raise SystemExit(
            "ERROR: PORT must be an integer from 1024 to 65535 in .env"
        ) from None
    if not (1024 <= port <= 65535):
        raise SystemExit("ERROR: PORT must be an integer from 1024 to 65535 in .env")
    return port


def read_port_host() -> tuple[str, int]:
    host = "127.0.0.1"
    port = 8787
    data = _read_env_assignments()
    configured_host = data.get("HOST")
    if configured_host is not None:
        if configured_host not in _LOOPBACK_HOSTS:
            raise SystemExit(
                "ERROR: HOST must be 127.0.0.1, localhost, or ::1 in .env"
            )
        host = configured_host
    configured_port = data.get("PORT")
    if configured_port is not None:
        port = _parse_env_port(configured_port)
    return host, port


def read_exchange_banner() -> str:
    if not (ROOT / ".env").is_file():
        return "exchange=?"
    data = _read_env_assignments()
    configured_exchange = data.get("EXCHANGE")
    ex = (
        "hyperliquid"
        if configured_exchange is None
        else configured_exchange.strip().lower()
    )
    if ex in ("hl", "hyperliquid"):
        network_value = data.get("HL_TESTNET", "true").lower()
        if network_value in _ENV_TRUE_VALUES:
            net = "TESTNET"
        elif network_value in _ENV_FALSE_VALUES:
            net = "MAINNET"
        else:
            return "Hyperliquid network=?"
        return f"Hyperliquid {net}"
    return "MEXC" if ex == "mexc" else "exchange=?"


def read_trading_banner() -> str:
    """Return an honest, secret-free trading state for launcher output."""
    configured = _read_env_assignments().get("TRADING_ENABLED")
    if configured is None or configured.lower() in _ENV_FALSE_VALUES:
        return "DISARMED (safe default)"
    if configured.lower() in _ENV_TRUE_VALUES:
        return "ARMED — order submission is enabled"
    return "UNKNOWN — invalid TRADING_ENABLED value"


def _port_in_use(host: str, port: int) -> bool:
    """W3-11: advisory pre-check — a closed/free port must never block a
    legitimate start. Only a clear 'something is listening' (connect
    succeeds) counts as in-use; anything inconclusive (DNS error, timeout,
    OS error) returns False so the normal startup proceeds."""
    check_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    try:
        with socket.create_connection((check_host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _local_url(host: str, port: int, path: str = "") -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}{path}"


def _port_arg(raw: str) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not (1024 <= port <= 65535):
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


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
        type=_port_arg,
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
    if host not in _LOOPBACK_HOSTS:
        print(f"WARNING: HOST={host} is not loopback — forcing 127.0.0.1")
        host = "127.0.0.1"

    # Open the browser at /setup while setup is incomplete, else the dashboard.
    setup_open = not read_setup_complete()
    url = _local_url(host, port, "/setup" if setup_open else "")
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
