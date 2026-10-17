"""Windows launcher ownership, established before importing the application.

The launcher alone retains a non-inheritable kill-on-close job handle. The
server opens the named job, joins itself and closes its temporary handle before
loading Uvicorn. This also works through Windows Venv Python redirectors without
giving them a handle that could keep the job alive after the launcher exits.

https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import sys
import threading
import uuid


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def _job_api():
    if sys.platform != "win32":
        raise OSError("Windows job objects are not available on this platform")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
        "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetProcessTimes": ([wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "WaitForMultipleObjects": ([wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD], wintypes.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _birth_time(api, handle) -> int:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def _owner_handles(api) -> list:
    """Pin the launcher wrappers and nearest calling shell against PID reuse.

    Windows Venv/py redirectors may survive an IDE shell's termination. Watch
    both those wrappers and the calling shell, using only process identities,
    never command lines, configuration, accounts or runtime data.
    """
    snapshot = api.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entries = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            entries[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    handles = []
    parent = os.getppid()
    child_birth = _birth_time(api, api.GetCurrentProcess())
    try:
        for _ in range(8):
            if not parent or parent not in entries:
                raise OSError("Start-window owner could not be identified")
            # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION; not inheritable.
            handle = api.OpenProcess(0x100000 | 0x1000, False, parent)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            birth = _birth_time(api, handle)
            if birth > child_birth or api.WaitForSingleObject(handle, 0) != 0x102:
                raise OSError("Start-window owner has exited or its PID was reused")
            parent, name = entries[parent]
            if name not in {"python.exe", "pythonw.exe", "py.exe", "pyw.exe"}:
                return handles
            child_birth = birth
        raise OSError("No unambiguous start-window owner")
    except BaseException:
        for handle in handles:
            api.CloseHandle(handle)
        raise


class _OwnerWatch:
    def __init__(self, api):
        self.api = api
        self.handles = _owner_handles(api)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        try:
            self.thread.start()
        except BaseException:
            for handle in self.handles:
                api.CloseHandle(handle)
            raise

    def _run(self):
        array = (wintypes.HANDLE * len(self.handles))(*self.handles)
        while not self.stop.is_set():
            result = self.api.WaitForMultipleObjects(len(array), array, False, 200)
            if self.stop.is_set():
                return
            if result != 0x102:  # owner ended, or monitoring failed: fail closed
                # This exits only THIS launcher. Kernel handle cleanup then
                # kills its own server job, including reload descendants.
                os._exit(130)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        if not self.thread.is_alive():
            for handle in self.handles:
                self.api.CloseHandle(handle)
            self.handles = []


class WindowsServerJob:
    """Own only this launcher's server tree, never the launcher or browser."""

    def __init__(self):
        self.api = _job_api()
        self.watch = None
        self.name = "Local\\ObsidianLiveTrader-" + uuid.uuid4().hex
        # NULL security attributes: the owning handle is NOT inheritable.
        ctypes.set_last_error(0)
        self.handle = self.api.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                raise OSError("Launcher job name already exists")
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.watch = _OwnerWatch(self.api)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None
        if self.watch is not None:
            self.watch.close()
            self.watch = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def join_launcher_job(name: str) -> None:
    """Join before any app import. A missing owner/job fails closed."""
    if not re.fullmatch(r"Local\\ObsidianLiveTrader-[0-9a-f]{32}", name):
        raise OSError("Invalid launcher job identity")
    api = _job_api()
    handle = api.OpenJobObjectW(0x0001, False, name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        # Only the launcher may retain a handle. If it already died, this last
        # close terminates us before Uvicorn or app.main can be imported.
        if not api.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) < 2:
            raise OSError("Launcher job required")
        join_launcher_job(args[0])
    except OSError:
        print("ERROR: Server could not bind to its launcher. No application was started.", file=sys.stderr)
        return 1
    from uvicorn.main import main as uvicorn_main

    uvicorn_main(args=args[1:], prog_name="uvicorn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
