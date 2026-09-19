"""Offline safety checks for the manual Hyperliquid Testnet smoke tool."""

from types import SimpleNamespace

import pytest

import scripts.hl_spike as hl_spike

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            SimpleNamespace(exchange="mexc", hl_testnet=True),
            "EXCHANGE must be hyperliquid",
        ),
        (
            SimpleNamespace(exchange="hyperliquid", hl_testnet=False),
            "HL_TESTNET must be true",
        ),
        (
            SimpleNamespace(exchange="hyperliquid", hl_testnet=None),
            "HL_TESTNET must be true",
        ),
    ],
)
def test_testnet_smoke_rejects_non_testnet_configuration(settings, message):
    assert message in (hl_spike._testnet_configuration_error(settings) or "")


def test_testnet_smoke_accepts_explicit_hyperliquid_testnet():
    settings = SimpleNamespace(exchange="hyperliquid", hl_testnet=True)

    assert hl_spike._testnet_configuration_error(settings) is None


@pytest.mark.asyncio
async def test_probe_rejects_invalid_default_symbol_before_client(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_SYMBOL/USDT"
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_ready=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol=marker,
    )
    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(
        hl_spike,
        "HyperliquidClient",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid symbol must block before client creation")
        ),
    )

    assert await hl_spike.main(with_account=True) == 2
    output = capsys.readouterr().out
    assert "DEFAULT_SYMBOL is invalid" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_rejected_configuration_never_constructs_exchange_client(
    monkeypatch, capsys
):
    settings = SimpleNamespace(exchange="hyperliquid", hl_testnet=False)
    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)

    def forbidden_client(*args, **kwargs):
        raise AssertionError("exchange client must not be constructed")

    monkeypatch.setattr(hl_spike, "HyperliquidClient", forbidden_client)

    assert await hl_spike.main(with_account=True) == 2
    assert "refusing to contact Hyperliquid Mainnet" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_account_probe_requires_key_before_client_or_network(monkeypatch, capsys):
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_private_key="",
        hl_ready=False,
    )
    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)

    def forbidden_client(*args, **kwargs):
        raise AssertionError("client must not be constructed without an account key")

    monkeypatch.setattr(hl_spike, "HyperliquidClient", forbidden_client)

    assert await hl_spike.main(with_account=True) == 2
    assert "--with-account requires a valid HL_PRIVATE_KEY" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "testnet"),
    [
        ("https://api.hyperliquid.xyz", True),
        (TESTNET_URL, False),
        ("https://SYNTHETIC_SECRET_MARKER@api.hyperliquid-testnet.xyz", True),
    ],
)
async def test_account_probe_rejects_non_testnet_client_before_network(
    monkeypatch, capsys, base_url, testnet
):
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_ready=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.base_url = base_url
            self.testnet = testnet

        async def ping(self):
            raise AssertionError("client target must be checked before network access")

        async def account_snapshot(self):
            raise AssertionError("client target must be checked before account access")

        async def aclose(self):
            calls.append("close")

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=True) == 2
    assert calls == ["close"]
    output = capsys.readouterr().out
    assert "constructed client" in output
    assert "SYNTHETIC_SECRET_MARKER" not in output


@pytest.mark.asyncio
async def test_account_probe_uses_validated_credentials_and_reads_account(monkeypatch):
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_ready=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )
    calls = []

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self, *, private_key, account_address, testnet, base_url):
            assert private_key == "synthetic-private-key"
            assert account_address == "0xsynthetic-account"
            assert testnet is True
            assert base_url is None

        async def ping(self):
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            return SimpleNamespace(symbol=symbol, vol_unit=0.001, max_leverage=20)

        async def ticker(self, symbol):
            return SimpleNamespace(
                symbol=symbol, last_price=50_000.0, funding_rate=0.0
            )

        async def klines(self, symbol, interval, *, limit_hint):
            return [SimpleNamespace(close=50_000.0)]

        async def account_snapshot(self):
            calls.append("account_snapshot")
            return {"equity_usdt": 100.0, "available_usdt": 80.0, "positions": []}

        async def aclose(self):
            calls.append("close")

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=True) == 0
    assert calls == ["account_snapshot", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "equity_usdt": "SYNTHETIC_SECRET_MARKER",
            "available_usdt": 80.0,
            "positions": [],
        },
        {
            "equity_usdt": 100.0,
            "available_usdt": "SYNTHETIC_SECRET_MARKER",
            "positions": [],
        },
        {
            "equity_usdt": 100.0,
            "available_usdt": 80.0,
            "positions": "SYNTHETIC_SECRET_MARKER",
        },
        {
            "equity_usdt": 100.0,
            "available_usdt": 80.0,
            "positions": ["SYNTHETIC_SECRET_MARKER"],
        },
    ],
)
async def test_account_probe_rejects_malformed_private_snapshot_without_echo(
    monkeypatch, capsys, snapshot
):
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_ready=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self, **kwargs):
            pass

        async def ping(self):
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            return SimpleNamespace(symbol=symbol, vol_unit=0.001, max_leverage=20)

        async def ticker(self, symbol):
            return SimpleNamespace(
                symbol=symbol, last_price=50_000.0, funding_rate=0.0
            )

        async def klines(self, symbol, interval, *, limit_hint):
            return [SimpleNamespace(close=50_000.0)]

        async def account_snapshot(self):
            return snapshot

        async def aclose(self):
            pass

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=True) == 1
    output = capsys.readouterr().out
    assert "HyperliquidError; provider details suppressed" in output
    assert "SYNTHETIC_SECRET_MARKER" not in output


