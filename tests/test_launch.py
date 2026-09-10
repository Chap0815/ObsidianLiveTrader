"""Tests für scripts/launch.py (Task 46, W3-03/W3-05/W3-10/W3-11).

launch.py ist kein Package-Modul (kein scripts/__init__.py), daher laden wir es
per importlib aus dem Dateipfad statt `import scripts.launch`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCH_PATH = ROOT / "scripts" / "launch.py"


def _load_launch():
    spec = importlib.util.spec_from_file_location(
        "launch_module_under_test", LAUNCH_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def launch():
    # Import fresh each time in case a test mutates module-level globals
    # (ROOT/VENV_DIR) — avoids cross-test bleed.
    return _load_launch()


def test_version_gate_rejects_below_3_11_with_english_message(launch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        launch._check_python_version((3, 9, 0))
    assert excinfo.value.code != 0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "3.11" in combined
    assert "Python" in combined
    # English-language, actionable message
    assert "requires" in combined
    assert "python.org" in combined.lower()


def test_version_gate_accepts_3_11_and_newer(launch):
    # Should not raise for 3.11.0 or 3.12.5
    launch._check_python_version((3, 11, 0))
    launch._check_python_version((3, 12, 5))


def test_version_gate_runs_at_import_time_on_current_interpreter(launch):
    # The module already executed _check_python_version() at import (module
    # load succeeded above), which only happens if the current interpreter
    # (running the test suite) satisfies the gate.
    assert sys.version_info >= (3, 11)


def test_ensure_deps_prefers_requirements_lock_when_present(
    launch, tmp_path, monkeypatch
):
    """W3-05: requirements.lock must win over requirements.txt when both exist,
    and the pip install call must reference the lock file path."""
    root = tmp_path
    venv_dir = root / ".venv"
    venv_dir.mkdir()
    (root / "requirements.lock").write_text("fastapi==0.139.0\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi>=0.115,<0.140\n", encoding="utf-8")

    monkeypatch.setattr(launch, "ROOT", root)
    monkeypatch.setattr(launch, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launch, "_is_project_venv", lambda p: True)
    monkeypatch.setattr(launch, "_venv_env", lambda: {})

    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True, env=None, error_hint=None):
        calls.append([str(c) for c in cmd])
        return 0

    monkeypatch.setattr(launch, "_run", fake_run)

    vpy = venv_dir / "Scripts" / "python.exe"
    launch.ensure_deps(vpy, skip=False)

    install_calls = [c for c in calls if "install" in c and "-r" in c]
    assert install_calls, "expected a pip install -r <file> call"
    req_arg = install_calls[0][install_calls[0].index("-r") + 1]
    assert req_arg.endswith("requirements.lock")
    assert "requirements.txt" not in req_arg


def test_ensure_deps_falls_back_to_requirements_txt_without_lock(
    launch, tmp_path, monkeypatch
):
    root = tmp_path
    venv_dir = root / ".venv"
    venv_dir.mkdir()
    (root / "requirements.txt").write_text("fastapi>=0.115,<0.140\n", encoding="utf-8")

    monkeypatch.setattr(launch, "ROOT", root)
    monkeypatch.setattr(launch, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launch, "_is_project_venv", lambda p: True)
    monkeypatch.setattr(launch, "_venv_env", lambda: {})

    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True, env=None, error_hint=None):
        calls.append([str(c) for c in cmd])
        return 0

    monkeypatch.setattr(launch, "_run", fake_run)

    vpy = venv_dir / "Scripts" / "python.exe"
    launch.ensure_deps(vpy, skip=False)

    install_calls = [c for c in calls if "install" in c and "-r" in c]
    req_arg = install_calls[0][install_calls[0].index("-r") + 1]
    assert req_arg.endswith("requirements.txt")


def test_ensure_deps_marker_tracks_actual_file_used_switch_reinstalls(
    launch, tmp_path, monkeypatch
):
    """Switching lock<->txt must trigger a reinstall even if the marker is
    otherwise 'up to date' by mtime."""
    root = tmp_path
    venv_dir = root / ".venv"
    venv_dir.mkdir()
    txt = root / "requirements.txt"
    txt.write_text("fastapi>=0.115,<0.140\n", encoding="utf-8")

    monkeypatch.setattr(launch, "ROOT", root)
    monkeypatch.setattr(launch, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launch, "_is_project_venv", lambda p: True)
    monkeypatch.setattr(launch, "_venv_env", lambda: {})

    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True, env=None, error_hint=None):
        calls.append([str(c) for c in cmd])
        return 0

    monkeypatch.setattr(launch, "_run", fake_run)
    vpy = venv_dir / "Scripts" / "python.exe"

    # First install: only requirements.txt exists.
    launch.ensure_deps(vpy, skip=False)
    assert len(calls) == 2  # pip upgrade + install

    # Second call, nothing changed -> marker up to date -> no new calls.
    calls.clear()
    launch.ensure_deps(vpy, skip=False)
    assert calls == []

    # Now a requirements.lock appears (e.g. after `git pull`) -> must reinstall
    # even though marker mtime vs. requirements.txt would look "up to date".
    lock = root / "requirements.lock"
    lock.write_text("fastapi==0.139.0\n", encoding="utf-8")
    calls.clear()
    launch.ensure_deps(vpy, skip=False)
    assert len(calls) == 2
    install_calls = [c for c in calls if "install" in c and "-r" in c]
    req_arg = install_calls[0][install_calls[0].index("-r") + 1]
    assert req_arg.endswith("requirements.lock")


def test_ensure_deps_marker_detects_content_change_with_same_mtime(
    launch, tmp_path, monkeypatch
):
    root = tmp_path
    venv_dir = root / ".venv"
    venv_dir.mkdir()
    req = root / "requirements.lock"
    req.write_text("fastapi==0.139.0\n", encoding="utf-8")
    original_stat = req.stat()
    marker = venv_dir / ".deps_installed"

    monkeypatch.setattr(launch, "ROOT", root)
    monkeypatch.setattr(launch, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launch, "_is_project_venv", lambda p: True)
    monkeypatch.setattr(launch, "_venv_env", lambda: {})
    marker.write_text(launch._deps_marker_state(req), encoding="utf-8")

    req.write_text("fastapi==0.138.0\n", encoding="utf-8")
    os.utime(req, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True, env=None, error_hint=None):
        calls.append([str(c) for c in cmd])
        return 0

    monkeypatch.setattr(launch, "_run", fake_run)

    launch.ensure_deps(venv_dir / "Scripts" / "python.exe", skip=False)

    assert len(calls) == 2
    assert marker.read_text(encoding="utf-8") == launch._deps_marker_state(req)


def test_setup_flag_explicit_alias_not_prefix_abbreviation(launch):
    """--setup must be a real, explicit alias for --setup-cli (W3-10) — not
    just an argparse prefix-abbreviation match."""
    setup_actions = [
        a for a in launch._build_arg_parser()._actions if "setup_cli" == a.dest
    ]
    assert setup_actions, "expected an action with dest=setup_cli"
    action = setup_actions[0]
    assert "--setup" in action.option_strings
    assert "--setup-cli" in action.option_strings


def test_setup_flag_parses_both_forms(launch):
    p = launch._build_arg_parser()
    assert p.parse_args(["--setup"]).setup_cli is True
    assert p.parse_args(["--setup-cli"]).setup_cli is True
    assert p.parse_args([]).setup_cli is False


@pytest.mark.parametrize("value", ["0", "1023", "65536", "99999", "9" * 5000])
def test_port_flag_rejects_out_of_range_values(launch, value):
    with pytest.raises(SystemExit):
        launch._build_arg_parser().parse_args(["--port", value])


@pytest.mark.parametrize("value", ["1024", "8787", "65535"])
def test_port_flag_accepts_configured_range(launch, value):
    args = launch._build_arg_parser().parse_args(["--port", value])
    assert args.port == int(value)


@pytest.mark.parametrize(
    "value",
    ["", "not-a-port", "9000.1", "9e3", "0", "1023", "65536", "9" * 5000],
)
def test_read_port_host_rejects_invalid_explicit_port(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"HOST=127.0.0.1\nPORT={value}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="PORT must be an integer from 1024 to 65535"):
        launch.read_port_host()


@pytest.mark.parametrize("value", ["+9000", "9_000", "9000.0"])
def test_read_port_host_matches_settings_integer_forms(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"HOST=127.0.0.1\nPORT={value}\n",
        encoding="utf-8",
    )

    assert launch.read_port_host() == ("127.0.0.1", 9000)


def test_trading_banner_reports_armed_state(launch, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("TRADING_ENABLED=true\n", encoding="utf-8")
    assert launch.read_trading_banner().startswith("ARMED")


def test_trading_banner_defaults_to_disarmed(launch, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    assert launch.read_trading_banner().startswith("DISARMED")


@pytest.mark.parametrize("value", ["", "enabled", "2"])
def test_trading_banner_marks_invalid_explicit_values_unknown(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"TRADING_ENABLED={value}\n",
        encoding="utf-8",
    )

    assert launch.read_trading_banner() == "UNKNOWN — invalid TRADING_ENABLED value"


@pytest.mark.parametrize("value", ["y", "t"])
def test_setup_complete_matches_short_true_literals(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"SETUP_COMPLETE={value}\n",
        encoding="utf-8",
    )

    assert launch.read_setup_complete() is True


@pytest.mark.parametrize("value", ["", "completed", "2"])
def test_setup_complete_rejects_invalid_explicit_values(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"SETUP_COMPLETE={value}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="SETUP_COMPLETE must be a valid boolean"):
        launch.read_setup_complete()


@pytest.mark.parametrize("value", ["y", "t"])
def test_trading_banner_never_hides_short_true_literals(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"TRADING_ENABLED={value}\n",
        encoding="utf-8",
    )

    assert launch.read_trading_banner().startswith("ARMED")


def test_launcher_readers_support_export_prefixed_assignments(
    launch, tmp_path, monkeypatch
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "export SETUP_COMPLETE=true\n"
        "export HOST=::1\n"
        "export PORT=9000\n"
        "export EXCHANGE=hyperliquid\n"
        "export HL_TESTNET=false\n"
        "export TRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    assert launch.read_setup_complete() is True
    assert launch.read_port_host() == ("::1", 9000)
    assert launch.read_exchange_banner() == "Hyperliquid MAINNET"
    assert launch.read_trading_banner().startswith("ARMED")


def test_launcher_reader_supports_tab_after_export(launch, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "export\tTRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    assert launch.read_trading_banner().startswith("ARMED")


def test_launcher_reader_matches_case_insensitive_settings_keys(
    launch, tmp_path, monkeypatch
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "setup_complete=true\n"
        "Host=::1\n"
        "port=9000\n"
        "Exchange=HL\n"
        "hl_TestNet=false\n"
        "trading_enabled=true\n",
        encoding="utf-8",
    )

    assert launch.read_setup_complete() is True
    assert launch.read_port_host() == ("::1", 9000)
    assert launch.read_exchange_banner() == "Hyperliquid MAINNET"
    assert launch.read_trading_banner().startswith("ARMED")


def test_launcher_reader_does_not_hide_first_key_after_utf8_bom(
    launch, tmp_path, monkeypatch
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "\ufeffTRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    assert launch.read_trading_banner().startswith("ARMED")


@pytest.mark.parametrize("value", ["", "0.0.0.0", "LOCALHOST"])
def test_read_port_host_rejects_invalid_explicit_host(
    launch, tmp_path, monkeypatch, value
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"HOST={value}\nPORT=8787\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="HOST must be 127.0.0.1, localhost, or ::1"):
        launch.read_port_host()


def test_launcher_readers_use_last_duplicate_assignment(launch, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "SETUP_COMPLETE=false\nSETUP_COMPLETE=true\n"
        "HOST=127.0.0.1\nHOST=::1\n"
        "PORT=8787\nPORT=9000\n"
        "EXCHANGE=mexc\nEXCHANGE=hyperliquid\n"
        "HL_TESTNET=true\nHL_TESTNET=false\n"
        "TRADING_ENABLED=false\nTRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    assert launch.read_setup_complete() is True
    assert launch.read_port_host() == ("::1", 9000)
    assert launch.read_exchange_banner() == "Hyperliquid MAINNET"
    assert launch.read_trading_banner().startswith("ARMED")


def test_launcher_readers_match_quoted_commented_dotenv_scalars(
    launch, tmp_path, monkeypatch
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        'SETUP_COMPLETE="true" # completed\n'
        "HOST='::1'\n"
        'PORT="9000" # local port\n'
        "EXCHANGE='hyperliquid'\n"
        'HL_TESTNET="false" # selected network\n'
        "TRADING_ENABLED='true' # explicit opt-in\n",
        encoding="utf-8",
    )

    assert launch.read_setup_complete() is True
    assert launch.read_port_host() == ("::1", 9000)
    assert launch.read_exchange_banner() == "Hyperliquid MAINNET"
    assert launch.read_trading_banner().startswith("ARMED")


def test_launcher_env_reader_does_not_retain_secret_fields(launch, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=synthetic-key-material\nTRADING_ENABLED=false\n",
        encoding="utf-8",
    )

    assert launch._read_env_assignments() == {"TRADING_ENABLED": "false"}


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", "Hyperliquid TESTNET"),
        ("EXCHANGE=HL\n", "Hyperliquid TESTNET"),
        ("EXCHANGE=Hyperliquid\n", "Hyperliquid TESTNET"),
        ("EXCHANGE=\n", "exchange=?"),
        ("EXCHANGE=unknown\n", "exchange=?"),
    ],
)
def test_exchange_banner_matches_settings_exchange_semantics(
    launch, tmp_path, monkeypatch, content, expected
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(content, encoding="utf-8")

    assert launch.read_exchange_banner() == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("yes", "Hyperliquid TESTNET"),
        ("t", "Hyperliquid TESTNET"),
        ("0", "Hyperliquid MAINNET"),
        ("n", "Hyperliquid MAINNET"),
        ("invalid", "Hyperliquid network=?"),
    ],
)
def test_exchange_banner_does_not_mislabel_hyperliquid_network(
    launch, tmp_path, monkeypatch, value, expected
):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"EXCHANGE=hyperliquid\nHL_TESTNET={value}\n",
        encoding="utf-8",
    )

    assert launch.read_exchange_banner() == expected


def test_port_in_use_detects_listening_socket(launch):
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert launch._port_in_use("127.0.0.1", port) is True
    finally:
        srv.close()


def test_port_in_use_false_when_port_free(launch):
    import socket

    # Find a free port, then close it immediately so it's very likely free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert launch._port_in_use("127.0.0.1", port) is False


def test_port_in_use_uses_address_family_aware_connection(launch, monkeypatch):
    calls = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_create_connection(address, timeout):
        calls.append((address, timeout))
        return FakeConnection()

    monkeypatch.setattr(launch.socket, "create_connection", fake_create_connection)

    assert launch._port_in_use("::1", 8787) is True
    assert calls == [(('::1', 8787), 0.5)]


@pytest.mark.parametrize(
    ("host", "path", "expected"),
    [
        ("127.0.0.1", "/setup", "http://127.0.0.1:8787/setup"),
        ("localhost", "", "http://localhost:8787"),
        ("::1", "/setup", "http://[::1]:8787/setup"),
    ],
)
def test_local_url_matches_bound_loopback_host(launch, host, path, expected):
    assert launch._local_url(host, 8787, path) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell launcher is Windows-only")
def test_powershell_launcher_uses_existing_project_venv_without_python_on_path():
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    env = os.environ.copy()
    env["PATH"] = str(powershell.parent)

    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "start.ps1"),
            "--help",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()
