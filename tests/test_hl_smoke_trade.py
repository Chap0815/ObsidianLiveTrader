"""Offline safety checks for the manual Hyperliquid Testnet smoke tool."""

import asyncio
from types import SimpleNamespace

import pytest

import scripts.hl_smoke_trade as hl_smoke_trade

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


def _settings(**overrides):
    values = {
        "exchange": "hyperliquid",
        "hl_testnet": True,
        "hl_private_key": "synthetic-private-key",
        "hl_ready": True,
        "trading_enabled": True,
        "database_path": "synthetic/trader.db",
        "default_symbol": "BTC_USDT",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _CleanLiveClient:
    base_url = TESTNET_URL
    testnet = True

    def __init__(self):
        self.closed = False

    async def account_snapshot(self):
        return {"equity_usdt": 100.0, "available_usdt": 100.0}

    async def positions(self, symbol, *, fresh=False):
        return []

    async def open_stop_orders(self, symbol):
        return []

    async def open_orders(self, symbol):
        return []

    async def contract_meta(self, symbol):
        return SimpleNamespace(
            symbol=symbol,
            contract_size=1.0,
            vol_unit=0.001,
            min_vol=0.001,
            max_leverage=20,
            price_unit=0.1,
        )

    async def ticker(self, symbol):
        return SimpleNamespace(symbol=symbol, last_price=50_000.0)

    async def aclose(self):
        self.closed = True


def test_cli_requires_exact_testnet_mutation_confirmation():
    for argv in (["--confirm"], ["--confirm", "testnet"], ["--confirm", "LIVE"]):
        with pytest.raises(SystemExit) as exc:
            hl_smoke_trade._parse_arguments(argv)

        assert exc.value.code == 2

    arguments = hl_smoke_trade._parse_arguments(["--confirm", "TESTNET"])
    assert arguments.confirm == "TESTNET"


def test_cli_rejects_conflicting_account_modes():
    with pytest.raises(SystemExit) as exc:
        hl_smoke_trade._parse_arguments(["--with-account", "--confirm", "TESTNET"])

    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_run_rejects_conflicting_account_modes_before_settings(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        hl_smoke_trade,
        "get_settings",
        lambda: (_ for _ in ()).throw(
            AssertionError("conflicting modes must fail before settings access")
        ),
    )

    assert await hl_smoke_trade.run(live=True, with_account=True) == 2
    assert "choose either --with-account or --confirm TESTNET" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_live_probe_refuses_busy_app_instance_before_client(monkeypatch, capsys):
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: None
    )

    def forbidden_client(*args, **kwargs):
        raise AssertionError("Hyperliquid client must not be constructed")

    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", forbidden_client)

    assert await hl_smoke_trade.run(live=True) == 2
    assert "another app/probe process" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_live_probe_lock_error_does_not_print_local_path(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_DATABASE_PATH"
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)

    def fail_lock(_database_path):
        raise OSError(marker)

    monkeypatch.setattr(hl_smoke_trade, "_acquire_live_probe_lock", fail_lock)

    def forbidden_client(*args, **kwargs):
        raise AssertionError("Hyperliquid client must not be constructed")

    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", forbidden_client)

    assert await hl_smoke_trade.run(live=True) == 2
    output = capsys.readouterr().out
    assert "cannot acquire the app instance lock" in output
    assert "OSError" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_probe_rejects_invalid_default_symbol_before_lock_or_client(
    monkeypatch, capsys
):
    marker = "SYNTHETIC_PRIVATE_SYMBOL/USDT"
    monkeypatch.setattr(
        hl_smoke_trade,
        "get_settings",
        lambda: _settings(default_symbol=marker),
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_acquire_live_probe_lock",
        lambda database_path: (_ for _ in ()).throw(
            AssertionError("invalid symbol must block before the instance lock")
        ),
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "create_exchange_client",
        lambda settings: (_ for _ in ()).throw(
            AssertionError("invalid symbol must block before client creation")
        ),
    )

    assert await hl_smoke_trade.run(live=True) == 2
    output = capsys.readouterr().out
    assert "DEFAULT_SYMBOL is invalid" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_live_probe_preserves_preexisting_selected_coin_exposure(
    monkeypatch, capsys
):
    lock = object()
    released = []

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self):
            self.cancelled = []
            self.closed = False

        async def account_snapshot(self):
            return {"equity_usdt": 100.0, "available_usdt": 100.0}

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": 0.25}]

        async def open_stop_orders(self, symbol):
            return [{"orderId": 71}]

        async def open_orders(self, symbol):
            return []

        async def cancel_order(self, body):
            self.cancelled.append(body)

        async def aclose(self):
            self.closed = True

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def close_position(self, **kwargs):
            raise AssertionError("pre-existing position must not be closed")

        async def confirm(self, token):
            raise AssertionError("no order may be confirmed on a dirty baseline")

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda settings: client)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    assert await hl_smoke_trade.run(live=True) == 2
    output = capsys.readouterr().out
    assert "preserve the existing position/orders" in output
    assert client.cancelled == []
    assert client.closed is True
    assert released == [lock]