@pytest.mark.asyncio
async def test_default_probe_constructs_credential_free_client(monkeypatch):
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )
    calls = []

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self, *, private_key, account_address, testnet, base_url):
            assert private_key == ""
            assert account_address == ""
            assert testnet is True
            assert base_url is None

        async def ping(self):
            calls.append("ping")
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            calls.append(("contract_meta", symbol))
            return SimpleNamespace(symbol=symbol, vol_unit=0.001, max_leverage=20)

        async def ticker(self, symbol):
            calls.append(("ticker", symbol))
            return SimpleNamespace(
                symbol=symbol, last_price=50_000.0, funding_rate=0.0
            )

        async def klines(self, symbol, interval, *, limit_hint):
            calls.append(("klines", symbol, interval, limit_hint))
            return [SimpleNamespace(close=50_000.0)]

        async def account_snapshot(self):
            raise AssertionError("default probe must not read the private account")

        async def aclose(self):
            calls.append("close")

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=False) == 0
    assert calls == [
        "ping",
        ("contract_meta", "BTC"),
        ("ticker", "BTC"),
        ("klines", "BTC", "15m", 20),
        "close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_source", ["contract", "ticker"])
async def test_probe_rejects_cross_symbol_market_response_without_echo(
    monkeypatch, capsys, mismatched_source
):
    marker = "SYNTHETIC_PRIVATE_WRONG_SYMBOL"
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self, **kwargs):
            pass

        async def ping(self):
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            return SimpleNamespace(
                symbol=marker if mismatched_source == "contract" else symbol,
                vol_unit=0.001,
                max_leverage=20,
            )

        async def ticker(self, symbol):
            return SimpleNamespace(
                symbol=marker if mismatched_source == "ticker" else symbol,
                last_price=50_000.0,
                funding_rate=0.0,
            )

        async def klines(self, *args, **kwargs):
            raise AssertionError("cross-symbol market data must stop the probe")

        async def account_snapshot(self):
            raise AssertionError("cross-symbol market data must stop account reads")

        async def aclose(self):
            pass

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=False) == 1
    output = capsys.readouterr().out
    assert "HyperliquidError; provider details suppressed" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_probe_failure_does_not_print_provider_detail(monkeypatch, capsys):
    marker = "SYNTHETIC_SECRET_MARKER"
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self, **kwargs):
            pass

        async def ping(self):
            raise hl_spike.HyperliquidError(marker)

        async def aclose(self):
            pass

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=False) == 1
    output = capsys.readouterr().out
    assert "HyperliquidError; provider details suppressed" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_unexpected_client_setup_failure_is_redacted(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_HL_CLIENT_SETUP_ERROR"
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )
    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)

    def failing_client(**kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(hl_spike, "HyperliquidClient", failing_client)

    assert await hl_spike.main(with_account=False) == 1
    output = capsys.readouterr().out
    assert "HL probe error: RuntimeError" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_client_close_failure_changes_exit_code_without_detail(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_HL_CLIENT_CLOSE_ERROR"
    settings = SimpleNamespace(
        exchange="hyperliquid",
        hl_testnet=True,
        hl_private_key="synthetic-private-key",
        hl_account_address="0xsynthetic-account",
        hl_base_url="",
        default_symbol="BTC_USDT",
    )

    class Client:
        base_url = TESTNET_URL
        testnet = True

        def __init__(self, **kwargs):
            pass

        async def ping(self):
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            return SimpleNamespace(symbol=symbol, vol_unit=0.001, max_leverage=20)

        async def ticker(self, symbol):
            return SimpleNamespace(
                symbol=symbol, last_price=50_000.0, funding_rate=0.0
            )

        async def klines(self, symbol, interval, *, limit_hint):
            return []

        async def aclose(self):
            raise RuntimeError(marker)

    monkeypatch.setattr(hl_spike, "get_settings", lambda: settings)
    monkeypatch.setattr(hl_spike, "HyperliquidClient", Client)

    assert await hl_spike.main(with_account=False) == 1
    output = capsys.readouterr().out
    assert "HL client close error: RuntimeError" in output
    assert marker not in output
