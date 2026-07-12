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