@pytest.mark.asyncio
async def test_open_order_ids_rejects_identity_overlap_across_exchange_sources():
    class Client:
        async def open_stop_orders(self, symbol):
            return [{"orderId": 11}]

        async def open_orders(self, symbol):
            return [{"orderId": 11}]

    with pytest.raises(hl_smoke_trade.HyperliquidError):
        await hl_smoke_trade._open_order_ids(Client(), "BTC")


@pytest.mark.asyncio
async def test_cleanup_cancels_only_ids_created_by_probe():
    class Client:
        def __init__(self):
            self.open_ids = {11, 22}

        async def positions(self, symbol, *, fresh=False):
            return []

        async def open_stop_orders(self, symbol):
            return [{"orderId": order_id} for order_id in sorted(self.open_ids)]

        async def open_orders(self, symbol):
            return []

        async def cancel_order(self, body):
            raise AssertionError("cleanup cancellation must use OrderService")

    class Service:
        def __init__(self, client):
            self.client = client
            self.cancelled = []

        async def close_position(self, **kwargs):
            raise AssertionError("cleanup must not close an unowned position")

        async def cancel(self, *, order_id, symbol):
            self.cancelled.append((order_id, symbol))
            self.client.open_ids.remove(order_id)
            return {"ok": True}

    client = Client()
    service = Service(client)
    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        client,
        service,
        "BTC",
        "long",
        report,
        position_may_be_owned=False,
        owned_order_ids={11},
    )

    assert service.cancelled == [(11, "BTC")]
    assert client.open_ids == {22}
    assert any(
        tag == "FAIL" and name == "cleanup: unrelated orders preserved"
        for tag, name, _ in report.rows
    )


@pytest.mark.asyncio
async def test_cleanup_final_proof_rereads_position_after_order_cancellation():
    class Client:
        def __init__(self):
            self.open_ids = {11}
            self.position_reads = 0

        async def positions(self, symbol, *, fresh=False):
            self.position_reads += 1
            assert fresh is True
            if self.position_reads == 1:
                return []
            return [{"holdVol": 0.25}]

        async def open_stop_orders(self, symbol):
            return [{"orderId": order_id} for order_id in self.open_ids]

        async def open_orders(self, symbol):
            return []

    class Service:
        def __init__(self, client):
            self.client = client

        async def close_position(self, **kwargs):
            raise AssertionError("the probe did not create a position")

        async def cancel(self, *, order_id, symbol):
            self.client.open_ids.remove(order_id)
            return {"ok": True}

    client = Client()
    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        client,
        Service(client),
        "BTC",
        "long",
        report,
        position_may_be_owned=False,
        owned_order_ids={11},
    )

    assert client.position_reads == 2
    assert any(
        tag == "FAIL" and name == "cleanup: created exposure removed"
        for tag, name, _ in report.rows
    )


@pytest.mark.asyncio
async def test_cleanup_does_not_close_position_without_probe_ownership():
    class Client:
        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": 0.5}]

        async def open_stop_orders(self, symbol):
            return []

        async def open_orders(self, symbol):
            return []

        async def cancel_order(self, body):
            raise AssertionError("there are no owned orders")

    class Service:
        async def close_position(self, **kwargs):
            raise AssertionError("unowned position must be preserved")

    report = hl_smoke_trade.Report()
    await hl_smoke_trade._cleanup(
        Client(),
        Service(),
        "BTC",
        "long",
        report,
        position_may_be_owned=False,
        owned_order_ids=set(),
    )

    assert any(
        tag == "FAIL" and name == "cleanup: created exposure removed"
        for tag, name, _ in report.rows
    )


@pytest.mark.asyncio
async def test_cleanup_never_closes_more_than_probe_requested_volume():
    class Client:
        def __init__(self):
            self.position = 0.75

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": self.position}]

        async def open_stop_orders(self, symbol):
            return []

        async def open_orders(self, symbol):
            return []

    class Service:
        def __init__(self, client):
            self.client = client
            self.close_calls = []

        async def close_position(self, **kwargs):
            self.close_calls.append(kwargs)
            self.client.position -= kwargs["vol"]
            return {"ok": True, "verified": True, "status": "closed"}

    client = Client()
    service = Service(client)
    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        client,
        service,
        "BTC",
        "long",
        report,
        position_may_be_owned=True,
        owned_position_vol=0.25,
        owned_order_ids=set(),
    )

    assert service.close_calls == [
        {"symbol": "BTC", "side": "long", "vol": 0.25}
    ]
    assert client.position == pytest.approx(0.5)
    assert any(
        tag == "FAIL" and name == "cleanup: created exposure removed"
        for tag, name, _ in report.rows
    )


