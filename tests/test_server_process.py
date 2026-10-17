"""Launcher ownership with synthetic workers only; never starts the trading app."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import app.server_process as guard

ROOT = Path(__file__).resolve().parents[1]


def _launch_module():
    spec = importlib.util.spec_from_file_location("owned_launch_test", ROOT / "scripts/launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_api(monkeypatch):
    closed, assigned, opened = [], [], []
    api = SimpleNamespace(
        CreateJobObjectW=lambda security, name: 10,
        SetInformationJobObject=lambda *args: True,
        OpenJobObjectW=lambda *args: opened.append(args) or 11,
        AssignProcessToJobObject=lambda *args: assigned.append(args) or True,
        GetCurrentProcess=lambda: 99,
        CloseHandle=lambda handle: closed.append(handle) or True,
    )
    monkeypatch.setattr(guard, "_job_api", lambda: api)
    monkeypatch.setattr(guard, "_OwnerWatch", lambda api: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: None, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic failure"), raising=False)
    return api, closed, assigned, opened


def test_job_owner_is_not_a_member_and_handle_is_not_inherited(fake_api):
    api, closed, assigned, _ = fake_api
    observed = []

    def limits(handle, kind, pointer, size):
        info = ctypes.cast(pointer, ctypes.POINTER(guard._ExtendedLimits)).contents
        observed.append((handle, kind, info.BasicLimitInformation.LimitFlags, size))
        return True

    api.SetInformationJobObject = limits
    with guard.WindowsServerJob():
        assert not assigned
    assert observed == [(10, 9, 0x2000, ctypes.sizeof(guard._ExtendedLimits))]
    assert closed == [10]


def test_server_joins_and_releases_its_only_temporary_handle(fake_api):
    _, closed, assigned, opened = fake_api
    name = "Local\\ObsidianLiveTrader-" + "a" * 32
    guard.join_launcher_job(name)
    assert opened == [(1, False, name)]
    assert assigned == [(11, 99)]
    assert closed == [11]


def test_job_configuration_failure_releases_handle(fake_api):
    api, closed, _, _ = fake_api
    api.SetInformationJobObject = lambda *args: False
    with pytest.raises(OSError):
        guard.WindowsServerJob()
    assert closed == [10]


def test_server_assignment_failure_releases_handle(fake_api):
    api, closed, _, _ = fake_api
    api.AssignProcessToJobObject = lambda *args: False
    with pytest.raises(OSError):
        guard.join_launcher_job("Local\\ObsidianLiveTrader-" + "b" * 32)
    assert closed == [11]


def test_owner_monitor_failure_cannot_leave_an_unowned_job(fake_api, monkeypatch):
    _, closed, _, _ = fake_api

    def unavailable(api):
        raise OSError("synthetic owner unavailable")

    monkeypatch.setattr(guard, "_OwnerWatch", unavailable)
    with pytest.raises(OSError):
        guard.WindowsServerJob()
    assert closed == [10]


@pytest.mark.parametrize(("birth", "state", "valid"), [(1001, 258, False), (999, 0, False), (999, 258, True)])
def test_owner_identity_pins_handles_and_rejects_pid_reuse(monkeypatch, birth, state, valid):
    closed = []

    def first(snapshot, pointer):
        entry = ctypes.cast(pointer, ctypes.POINTER(guard._ProcessEntry)).contents
        entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile = 123, 0, "cmd.exe"
        return True

    api = SimpleNamespace(
        CreateToolhelp32Snapshot=lambda *args: 1,
        Process32FirstW=first, Process32NextW=lambda *args: False,
        CloseHandle=lambda handle: closed.append(handle) or True,
        GetCurrentProcess=lambda: 99, OpenProcess=lambda *args: 33,
        WaitForSingleObject=lambda *args: state,
    )
    monkeypatch.setattr(guard, "os", SimpleNamespace(getppid=lambda: 123))
    monkeypatch.setattr(guard, "_birth_time", lambda api, handle: 1000 if handle == 99 else birth)
    if valid:
        assert guard._owner_handles(api) == [33]
        assert closed == [1]  # owner handle stays pinned until monitoring ends
        api.CloseHandle(33)
    else:
        with pytest.raises(OSError):
            guard._owner_handles(api)
    assert closed == [1, 33]


@pytest.mark.parametrize("result", [0, 0xFFFFFFFF])
def test_owner_exit_or_monitor_failure_exits_only_the_launcher(monkeypatch, result):
    watcher = guard._OwnerWatch.__new__(guard._OwnerWatch)
    watcher.handles, watcher.stop = [33], threading.Event()
    watcher.api = SimpleNamespace(WaitForMultipleObjects=lambda *args: result)

    def exit_current(code):
        raise SystemExit(code)

    monkeypatch.setattr(guard, "os", SimpleNamespace(_exit=exit_current))
    with pytest.raises(SystemExit) as exc:
        watcher._run()
    assert exc.value.code == 130


@pytest.mark.parametrize("args", [[], ["invalid", "app.main:app"]])
def test_invalid_bootstrap_fails_before_uvicorn_import(monkeypatch, args, capsys):
    import builtins

    original = builtins.__import__
    imports = []

    def record(name, *a, **kw):
        imports.append(name)
        return original(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", record)
    assert guard.main(args) == 1
    assert "uvicorn.main" not in imports
    assert "No application was started" in capsys.readouterr().err


@pytest.mark.parametrize("outcome", [0, "interrupt", "timeout", "spawn-failure"])
def test_launcher_owns_job_for_all_exit_paths(monkeypatch, outcome):
    launch = _launch_module()
    monkeypatch.setattr(launch, "os", SimpleNamespace(name="nt"))
    closed, spawned, waits = [], [], []
    monkeypatch.setattr(guard, "WindowsServerJob", lambda: SimpleNamespace(name="synthetic-job", close=lambda: closed.append(True)))

    class Process:
        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is None and outcome != 0:
                raise KeyboardInterrupt
            if outcome == "timeout" and timeout == 10:
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return 0

        def poll(self):
            return 0

    def popen(cmd, **kwargs):
        spawned.append((cmd, kwargs))
        if outcome == "spawn-failure":
            raise OSError("synthetic spawn error")
        return Process()

    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    cmd = ["synthetic-python", "-m", "uvicorn", "app.main:app", "--reload"]
    if outcome == "spawn-failure":
        with pytest.raises(OSError):
            launch._run_server(cmd, {"SYNTHETIC": "1"})
    else:
        assert launch._run_server(cmd, {"SYNTHETIC": "1"}) == (130 if outcome == "timeout" else 0)
    assert closed == [True]
    assert spawned[0][0] == ["synthetic-python", "-m", "app.server_process", "synthetic-job", "app.main:app", "--reload"]
    assert spawned[0][1]["close_fds"] is True
    assert spawned[0][1]["env"] == {"SYNTHETIC": "1"}


def test_launcher_cannot_start_if_job_setup_fails(monkeypatch):
    launch = _launch_module()
    monkeypatch.setattr(launch, "os", SimpleNamespace(name="nt"))

    def fail():
        raise OSError("synthetic ownership failure")

    monkeypatch.setattr(guard, "WindowsServerJob", fail)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **kw: pytest.fail("Unprotected spawn"))
    assert launch._run_server(["python", "-m", "uvicorn", "synthetic:app"], {}) == 1


def test_existing_server_is_not_adopted_by_a_second_launcher(monkeypatch, capsys):
    launch = _launch_module()
    monkeypatch.setattr(launch.sys, "argv", ["launch", "--skip-setup", "--skip-install", "--no-browser"])
    monkeypatch.setattr(launch, "ensure_venv", lambda path: path)
    monkeypatch.setattr(launch, "ensure_deps", lambda *a, **kw: None)
    monkeypatch.setattr(launch, "read_port_host", lambda: ("127.0.0.1", 8787))
    monkeypatch.setattr(launch, "read_setup_complete", lambda: True)
    monkeypatch.setattr(launch, "read_exchange_banner", lambda: "synthetic")
    monkeypatch.setattr(launch, "read_trading_banner", lambda: "DISARMED")
    monkeypatch.setattr(launch, "_port_in_use", lambda *a: True)
    monkeypatch.setattr(launch, "_app_is_running", lambda *a: True)
    monkeypatch.setattr(launch, "_run_server", lambda *a: pytest.fail("Existing server must not be adopted"))
    with pytest.raises(SystemExit) as exc:
        launch.main()
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "NOT owned by this window" in output
    assert "Closing THIS server's start window" not in output


def _line_json(process):
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    line = ready.get(timeout=20)
    if not line:
        process.wait(timeout=5)
        raise AssertionError("Synthetic worker exited before readiness: " + process.stderr.read())
    return json.loads(line)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows job ownership")
@pytest.mark.parametrize("end", ["normal", "launcher", "redirector", "shell"])
def test_native_owner_exit_reaps_server_and_grandchildren(tmp_path, end):
    """Real Venv redirectors and native jobs; fake Uvicorn, no app import/API."""
    api = guard._job_api()
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    handles = []
    processes = []

    def pin(pid):
        handle = api.OpenProcess(0x100000 | 0x0001, False, pid)
        assert handle
        handles.append(handle)
        return handle

    fake = tmp_path / "uvicorn"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "main.py").write_text(
        "import json, os, subprocess, sys, threading\n"
        "def main(args, prog_name):\n"
        "    child = subprocess.Popen([sys.executable, '-u', '-c', "
        "\"import os, threading; print(os.getpid(), flush=True); threading.Event().wait()\"], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)\n"
        "    pid = int(child.stdout.readline())\n"
        "    print(json.dumps({'server': os.getpid(), 'server_redirector': os.getppid(), "
        "'grandchild': pid, 'grandchild_redirector': child.pid}), flush=True)\n"
        "    if args[-1] == 'normal': sys.stdin.buffer.read(1)\n"
        "    else: threading.Event().wait()\n",
        encoding="utf-8",
    )
    driver = tmp_path / "launcher_driver.py"
    driver.write_text(
        "import importlib.util, json, os, sys\n"
        "from app.server_process import join_launcher_job\n"
        "join_launcher_job(sys.argv[1])\n"
        "print(json.dumps({'launcher': os.getpid()}), flush=True)\n"
        f"spec=importlib.util.spec_from_file_location('launcher', {str(ROOT / 'scripts/launch.py')!r})\n"
        "launch=importlib.util.module_from_spec(spec); spec.loader.exec_module(launch)\n"
        f"sys.exit(launch._run_server([sys.executable, '-m', 'uvicorn', 'synthetic:app', {end!r}], os.environ.copy()))\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(tmp_path), str(ROOT))))
    # An independent outer job bounds every test worker even if an assertion
    # fails. The production launcher creates its own nested server-only job.
    with guard.WindowsServerJob() as outer:
        command = [sys.executable, "-u", str(driver), outer.name]
        if end == "shell":
            shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            env.update(SYNTHETIC_OWNER_PYTHON=sys.executable, SYNTHETIC_OWNER_SCRIPT=str(driver), SYNTHETIC_OWNER_JOB=outer.name)
            command = [str(shell), "-NoProfile", "-NonInteractive", "-Command",
                       "& $env:SYNTHETIC_OWNER_PYTHON -u $env:SYNTHETIC_OWNER_SCRIPT $env:SYNTHETIC_OWNER_JOB"]
        process = subprocess.Popen(command, cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        processes.append(process)
        unrelated_job = None
        try:
            launcher_handle = pin(_line_json(process)["launcher"])
            identities = _line_json(process)
            server_handle = pin(identities["server"])
            grandchild_handle = pin(identities["grandchild"])
            redirector_handles = [pin(identities[key]) for key in ("server_redirector", "grandchild_redirector")]
            unrelated_handle = None
            if end == "launcher":
                # Independent safety job, NOT this launcher's server job.
                # It must survive the launcher exit: never kill Python by name.
                unrelated_job = guard.WindowsServerJob()
                sibling = subprocess.Popen(
                    [sys.executable, "-u", "-c",
                     "import os, sys, threading; from app.server_process import join_launcher_job; "
                     "join_launcher_job(sys.argv[1]); print(os.getpid(), flush=True); threading.Event().wait()",
                     unrelated_job.name],
                    cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                processes.append(sibling)
                unrelated_handle = pin(_line_json(sibling))
            if end == "normal":
                process.stdin.write("x")
                process.stdin.flush()
                assert process.wait(timeout=15) == 0, process.stderr.read()
            elif end == "launcher":
                assert api.TerminateProcess(launcher_handle, 77)
            else:
                assert api.TerminateProcess(pin(process.pid), 77)
            for handle in (launcher_handle, server_handle, grandchild_handle, *redirector_handles):
                assert api.WaitForSingleObject(handle, 15000) == 0, (end, identities)
            if unrelated_handle:
                assert api.WaitForSingleObject(unrelated_handle, 0) == 0x102
        finally:
            if unrelated_job is not None:
                unrelated_job.close()
            outer.close()
            for created in processes:
                if created.poll() is None:
                    created.kill()
                created.wait(timeout=10)
                for stream in (created.stdin, created.stdout, created.stderr):
                    if stream is not None:
                        stream.close()
            for handle in handles:
                api.CloseHandle(handle)
