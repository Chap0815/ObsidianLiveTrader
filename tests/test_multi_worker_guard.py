"""F-16 (deployment/concurrency): the in-process preview-token store and
trade_lock require a single uvicorn worker. Common multi-worker environment
settings and the runtime instance lock both fail startup closed.
"""

from app.main import _detect_multi_worker_env


def test_no_warning_when_no_worker_env_vars_set():
    assert _detect_multi_worker_env({}) is None


def test_no_warning_for_single_worker():
    assert _detect_multi_worker_env({"WEB_CONCURRENCY": "1"}) is None
    assert _detect_multi_worker_env({"UVICORN_WORKERS": "1"}) is None


def test_warns_for_web_concurrency_above_one():
    msg = _detect_multi_worker_env({"WEB_CONCURRENCY": "4"})
    assert msg is not None
    assert "WEB_CONCURRENCY=4" in msg
    assert "single" in msg.lower() or "SINGLE" in msg


def test_warns_for_uvicorn_workers_above_one():
    msg = _detect_multi_worker_env({"UVICORN_WORKERS": "2"})
    assert msg is not None
    assert "UVICORN_WORKERS=2" in msg


def test_ignores_unparseable_value_without_raising():
    # Must never crash a normal single-worker launch on a weird env value.
    assert _detect_multi_worker_env({"WEB_CONCURRENCY": "not-a-number"}) is None