@pytest.mark.asyncio
async def test_cleanup_closes_only_remaining_probe_volume_after_partial_reduction():
    class Client:
        def __init__(self):
            self.position = 0.1

        async def positions(self, symbol, *, fresh=False):
            return [] if self.position <= 1e-9 else [{"holdVol": self.position}]

        async def open_stop_orders(self, symbol):
            return []

        async def open_orders(self, symbol):
            return []

    class Service:
        def __init__(self, client):
            self.client = client
            self.close_calls = []

        async def close_position(self, **kwargs):
            self.close_calls.append(kwargs)
            self.client.position -= kwargs["vol"]
            return {"ok": True, "verified": True, "status": "closed"}

    client = Client()
    service = Service(client)
    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        client,
        service,
        "BTC",
        "long",
        report,
        position_may_be_owned=True,
        owned_position_vol=0.25,
        owned_order_ids=set(),
    )

    assert service.close_calls == [
        {"symbol": "BTC", "side": "long", "vol": 0.1}
    ]
    assert client.position == pytest.approx(0.0)
    assert any(
        tag == "PASS" and name == "cleanup: created exposure removed"
        for tag, name, _ in report.rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owned_position_vol", [None, float("nan"), 0.0, -0.25]
)
async def test_cleanup_blocks_position_close_without_valid_owned_volume(
    owned_position_vol,
):
    class Client:
        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": 0.25}]

        async def open_stop_orders(self, symbol):
            return []

        async def open_orders(self, symbol):
            return []

    class Service:
        async def close_position(self, **kwargs):
            raise AssertionError("cleanup must have a valid ownership bound")

    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        Client(),
        Service(),
        "BTC",
        "long",
        report,
        position_may_be_owned=True,
        owned_position_vol=owned_position_vol,
        owned_order_ids=set(),
    )

    assert any(
        tag == "FAIL"
        and name == "cleanup: close created position"
        and detail == "HyperliquidError; provider details suppressed"
        for tag, name, detail in report.rows
    )


@pytest.mark.asyncio
async def test_cleanup_cancellation_keeps_protection_and_still_verifies():
    class Client:
        def __init__(self):
            self.open_ids = {11}
            self.position_reads = 0

        async def positions(self, symbol, *, fresh=False):
            self.position_reads += 1
            return [{"holdVol": 0.25}]

        async def open_stop_orders(self, symbol):
            return [{"orderId": order_id} for order_id in self.open_ids]

        async def open_orders(self, symbol):
            return []

    class Service:
        def __init__(self, client):
            self.client = client
            self.cancelled = []

        async def close_position(self, **kwargs):
            raise asyncio.CancelledError

        async def cancel(self, *, order_id, symbol):
            self.cancelled.append((order_id, symbol))
            self.client.open_ids.remove(order_id)
            return {"ok": True}

    client = Client()
    service = Service(client)
    report = hl_smoke_trade.Report()

    with pytest.raises(asyncio.CancelledError):
        await hl_smoke_trade._cleanup(
            client,
            service,
            "BTC",
            "long",
            report,
            position_may_be_owned=True,
            owned_position_vol=0.25,
            owned_order_ids={11},
        )

    assert service.cancelled == []
    assert client.open_ids == {11}
    assert client.position_reads == 3
    assert any(
        tag == "FAIL" and name == "cleanup: close created position"
        for tag, name, _ in report.rows
    )
    assert any(
        tag == "FAIL"
        and name == "cleanup: cancel created orders"
        and "protective orders kept" in detail
        for tag, name, detail in report.rows
    )


