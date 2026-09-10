"""First-run setup: marker-based gating + lock-once-complete guard.

The env-content builder itself is covered by test_env_builder.py; this file
covers the /setup + /api/setup wiring and the SETUP_COMPLETE marker.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.env_builder import build_full_env, normalize_answers


def _payload(**kw):
    base = dict(
        exchange="hl-testnet",
        hl_private_key="0x" + "a" * 64,
        hl_account_address="",
        llm_provider="claude",
        llm_api_key="sk-ant-test",
        risk_profile="conservative",
        port=8787,
    )
    base.update(kw)
    return base


def test_full_env_roundtrips_through_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("EXCHANGE", raising=False)
    monkeypatch.delenv("HL_TESTNET", raising=False)
    content = build_full_env(normalize_answers(_payload()))
    assert "EXCHANGE=hyperliquid" in content
    assert "HL_TESTNET=true" in content
    assert "TRADING_ENABLED=false" in content
    assert "SETUP_COMPLETE=true" in content
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.exchange == "hyperliquid"
    assert s.hl_testnet is True
    assert s.trading_enabled is False
    assert s.setup_complete is True


def test_bootstrap_env_does_not_overwrite_file_created_during_publish(
    tmp_path, monkeypatch
):
    import scripts.write_bootstrap_env as bootstrap

    target = tmp_path / ".env"
    sentinel = "SETUP_COMPLETE=true\nSENTINEL=keep\n"
    real_link = bootstrap.os.link

    def racing_link(source, destination):
        target.write_text(sentinel, encoding="utf-8")
        return real_link(source, destination)

    monkeypatch.setattr(bootstrap.os, "link", racing_link)

    assert bootstrap.write_bootstrap_env(target, port=8787) is False
    assert target.read_text(encoding="utf-8") == sentinel
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_bootstrap_env_publish_failure_leaves_no_partial_target(tmp_path, monkeypatch):
    import scripts.write_bootstrap_env as bootstrap

    target = tmp_path / ".env"

    def fail_publish(source, destination):
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(bootstrap.os, "link", fail_publish)

    with pytest.raises(OSError, match="synthetic publish failure"):
        bootstrap.write_bootstrap_env(target, port=8787)

    assert not target.exists()
    assert list(tmp_path.glob(".env.*.tmp")) == []


@pytest.mark.parametrize("port", [0, 1023, 65536, 99999])
def test_bootstrap_env_rejects_out_of_range_port_before_create(tmp_path, port):
    import scripts.write_bootstrap_env as bootstrap

    target = tmp_path / ".env"

    with pytest.raises(bootstrap.argparse.ArgumentTypeError, match="between"):
        bootstrap.write_bootstrap_env(target, port=port)

    assert not target.exists()
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_marker_transitions(tmp_path):
    """_setup_needed keys on SETUP_COMPLETE, not mere existence."""
    incomplete = tmp_path / ".env.a"
    incomplete.write_text("SETUP_COMPLETE=false\nHOST=127.0.0.1\n", encoding="utf-8")
    assert Settings(_env_file=str(incomplete)).setup_complete is False

    complete = tmp_path / ".env.b"
    complete.write_text("SETUP_COMPLETE=true\nHOST=127.0.0.1\n", encoding="utf-8")
    assert Settings(_env_file=str(complete)).setup_complete is True


def test_settings_reads_first_key_after_utf8_bom(tmp_path):
    complete = tmp_path / ".env"
    complete.write_text("\ufeffSETUP_COMPLETE=true\n", encoding="utf-8")

    assert Settings(_env_file=str(complete)).setup_complete is True


@pytest.mark.parametrize(
    "settings_kwargs",
    [
        {"exchange": "mexc", "mexc_api_key": "synthetic-mexc-key"},
        {"exchange": "mexc", "mexc_api_secret": "synthetic-mexc-secret"},
        {"exchange": "mexc", "hl_private_key": "synthetic-hl-key"},
        {"anthropic_api_key": "synthetic-anthropic-key"},
        {"xai_api_key": "synthetic-xai-key"},
        {"openai_api_key": "synthetic-openai-key"},
    ],
)
def test_setup_locks_when_any_sensitive_credential_exists(
    tmp_path, monkeypatch, settings_kwargs
):
    import app.main as main

    env = tmp_path / ".env"
    env.write_text("SETUP_COMPLETE=false\n", encoding="utf-8")
    settings = Settings(_env_file=None, setup_complete=False, **settings_kwargs)
    monkeypatch.setattr(main, "ENV_PATH", env)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    assert main._setup_needed() is False


@pytest.mark.parametrize(
    "env_content",
    [
        (
            "SETUP_COMPLETE=false\n"
            "ANTHROPIC_API_KEY=\n"
            "export CLAUDE_API_KEY=synthetic-legacy-key\n"
        ),
        (
            "SETUP_COMPLETE=false\n"
            "xai_api_key=synthetic-stale-key\n"
            "XAI_API_KEY=\n"
        ),
        "\ufeffOPENAI_API_KEY=synthetic-bom-key\nSETUP_COMPLETE=false\n",
    ],
)
def test_setup_file_scan_locks_on_hidden_sensitive_definition(
    tmp_path, monkeypatch, env_content
):
    import app.main as main

    env = tmp_path / ".env"
    env.write_text(env_content, encoding="utf-8")
    settings = Settings(_env_file=None, setup_complete=False)
    monkeypatch.setattr(main, "ENV_PATH", env)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    assert main._setup_needed() is False


def test_bootstrap_local_token_alone_keeps_setup_open(tmp_path, monkeypatch):
    import app.main as main

    env = tmp_path / ".env"
    env.write_text("SETUP_COMPLETE=false\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        setup_complete=False,
        local_api_token="synthetic-local-token",
    )
    monkeypatch.setattr(main, "ENV_PATH", env)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    assert main._setup_needed() is True


def test_setup_locked_when_setup_complete(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main

    # Exercise the lock with a synthetic file, never a developer's real .env.
    env = tmp_path / ".env"
    original = "SETUP_COMPLETE=true\nTRADING_ENABLED=false\nHOST=127.0.0.1\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(main, "ENV_PATH", env)
    settings = Settings(_env_file=str(env))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app) as c:
        r = c.get("/setup", follow_redirects=False)
        assert r.status_code == 303  # locked → back to dashboard
        r2 = c.post("/api/setup", json=_payload())
        assert r2.status_code == 403  # cannot overwrite existing config
    assert env.read_text(encoding="utf-8") == original


def test_setup_overflowed_custom_risk_returns_400_before_create(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    import app.main as main

    env = tmp_path / ".env"
    payload = _payload(
        risk_profile="custom",
        max_risk_pct=10 ** 1_000,
        max_leverage=10,
        min_rrr=2.0,
        max_notional_pct_of_equity=1000,
        max_notional_usdt=500,
    )

    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "ENV_PATH", env)
        monkeypatch.setattr(main, "_setup_needed", lambda: True)
        create_client = MagicMock(side_effect=AssertionError("must not construct"))
        monkeypatch.setattr(main, "create_exchange_client", create_client)

        response = client.post("/api/setup", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "MAX_RISK_PCT is not a number"
    create_client.assert_not_called()
    assert not env.exists()


@pytest.mark.asyncio
async def test_setup_hot_apply_invalidates_exchange_dependent_caches(
    tmp_path, monkeypatch
):
    import app.env_builder as env_builder
    import app.main as main

    env = tmp_path / ".env"
    monkeypatch.setattr(main, "ENV_PATH", env)
    monkeypatch.setattr(main, "_setup_needed", lambda: True)
    monkeypatch.setattr(env_builder, "restrict_env_permissions", lambda path: None)

    settings = SimpleNamespace(exchange="hyperliquid", llm_provider="claude")
    settings_getter = MagicMock(return_value=settings)
    settings_getter.cache_clear = MagicMock()
    monkeypatch.setattr(main, "get_settings", settings_getter)

    old_client = SimpleNamespace(aclose=AsyncMock())
    new_client = object()
    monkeypatch.setattr(main, "create_exchange_client", lambda _: new_client)
    state = SimpleNamespace(
        mexc=old_client,
        exchange=old_client,
        _owned_exchange_client=old_client,
        symbols_cache=(1.0, ["OLD_USDT"]),
        market_cache={"old": "market"},
        mini_cache={"old": "mini"},
        analyze_cache={"old": "analysis"},
        tm_atr_cache={"old": "atr"},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    response = await main.setup_save(request, _payload())

    assert response["ok"] is True
    assert state.mexc is new_client
    assert state.exchange is new_client
    assert state.symbols_cache is None
    assert state.market_cache == {}
    assert state.mini_cache == {}
    assert state.analyze_cache == {}
    assert state.tm_atr_cache == {}
    old_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_client_creation_failure_does_not_commit_configuration(
    tmp_path, monkeypatch
):
    import app.env_builder as env_builder
    import app.main as main

    env = tmp_path / ".env"
    monkeypatch.setattr(main, "ENV_PATH", env)
    monkeypatch.setattr(main, "_setup_needed", lambda: True)
    monkeypatch.setattr(env_builder, "restrict_env_permissions", lambda path: None)

    settings = SimpleNamespace(exchange="hyperliquid", llm_provider="claude")
    settings_getter = MagicMock(return_value=settings)
    settings_getter.cache_clear = MagicMock()
    monkeypatch.setattr(main, "get_settings", settings_getter)
    monkeypatch.setattr(
        main,
        "create_exchange_client",
        MagicMock(side_effect=RuntimeError("client initialization failed")),
    )

    old_client = SimpleNamespace(aclose=AsyncMock())
    state = SimpleNamespace(mexc=old_client, exchange=old_client)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    with pytest.raises(RuntimeError, match="client initialization failed"):
        await main.setup_save(request, _payload())

    assert not env.exists()
    assert list(tmp_path.glob(".env.*.tmp")) == []
    assert state.mexc is old_client
    assert state.exchange is old_client
    old_client.aclose.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_replace_failure_closes_uninstalled_client(tmp_path, monkeypatch):
    import app.env_builder as env_builder
    import app.main as main

    env = tmp_path / ".env"
    monkeypatch.setattr(main, "ENV_PATH", env)
    monkeypatch.setattr(main, "_setup_needed", lambda: True)
    monkeypatch.setattr(env_builder, "restrict_env_permissions", lambda path: None)
    monkeypatch.setattr(
        env_builder,
        "replace_with_retry",
        MagicMock(side_effect=PermissionError("destination locked")),
    )

    settings_getter = MagicMock()
    settings_getter.cache_clear = MagicMock()
    monkeypatch.setattr(main, "get_settings", settings_getter)
    candidate = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(main, "create_exchange_client", lambda _: candidate)
    old_client = SimpleNamespace(aclose=AsyncMock())
    state = SimpleNamespace(mexc=old_client, exchange=old_client)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    with pytest.raises(main.HTTPException) as exc:
        await main.setup_save(request, _payload())

    assert exc.value.status_code == 409
    assert not env.exists()
    assert list(tmp_path.glob(".env.*.tmp")) == []
    candidate.aclose.assert_awaited_once()
    assert state.mexc is old_client
    assert state.exchange is old_client


# ── Finding 3: os.replace transient Windows lock -> 409, not opaque 500 ─────
def test_setup_save_permission_error_maps_to_409(tmp_path, monkeypatch):
    """A transient Windows file lock on the final os.replace (OneDrive/AV/an
    open editor briefly holding a handle) must not surface as an opaque
    500 — the client gets a clear 409 to retry, and the tmp file (which
    already carried the new secrets) is cleaned up, never left behind."""
    from fastapi.testclient import TestClient

    import app.env_builder as env_builder
    import app.main as main

    env = tmp_path / ".env"  # does not exist -> _setup_needed() is True
    monkeypatch.setattr(main, "ENV_PATH", env)

    def always_fail(src, dst):
        raise PermissionError("WinError 5: Zugriff verweigert")

    monkeypatch.setattr(env_builder.os, "replace", always_fail)
    monkeypatch.setattr(env_builder.time, "sleep", lambda s: None)  # no real delay in tests

    with TestClient(main.app) as c:
        r = c.post("/api/setup", json=_payload())

    assert r.status_code == 409
    assert "locked" in r.json()["detail"]
    assert not env.exists()  # fail-safe: never created
    assert list(tmp_path.glob(".env.*.tmp")) == []  # tmp cleaned up