def test_never_raises_and_defaults_to_real_os_environ(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    assert _detect_multi_worker_env() is None
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    assert _detect_multi_worker_env() is not None


def test_declared_multi_worker_lifespan_fails_before_resource_setup(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    from app.config import get_settings

    create_client = main.create_exchange_client
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr(
        main,
        "create_exchange_client",
        lambda settings: (_ for _ in ()).throw(
            AssertionError("exchange client must not be constructed")
        ),
    )
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="SINGLE-WORKER REQUIRED"):
            with TestClient(main.app):
                pass
    finally:
        get_settings.cache_clear()
        monkeypatch.setattr(main, "create_exchange_client", create_client)


# ── Q-02: exclusive file/PID lock on data/ (runtime detection, independent of
# the env-var heuristic above — catches a bare `--workers N` / `gunicorn -w
# N` launch that sets none of those vars). ─────────────────────────────────

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.main import (
    _acquire_instance_lock,
    _parse_lock_content,
    _pid_is_alive,
    _process_start_time,
    _release_instance_lock,
    _windows_open_failure_may_be_live,
)


_LOCK_WORKER = r"""
import sys
from pathlib import Path
from app.main import _acquire_instance_lock, _release_instance_lock

data_dir = Path(sys.argv[1])
print("READY", flush=True)
if sys.stdin.readline().strip() != "GO":
    raise SystemExit(2)
lock_path = _acquire_instance_lock(data_dir)
print("ACQUIRED" if lock_path is not None else "BUSY", flush=True)
if lock_path is not None:
    sys.stdin.readline()
    _release_instance_lock(lock_path)
"""


def _lock_worker(data_dir: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _LOCK_WORKER, str(data_dir)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY"
    return proc


def _worker_command(proc: subprocess.Popen[str], command: str) -> str:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(command + "\n")
    proc.stdin.flush()
    return proc.stdout.readline().strip()


def _finish_worker(proc: subprocess.Popen[str], *, release: bool) -> None:
    if release:
        assert proc.stdin is not None
        proc.stdin.write("RELEASE\n")
        proc.stdin.flush()
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"stdout={stdout!r} stderr={stderr!r}"


def test_oversized_legacy_metadata_fails_closed(tmp_path):
    oversized_pid = 10**100
    assert _pid_is_alive(oversized_pid) is True
    assert _process_start_time(oversized_pid) is None

    data_dir = tmp_path / "oversized_pid"
    data_dir.mkdir()
    lock_path = data_dir / "instance.lock"
    original = f"{oversized_pid}:1"
    lock_path.write_text(original)

    assert _acquire_instance_lock(data_dir) is None
    assert lock_path.read_text() == original


def test_live_legacy_owner_without_kernel_lock_remains_busy(tmp_path):
    """An app started before the upgrade owns only its unversioned PID file."""
    data_dir = tmp_path / "live_legacy"
    data_dir.mkdir()
    lock_path = data_dir / "instance.lock"
    current_start = _process_start_time(os.getpid())
    legacy_marker = (
        str(os.getpid())
        if current_start is None
        else f"{os.getpid()}:{current_start}"
    )
    lock_path.write_text(legacy_marker)

    assert _acquire_instance_lock(data_dir) is None
    assert lock_path.read_text() == legacy_marker


@pytest.mark.parametrize(
    "legacy_marker", ["", "not-a-pid", "0", "999999999:not-a-time"]
)
def test_ambiguous_legacy_marker_fails_closed_without_rewrite(
    tmp_path, legacy_marker
):
    data_dir = tmp_path / "ambiguous_legacy"
    data_dir.mkdir()
    lock_path = data_dir / "instance.lock"
    lock_path.write_text(legacy_marker)

    assert _acquire_instance_lock(data_dir) is None
    assert lock_path.read_text() == legacy_marker


@pytest.mark.parametrize(
    ("error_code", "may_be_live"),
    [
        (5, True),  # ERROR_ACCESS_DENIED: the process can exist but be protected
        (8, True),  # unrelated resource failure is likewise inconclusive
        (87, False),  # ERROR_INVALID_PARAMETER: invalid/nonexistent PID
    ],
)
def test_windows_open_failure_only_proves_dead_for_invalid_pid(
    error_code, may_be_live
):
    assert _windows_open_failure_may_be_live(error_code) is may_be_live


def test_file_lock_detects_second_instance(tmp_path, monkeypatch, caplog):
    """A kernel lock, not stale PID text, is the startup authority."""
    from app.config import get_settings
    from app.main import app
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "db_kernel_lock"
    data_dir.mkdir()
    lock_path = data_dir / "instance.lock"

    holder = _lock_worker(data_dir)
    try:
        assert _worker_command(holder, "GO") == "ACQUIRED"
        monkeypatch.setenv("DATABASE_PATH", str(data_dir / "trader.db"))
        monkeypatch.setenv("TRADING_ENABLED", "false")
        get_settings.cache_clear()
        with caplog.at_level(logging.WARNING, logger="app.main"):
            with pytest.raises(RuntimeError, match="Refusing to start"):
                with TestClient(app):
                    pass
        assert any("MULTI-INSTANCE DETECTED" in r.message for r in caplog.records)
    finally:
        get_settings.cache_clear()
        _finish_worker(holder, release=True)

    # Release retains one stable path; the next owner locks that same inode.
    assert lock_path.exists()
    reacquired = _acquire_instance_lock(data_dir)
    assert reacquired == lock_path.resolve()
    _release_instance_lock(reacquired)


# ── Q-02: retained kernel lock + diagnostic metadata ────────────────────────
# Versioned PID/start-time text is diagnostic; the open descriptor's OS lock is
# the cross-process authority. Live or ambiguous unversioned legacy markers stay
# fail-closed because an app started before this protocol owns no kernel lock.


def test_instance_lock_second_acquire_in_same_process_is_busy(tmp_path):
    """Two app lifespans in one process still represent two live instances."""
    data_dir = tmp_path / "same_process"
    data_dir.mkdir()
    lock_path = _acquire_instance_lock(data_dir)
    assert lock_path is not None

    try:
        assert _acquire_instance_lock(data_dir) is None
    finally:
        _release_instance_lock(lock_path)
    assert lock_path.exists()
    written = lock_path.read_text()
    assert written.startswith("oslock-v1:")
    written_pid, _written_start = _parse_lock_content(written)
    assert written_pid == os.getpid()

    # The prior versioned marker still names this live PID, but its released
    # kernel lock (not PID text) permits a new owner.
    reacquired = _acquire_instance_lock(data_dir)
    assert reacquired == lock_path
    _release_instance_lock(reacquired)


def test_stale_metadata_concurrent_reclaim_has_exactly_one_owner(tmp_path):
    """Two reclaimers contend on one persistent inode; neither may unlink it."""
    data_dir = tmp_path / "stale_contention"
    data_dir.mkdir()
    lock_path = data_dir / "instance.lock"
    lock_path.write_text("999999999:1.0")
    workers = [_lock_worker(data_dir), _lock_worker(data_dir)]
    statuses: list[str] = []
    try:
        # Release both subprocesses from their barrier before reading either
        # result. The winner retains its kernel lock until explicit cleanup.
        for proc in workers:
            assert proc.stdin is not None
            proc.stdin.write("GO\n")
            proc.stdin.flush()
        for proc in workers:
            assert proc.stdout is not None
            statuses.append(proc.stdout.readline().strip())
    finally:
        for index, proc in enumerate(workers):
            _finish_worker(
                proc,
                release=index < len(statuses) and statuses[index] == "ACQUIRED",
            )

    assert sorted(statuses) == ["ACQUIRED", "BUSY"]
    assert lock_path.exists()