@pytest.mark.asyncio
async def test_cleanup_unverified_close_keeps_protective_orders():
    class Client:
        def __init__(self):
            self.open_ids = {11}

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": 0.25}]

        async def open_stop_orders(self, symbol):
            return [{"orderId": order_id} for order_id in self.open_ids]

        async def open_orders(self, symbol):
            return []

    class Service:
        def __init__(self):
            self.cancelled = []

        async def close_position(self, **kwargs):
            return {"ok": False, "verified": False, "status": "partial"}

        async def cancel(self, *, order_id, symbol):
            self.cancelled.append((order_id, symbol))
            raise AssertionError("a residual position still needs its protection")

    client = Client()
    service = Service()
    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        client,
        service,
        "BTC",
        "long",
        report,
        position_may_be_owned=True,
        owned_position_vol=0.25,
        owned_order_ids={11},
    )

    assert service.cancelled == []
    assert client.open_ids == {11}
    assert any(
        tag == "FAIL"
        and name == "cleanup: cancel created orders"
        and "protective orders kept" in detail
        for tag, name, detail in report.rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_volume", [False, True, "0", "0.25"])
async def test_cleanup_rejects_coercible_position_volume_and_keeps_protection(
    raw_volume,
):
    class Client:
        def __init__(self):
            self.open_ids = {11}

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": raw_volume}]

        async def open_stop_orders(self, symbol):
            return [{"orderId": order_id} for order_id in self.open_ids]

        async def open_orders(self, symbol):
            return []

    class Service:
        def __init__(self):
            self.cancelled = []

        async def cancel(self, *, order_id, symbol):
            self.cancelled.append((order_id, symbol))
            raise AssertionError("unverified position evidence must keep protection")

    client = Client()
    service = Service()
    report = hl_smoke_trade.Report()

    await hl_smoke_trade._cleanup(
        client,
        service,
        "BTC",
        "long",
        report,
        position_may_be_owned=False,
        owned_order_ids={11},
    )

    assert service.cancelled == []
    assert client.open_ids == {11}
    assert any(
        tag == "FAIL"
        and name == "cleanup: cancel created orders"
        and detail == "HyperliquidError; provider details suppressed"
        for tag, name, detail in report.rows
    )


def test_smoke_exception_detail_never_reflects_provider_message():
    marker = "SYNTHETIC_SECRET_MARKER"

    detail = hl_smoke_trade._safe_exception_detail(
        hl_smoke_trade.HyperliquidError(marker)
    )

    assert detail == "HyperliquidError; provider details suppressed"
    assert marker not in detail


@pytest.mark.asyncio
async def test_position_abs_requires_a_fresh_account_read():
    calls = []

    class Client:
        async def positions(self, symbol, *, fresh=False):
            calls.append((symbol, fresh))
            return []

    assert await hl_smoke_trade._position_abs(Client(), "BTC") == 0.0
    assert calls == [("BTC", True)]


@pytest.mark.asyncio
async def test_default_probe_strips_credentials_and_skips_private_paths(
    monkeypatch, capsys
):
    class PublicClient:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self):
            self.closed = False

        async def account_snapshot(self):
            raise AssertionError("default probe must not read the account")

        async def contract_meta(self, symbol):
            assert symbol == "BTC"
            return SimpleNamespace(symbol=symbol)

        async def ticker(self, symbol):
            assert symbol == "BTC"
            return SimpleNamespace(symbol=symbol, last_price=50_000.0)

        async def aclose(self):
            self.closed = True

    client = PublicClient()
    monkeypatch.setattr(
        hl_smoke_trade,
        "get_settings",
        lambda: _settings(hl_account_address="synthetic-account"),
    )

    def create_public_client(settings):
        assert settings.hl_private_key == ""
        assert settings.hl_account_address == ""
        return client

    monkeypatch.setattr(
        hl_smoke_trade, "create_exchange_client", create_public_client
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "OrderService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("default probe must not construct the money path")
        ),
    )

    assert await hl_smoke_trade.run(live=False) == 0
    assert client.closed is True
    output = capsys.readouterr().out
    assert "PUBLIC PREFLIGHT" in output
    assert "no account read or order preview" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "testnet"),
    [
        ("https://api.hyperliquid.xyz", True),
        (TESTNET_URL, False),
        ("https://SYNTHETIC_SECRET_MARKER@api.hyperliquid-testnet.xyz", True),
    ],
)
async def test_probe_rejects_non_testnet_client_before_any_exchange_read(
    monkeypatch, capsys, base_url, testnet
):
    class Client:
        def __init__(self):
            self.base_url = base_url
            self.testnet = testnet
            self.closed = False

        async def contract_meta(self, symbol):
            raise AssertionError("client target must be checked before market reads")

        async def ticker(self, symbol):
            raise AssertionError("client target must be checked before market reads")

        async def account_snapshot(self):
            raise AssertionError("client target must be checked before account reads")

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)

    assert await hl_smoke_trade.run(live=False) == 2
    assert client.closed is True
    output = capsys.readouterr().out
    assert "client targets Hyperliquid Testnet" in output
    assert "SYNTHETIC_SECRET_MARKER" not in output


@pytest.mark.asyncio
async def test_account_preview_requires_explicit_credentials_before_client(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        hl_smoke_trade,
        "get_settings",
        lambda: _settings(hl_private_key="", hl_ready=False),
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "create_exchange_client",
        lambda settings: (_ for _ in ()).throw(
            AssertionError("client must not be constructed without credentials")
        ),
    )

    assert await hl_smoke_trade.run(live=False, with_account=True) == 2
    assert "--with-account needs a valid HL_PRIVATE_KEY" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "with_account", "mode"),
    [(False, True, "--with-account"), (True, False, "--confirm TESTNET")],
)
async def test_account_modes_require_hl_ready_before_lock_or_client(
    monkeypatch, capsys, live, with_account, mode
):
    monkeypatch.setattr(
        hl_smoke_trade,
        "get_settings",
        lambda: _settings(
            hl_private_key="truthy-but-invalid",
            hl_account_address="invalid-account",
            hl_ready=False,
        ),
    )

    def forbidden_lock(*args, **kwargs):
        raise AssertionError("invalid account identity must fail before the lock")

    def forbidden_client(*args, **kwargs):
        raise AssertionError("invalid account identity must fail before the client")

    monkeypatch.setattr(hl_smoke_trade, "_acquire_live_probe_lock", forbidden_lock)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", forbidden_client)

    assert await hl_smoke_trade.run(live=live, with_account=with_account) == 2
    output = capsys.readouterr().out
    assert f"{mode} needs a valid HL_PRIVATE_KEY" in output
    assert "valid HL_ACCOUNT_ADDRESS" in output


