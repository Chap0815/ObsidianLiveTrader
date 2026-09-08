"""F-16 (deployment/concurrency): the in-process preview-token store and
trade_lock require a single uvicorn worker. This app has no way to see a
bare `uvicorn ... --workers N` CLI flag, but it CAN detect common
multi-worker env vars some process managers set and warn loudly instead of
silently corrupting preview tokens / the confirm/close lock across workers.
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


# ── Q-02: exclusive file/PID lock on data/ (runtime detection, independent of
# the env-var heuristic above — catches a bare `--workers N` / `gunicorn -w
# N` launch that sets none of those vars). ─────────────────────────────────

import logging
import os
import subprocess
import sys

import pytest

from app.main import (
    _acquire_instance_lock,
    _parse_lock_content,
    _pid_is_alive,
    _process_start_time,
    _release_instance_lock,
)


def _dead_pid() -> int:
    """A PID guaranteed to be dead: spawn a trivial child and wait for exit."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def test_file_lock_detects_second_instance(tmp_path, monkeypatch, caplog):
    """Unit-level: the lockfile primitives at the heart of Q-02.

    - A fresh acquire creates a PID lockfile and succeeds.
    - A second acquire while a LIVE foreign PID holds it is busy (None) and
      must not disturb the other side's lock content.
    - A lock left behind by a crashed process (dead PID) is stale and must
      be silently reclaimed instead of blocking startup forever.
    - Release only ever removes a lock THIS process actually owns.
    """
    # Fresh acquire. B3-01: the lockfile now stores "pid:start_time" (or bare
    # "pid" if the start time could not be determined) — parse instead of a
    # literal string match against the new format.
    own_dir = tmp_path / "own"
    own_dir.mkdir()
    lock_path = _acquire_instance_lock(own_dir)
    assert lock_path is not None
    written_pid, _written_start = _parse_lock_content(lock_path.read_text())
    assert written_pid == os.getpid()
    assert _pid_is_alive(os.getpid()) is True

    # Busy: a live foreign PID (the test-runner's parent process) holds it.
    busy_dir = tmp_path / "busy"
    busy_dir.mkdir()
    busy_lock = busy_dir / "instance.lock"
    other_live_pid = os.getppid()
    busy_lock.write_text(str(other_live_pid))
    assert _acquire_instance_lock(busy_dir) is None
    assert busy_lock.read_text().strip() == str(other_live_pid)  # untouched

    # Stale: a dead PID must be reclaimed, not block startup.
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    dead_pid = _dead_pid()
    (stale_dir / "instance.lock").write_text(str(dead_pid))
    assert _pid_is_alive(dead_pid) is False
    reclaimed = _acquire_instance_lock(stale_dir)
    assert reclaimed is not None
    reclaimed_pid, _reclaimed_start = _parse_lock_content(reclaimed.read_text())
    assert reclaimed_pid == os.getpid()

    # Release: only our own lock is removed; a foreign one is left alone.
    _release_instance_lock(lock_path)
    assert not lock_path.exists()
    foreign_lock = tmp_path / "foreign" / "instance.lock"
    foreign_lock.parent.mkdir()
    foreign_lock.write_text(str(other_live_pid))
    _release_instance_lock(foreign_lock)
    assert foreign_lock.exists()

    # Integration (lifespan): a second instance whose startup lock is busy
    # (a live foreign PID already holds it) logs a loud WARNING; when this
    # instance is ARMED (TRADING_ENABLED=true) it must abort startup
    # entirely instead of quietly running two copies of the in-process
    # preview_store/trade_lock state — an unarmed instance only warns.
    from app.config import get_settings
    from fastapi.testclient import TestClient
    from app.main import app

    data_dir = tmp_path / "db"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "instance.lock").write_text(str(other_live_pid))
    monkeypatch.setenv("DATABASE_PATH", str(data_dir / "trader.db"))

    # Unarmed: warns, still starts.
    monkeypatch.setenv("TRADING_ENABLED", "false")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(app):
            pass
    assert any("MULTI-INSTANCE DETECTED" in r.message for r in caplog.records)

    # Armed: fail-closed abort.
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("LOCAL_API_TOKEN", "test-token-1234567890")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Refusing to start"):
        with TestClient(app):
            pass

    get_settings.cache_clear()


# ── B3-01: PID + process start time in the lockfile ─────────────────────────
# An alive PID alone is not proof the ORIGINAL lock-owning process is still
# running — the OS can reassign a dead process's PID to an unrelated new
# process (PID reuse/wraparound), which would otherwise make a stale lock
# look permanently busy and block every future ARMED start (a false-positive
# DoS). Storing + comparing the process START TIME lets a reclaim tell a
# genuinely-busy PID apart from a reused one.


def test_stale_pid_reuse_reclaimed_via_start_time_mismatch(tmp_path):
    """Same (alive) PID, but the recorded start time does NOT match the
    current process running under that PID -> the original owner is gone
    and the PID was reissued -> stale, must be reclaimed (not treated as
    busy). Contrast: the SAME alive PID with the CORRECT start time must
    still be treated as genuinely busy (Q-02 fail-closed must not be
    weakened by this change)."""
    other_live_pid = os.getppid()  # guaranteed alive for the whole test

    # Reused-PID case: deliberately WRONG start time for this alive PID.
    reuse_dir = tmp_path / "reuse"
    reuse_dir.mkdir()
    (reuse_dir / "instance.lock").write_text(f"{other_live_pid}:1.0")
    reclaimed = _acquire_instance_lock(reuse_dir)
    assert reclaimed is not None
    reclaimed_pid, _ts = _parse_lock_content(reclaimed.read_text())
    assert reclaimed_pid == os.getpid()

    # Contrast: genuinely busy — same alive PID AND matching start time must
    # still block (fail-closed preserved). Skips if this platform/permission
    # cannot resolve a start time at all (inconclusive, not a regression).
    real_start = _process_start_time(other_live_pid)
    if real_start is not None:
        busy_dir = tmp_path / "busy_matching_start"
        busy_dir.mkdir()
        (busy_dir / "instance.lock").write_text(f"{other_live_pid}:{real_start}")
        assert _acquire_instance_lock(busy_dir) is None

    # Backward tolerance: an OLD-format lockfile (bare PID, no start time) on
    # an alive foreign PID is still treated as busy (today's PID-only path),
    # not a crash and not a reclaim.
    old_format_dir = tmp_path / "old_format_busy"
    old_format_dir.mkdir()
    (old_format_dir / "instance.lock").write_text(str(other_live_pid))
    assert _acquire_instance_lock(old_format_dir) is None