@pytest.mark.asyncio
async def test_dry_run_asserts_limit_gate_without_confirming_any_order(monkeypatch):
    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self):
            self.closed = False

        async def account_snapshot(self):
            return {"equity_usdt": 100.0, "available_usdt": 100.0}

        async def contract_meta(self, symbol):
            return SimpleNamespace(
                symbol=symbol,
                contract_size=1.0,
                vol_unit=0.001,
                min_vol=0.001,
                max_leverage=20,
                price_unit=0.1,
            )

        async def ticker(self, symbol):
            return SimpleNamespace(symbol=symbol, last_price=50_000.0)

        async def aclose(self):
            self.closed = True

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def preview(self, ticket):
            if ticket.order_type == "limit":
                return {
                    "ok": False,
                    "errors": [
                        "Hyperliquid limit entries are disabled: synthetic gate"
                    ],
                }
            return {"ok": True, "token": "synthetic-preview"}

        async def confirm(self, token):
            raise AssertionError("dry-run must never confirm an order")

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda settings: client)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    def forbidden_database(*args, **kwargs):
        raise AssertionError("dry-run must not open the audit database")

    monkeypatch.setattr(hl_smoke_trade, "Database", forbidden_database)

    assert await hl_smoke_trade.run(live=False, with_account=True) == 0
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {"equity_usdt": "SYNTHETIC_SECRET_MARKER", "available_usdt": 100.0},
        {"equity_usdt": True, "available_usdt": 100.0},
        {"equity_usdt": float("nan"), "available_usdt": 100.0},
        {"equity_usdt": 100.0, "available_usdt": "SYNTHETIC_SECRET_MARKER"},
        {"equity_usdt": 100.0, "available_usdt": False},
        {"equity_usdt": 100.0, "available_usdt": -1.0},
        {"equity_usdt": 100.0, "available_usdt": 10**10_000},
        {"equity_usdt": 100.0},
    ],
)
async def test_account_probe_rejects_invalid_private_balances_without_echo(
    monkeypatch, capsys, snapshot
):
    class Client(_CleanLiveClient):
        async def account_snapshot(self):
            return snapshot

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(
        hl_smoke_trade,
        "OrderService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid balances must block the money path")
        ),
    )

    assert await hl_smoke_trade.run(live=False, with_account=True) == 1
    assert client.closed is True
    output = capsys.readouterr().out
    assert "HyperliquidError; provider details suppressed" in output
    assert "SYNTHETIC_SECRET_MARKER" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("last_price", 0.0),
        ("contract_size", 0.0),
        ("vol_unit", 0.0),
        ("min_vol", float("nan")),
        ("price_unit", -0.1),
        ("max_leverage", True),
    ],
)
async def test_account_probe_rejects_invalid_sizing_before_money_path(
    monkeypatch, capsys, field, value
):
    class Client(_CleanLiveClient):
        async def contract_meta(self, symbol):
            values = {
                "contract_size": 1.0,
                "vol_unit": 0.001,
                "min_vol": 0.001,
                "max_leverage": 20,
                "price_unit": 0.0,
            }
            if field != "last_price":
                values[field] = value
            return SimpleNamespace(symbol=symbol, **values)

        async def ticker(self, symbol):
            return SimpleNamespace(
                symbol=symbol,
                last_price=value if field == "last_price" else 50_000.0
            )

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(
        hl_smoke_trade,
        "OrderService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid sizing must block the money path")
        ),
    )

    assert await hl_smoke_trade.run(live=False, with_account=True) == 1
    assert client.closed is True
    assert "Testnet sizing metadata is invalid" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_source", ["contract", "ticker"])
async def test_account_probe_rejects_cross_symbol_market_data_before_money_path(
    monkeypatch, capsys, mismatched_source
):
    marker = "SYNTHETIC_PRIVATE_WRONG_SYMBOL"

    class Client(_CleanLiveClient):
        async def contract_meta(self, symbol):
            meta = await super().contract_meta(symbol)
            if mismatched_source == "contract":
                meta.symbol = marker
            return meta

        async def ticker(self, symbol):
            ticker = await super().ticker(symbol)
            if mismatched_source == "ticker":
                ticker.symbol = marker
            return ticker

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(
        hl_smoke_trade,
        "OrderService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cross-symbol market data must block the money path")
        ),
    )

    assert await hl_smoke_trade.run(live=False, with_account=True) == 1
    assert client.closed is True
    output = capsys.readouterr().out
    assert "HyperliquidError; provider details suppressed" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_account_probe_refuses_rounded_notional_above_hard_limit(
    monkeypatch, capsys
):
    class Client(_CleanLiveClient):
        async def contract_meta(self, symbol):
            return SimpleNamespace(
                symbol=symbol,
                contract_size=1.0,
                vol_unit=0.01,
                min_vol=0.01,
                max_leverage=20,
                price_unit=0.1,
            )

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(
        hl_smoke_trade,
        "OrderService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("oversized probe must block before the money path")
        ),
    )

    assert await hl_smoke_trade.run(live=False, with_account=True) == 2
    assert client.closed is True
    output = capsys.readouterr().out
    assert "hard limit of 50.00 USDC" in output
    assert "rounded notional=500.00 USDC" in output


@pytest.mark.asyncio
async def test_live_probe_binds_initialized_audit_db_before_preview(monkeypatch):
    lock = object()
    released = []
    events = []
    databases = []

    class AuditDatabase:
        def __init__(self, path):
            assert path == "synthetic/trader.db"
            databases.append(self)

        async def init(self):
            events.append("db_init")

        async def open(self):
            events.append("db_open")

        async def close(self):
            events.append("db_close")

    class Service:
        def __init__(self, client, settings, store, *, db):
            assert db is databases[0]
            assert events == ["db_init", "db_open"]
            events.append("service")

        async def preview(self, ticket):
            events.append("preview")
            raise hl_smoke_trade.OrderError("synthetic preview stop")

    client = _CleanLiveClient()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", AuditDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    assert await hl_smoke_trade.run(live=True) == 1

    assert events == ["db_init", "db_open", "service", "preview", "db_close"]
    assert client.closed is True
    assert released == [lock]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_state", ["position", "order"])
async def test_live_probe_rechecks_clean_baseline_after_preview_before_confirm(
    monkeypatch, capsys, foreign_state
):
    lock = object()
    released = []
    confirm_calls = []
    close_calls = []
    cancel_calls = []

    class Client(_CleanLiveClient):
        def __init__(self):
            super().__init__()
            self.position = 0.0
            self.open_ids = set()

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": self.position}] if self.position else []

        async def open_orders(self, symbol):
            return [{"orderId": order_id} for order_id in sorted(self.open_ids)]

    class AuditDatabase:
        def __init__(self, path):
            pass

        async def init(self):
            pass

        async def open(self):
            pass

        async def close(self):
            pass

    class Service:
        def __init__(self, client, settings, store, *, db):
            self.client = client

        async def preview(self, ticket):
            if foreign_state == "position":
                self.client.position = 0.25
            else:
                self.client.open_ids = {77}
            return {"ok": True, "token": "synthetic-preview"}

        async def confirm(self, token):
            confirm_calls.append(token)
            raise AssertionError("confirm must not run after the baseline changes")

        async def close_position(self, *, symbol, side, vol):
            close_calls.append({"symbol": symbol, "side": side, "vol": vol})
            raise AssertionError("foreign position must not be closed")

        async def cancel(self, *, order_id, symbol):
            cancel_calls.append({"order_id": order_id, "symbol": symbol})
            raise AssertionError("foreign order must not be cancelled")

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", AuditDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    assert await hl_smoke_trade.run(live=True) == 1

    assert confirm_calls == []
    assert close_calls == []
    assert cancel_calls == []
    assert client.position == (0.25 if foreign_state == "position" else 0.0)
    assert client.open_ids == ({77} if foreign_state == "order" else set())
    assert client.closed is True
    assert released == [lock]
    assert "A: pre-confirm baseline remains clean" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_live_probe_never_claims_concurrently_observed_stop(monkeypatch):
    lock = object()
    released = []
    services = []

    class Client(_CleanLiveClient):
        def __init__(self):
            super().__init__()
            self.confirmed = False
            self.position = 0.0
            self.stop_ids = set()

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": self.position}] if self.position else []

        async def open_stop_orders(self, symbol):
            if not self.confirmed:
                return []
            return [
                {"orderId": order_id} for order_id in sorted(self.stop_ids)
            ]

    class AuditDatabase:
        def __init__(self, path):
            pass

        async def init(self):
            pass

        async def open(self):
            pass

        async def close(self):
            pass

    class Service:
        def __init__(self, client, settings, store, *, db):
            self.client = client
            self.market_volume = None
            self.cancelled = []
            services.append(self)

        async def preview(self, ticket):
            if ticket.order_type == "limit":
                return {
                    "ok": False,
                    "errors": ["Hyperliquid limit entries are disabled"],
                }
            self.market_volume = ticket.vol
            return {"ok": True, "token": "synthetic-preview"}

        async def confirm(self, token):
            self.client.confirmed = True
            self.client.position = self.market_volume
            # 11 belongs to this response. 22 appeared concurrently and must
            # remain unowned even though the post-confirm read observes it.
            self.client.stop_ids = {11, 22}
            return {
                "ok": True,
                "status": "placed",
                "sl_verified": True,
                "sl_detail": "synthetic",
                "response": {"orderId": 10, "slTriggerOid": 11},
            }

        async def close_position(self, *, symbol, side, vol):
            self.client.position = max(0.0, self.client.position - vol)
            return {"ok": True, "verified": True, "status": "closed"}

        async def cancel(self, *, order_id, symbol):
            self.cancelled.append(order_id)
            self.client.stop_ids.remove(order_id)
            return {"ok": True}

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", AuditDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    assert await hl_smoke_trade.run(live=True) == 1

    assert services[0].cancelled == [11]
    assert client.stop_ids == {22}
    assert client.closed is True
    assert released == [lock]


@pytest.mark.asyncio
async def test_live_probe_does_not_close_foreign_position_after_presend_confirm_failure(
    monkeypatch,
):
    lock = object()
    released = []
    close_calls = []

    class Client(_CleanLiveClient):
        def __init__(self):
            super().__init__()
            self.position = 0.0

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": self.position}] if self.position else []

    class AuditDatabase:
        def __init__(self, path):
            pass

        async def init(self):
            pass

        async def open(self):
            pass

        async def close(self):
            pass

    class Service:
        def __init__(self, client, settings, store, *, db):
            self.client = client

        async def preview(self, ticket):
            return {"ok": True, "token": "synthetic-preview"}

        async def confirm(self, token):
            # The probe itself did not submit. Another Testnet actor opens the
            # selected coin before this pre-send failure reaches cleanup.
            self.client.position = 0.25
            raise RuntimeError("synthetic pre-send rejection")

        async def close_position(self, *, symbol, side, vol):
            close_calls.append({"symbol": symbol, "side": side, "vol": vol})
            self.client.position = 0.0
            return {"ok": True, "verified": True, "status": "closed"}

        async def cancel(self, *, order_id, symbol):
            raise AssertionError("no probe-owned order may exist")

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", AuditDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    assert await hl_smoke_trade.run(live=True) == 1

    assert close_calls == []
    assert client.position == 0.25
    assert client.closed is True
    assert released == [lock]


@pytest.mark.asyncio
async def test_live_probe_unfilled_order_does_not_claim_or_close_foreign_position(
    monkeypatch,
):
    lock = object()
    released = []
    close_calls = []
    cancel_calls = []

    class Client(_CleanLiveClient):
        def __init__(self):
            super().__init__()
            self.position = 0.0
            self.open_ids = set()

        async def positions(self, symbol, *, fresh=False):
            return [{"holdVol": self.position}] if self.position else []

        async def open_orders(self, symbol):
            return [{"orderId": order_id} for order_id in sorted(self.open_ids)]

    class AuditDatabase:
        def __init__(self, path):
            pass

        async def init(self):
            pass

        async def open(self):
            pass

        async def close(self):
            pass

    class Service:
        def __init__(self, client, settings, store, *, db):
            self.client = client

        async def preview(self, ticket):
            if ticket.order_type == "limit":
                return {
                    "ok": False,
                    "errors": ["Hyperliquid limit entries are disabled"],
                }
            return {"ok": True, "token": "synthetic-preview"}

        async def confirm(self, token):
            # The probe order is accepted but explicitly unfilled. A different
            # Testnet actor opens the selected coin before cleanup observes it.
            self.client.open_ids = {10}
            self.client.position = 0.25
            return {
                "ok": True,
                "status": "placed_unfilled_resting",
                "sl_verified": False,
                "sl_detail": "synthetic unfilled order",
                "response": {"orderId": 10},
            }

        async def close_position(self, *, symbol, side, vol):
            close_calls.append({"symbol": symbol, "side": side, "vol": vol})
            self.client.position = max(0.0, self.client.position - vol)
            return {"ok": True, "verified": True, "status": "closed"}

        async def cancel(self, *, order_id, symbol):
            cancel_calls.append({"order_id": order_id, "symbol": symbol})
            self.client.open_ids.remove(order_id)
            return {"ok": True}

    client = Client()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", AuditDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    assert await hl_smoke_trade.run(live=True) == 1

    assert close_calls == []
    assert cancel_calls == [{"order_id": 10, "symbol": "BTC"}]
    assert client.position == 0.25
    assert client.open_ids == set()
    assert client.closed is True
    assert released == [lock]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["init", "open"])
async def test_live_probe_audit_db_failure_blocks_service_and_releases_resources(
    monkeypatch, failure_stage, capsys
):
    lock = object()
    released = []
    databases = []
    marker = f"SYNTHETIC_PRIVATE_AUDIT_{failure_stage.upper()}_PATH"

    class FailingDatabase:
        def __init__(self, path):
            databases.append(self)
            self.closed = False

        async def init(self):
            if failure_stage == "init":
                raise RuntimeError(marker)

        async def open(self):
            if failure_stage == "open":
                raise RuntimeError(marker)

        async def close(self):
            self.closed = True

    def forbidden_service(*args, **kwargs):
        raise AssertionError("money service must not exist without a ready audit DB")

    client = _CleanLiveClient()
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", FailingDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", forbidden_service)

    assert await hl_smoke_trade.run(live=True) == 1

    assert databases[0].closed is True
    assert client.closed is True
    assert released == [lock]
    output = capsys.readouterr().out
    assert "probe error" in output
    assert "RuntimeError" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_live_probe_releases_instance_lock_when_client_creation_fails(
    monkeypatch, capsys
):
    lock = object()
    released = []
    marker = "SYNTHETIC_PRIVATE_CLIENT_SETUP_ERROR"
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )

    def failing_client(settings):
        raise RuntimeError(marker)

    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", failing_client)

    assert await hl_smoke_trade.run(live=True) == 1
    assert released == [lock]
    output = capsys.readouterr().out
    assert "probe error" in output
    assert "RuntimeError" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_live_probe_redacts_primary_and_client_close_failures(monkeypatch, capsys):
    lock = object()
    released = []
    primary_marker = "SYNTHETIC_PRIVATE_ACCOUNT_SETUP_ERROR"
    close_marker = "SYNTHETIC_PRIVATE_CLIENT_CLOSE_ERROR"

    class FailingClient:
        base_url = TESTNET_URL
        testnet = True

        async def account_snapshot(self):
            raise RuntimeError(primary_marker)

        async def aclose(self):
            raise RuntimeError(close_marker)

    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: released.append(lock_path),
    )
    monkeypatch.setattr(
        hl_smoke_trade, "create_exchange_client", lambda _settings: FailingClient()
    )

    assert await hl_smoke_trade.run(live=True) == 1
    assert released == [lock]
    output = capsys.readouterr().out
    assert "probe error" in output
    assert "cleanup: close exchange client" in output
    assert primary_marker not in output
    assert close_marker not in output


@pytest.mark.asyncio
async def test_live_probe_cleanup_cancellation_still_releases_resources(
    monkeypatch,
):
    lock = object()
    released = []
    events = []

    class AuditDatabase:
        def __init__(self, path):
            pass

        async def init(self):
            pass

        async def open(self):
            pass

        async def close(self):
            events.append("db_close")

    class Service:
        def __init__(self, client, settings, store, *, db):
            pass

        async def preview(self, ticket):
            raise hl_smoke_trade.OrderError("synthetic preview stop")

    client = _CleanLiveClient()
    original_close = client.aclose

    async def tracked_close():
        events.append("client_close")
        await original_close()

    client.aclose = tracked_close
    monkeypatch.setattr(hl_smoke_trade, "get_settings", _settings)
    monkeypatch.setattr(
        hl_smoke_trade, "_acquire_live_probe_lock", lambda database_path: lock
    )
    monkeypatch.setattr(
        hl_smoke_trade,
        "_release_live_probe_lock",
        lambda lock_path: (events.append("lock_release"), released.append(lock_path)),
    )
    monkeypatch.setattr(hl_smoke_trade, "create_exchange_client", lambda _: client)
    monkeypatch.setattr(hl_smoke_trade, "Database", AuditDatabase)
    monkeypatch.setattr(hl_smoke_trade, "OrderService", Service)

    async def cancelled_cleanup(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(hl_smoke_trade, "_cleanup", cancelled_cleanup)

    with pytest.raises(asyncio.CancelledError):
        await hl_smoke_trade.run(live=True)

    assert client.closed is True
    assert released == [lock]
    assert events == ["client_close", "db_close", "lock_release"]


def test_confirm_order_ids_reject_ambiguous_or_invalid_identity():
    result = {
        "response": {
            "orderId": 10,
            "slTriggerOid": "11",
            "tpTriggerOid": None,
            "tpTriggerOid2": 12,
        }
    }
    assert hl_smoke_trade._created_order_ids(result) == {10, 11, 12}

    with pytest.raises(Exception, match="invalid slTriggerOid"):
        hl_smoke_trade._created_order_ids(
            {"response": {"orderId": 10, "slTriggerOid": True}}
        )


def test_confirm_placement_and_fill_require_exact_consistent_status():
    filled = {"ok": True, "status": "recovered_placed_partial_fill"}
    unfilled = {"ok": True, "status": "placed_unfilled_resting"}

    assert hl_smoke_trade._confirm_proves_entry_placement(filled) is True
    assert hl_smoke_trade._confirm_proves_entry_fill(filled) is True
    assert hl_smoke_trade._confirm_proves_entry_placement(unfilled) is True
    assert hl_smoke_trade._confirm_proves_entry_fill(unfilled) is False
    assert (
        hl_smoke_trade._confirm_proves_entry_placement(
            {"ok": True, "status": "placed_unrecognized_future_status"}
        )
        is False
    )
    assert (
        hl_smoke_trade._confirm_proves_entry_fill(
            {"ok": False, "status": "placed"}
        )
        is False
    )

    with pytest.raises(Exception, match="overlapping order identities"):
        hl_smoke_trade._created_order_ids(
            {"response": {"orderId": 10, "slTriggerOid": 10}}
        )
