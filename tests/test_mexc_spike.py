"""Offline safety checks for the read-only MEXC diagnostic tool."""

from types import SimpleNamespace

import pytest

import scripts.mexc_spike as mexc_spike

MEXC_URL = "https://api.mexc.com"


def _settings(**overrides):
    values = {
        "exchange": "mexc",
        "mexc_ready": True,
        "mexc_base_url": MEXC_URL,
        "mexc_api_key": "synthetic-key",
        "mexc_api_secret": "synthetic-secret",
        "default_symbol": "BTC_USDT",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mutating_legacy_flags_are_not_accepted():
    parser = mexc_spike._build_parser()

    with pytest.raises(SystemExit) as place_exit:
        parser.parse_args(["--place"])
    assert place_exit.value.code == 2

    with pytest.raises(SystemExit) as confirm_exit:
        parser.parse_args(["--confirm-live", "MEXC-LIVE"])
    assert confirm_exit.value.code == 2


@pytest.mark.asyncio
async def test_probe_rejects_inactive_mexc_exchange_before_client(monkeypatch, capsys):
    monkeypatch.setattr(
        mexc_spike,
        "get_settings",
        lambda: _settings(exchange="hyperliquid"),
    )

    def forbidden_client(*args, **kwargs):
        raise AssertionError("MEXC client must not be constructed")

    monkeypatch.setattr(mexc_spike, "MexcClient", forbidden_client)

    assert await mexc_spike.run(with_account=False) == 2
    assert "EXCHANGE must be mexc" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_account_reads_require_configured_credentials_before_client(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        mexc_spike,
        "get_settings",
        lambda: _settings(mexc_ready=False),
    )

    def forbidden_client(*args, **kwargs):
        raise AssertionError("MEXC client must not be constructed")

    monkeypatch.setattr(mexc_spike, "MexcClient", forbidden_client)

    assert await mexc_spike.run(with_account=True) == 2
    assert "--with-account requires" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_probe_rejects_invalid_default_symbol_before_client(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_SYMBOL/USDT"
    monkeypatch.setattr(
        mexc_spike,
        "get_settings",
        lambda: _settings(default_symbol=marker),
    )

    def forbidden_client(*args, **kwargs):
        raise AssertionError("invalid symbol must block before client creation")

    monkeypatch.setattr(mexc_spike, "MexcClient", forbidden_client)

    assert await mexc_spike.run(with_account=False) == 2
    output = capsys.readouterr().out
    assert "DEFAULT_SYMBOL is invalid" in output
    assert marker not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_url",
    [
        "http://api.mexc.com",
        "https://evil.example.com",
        "https://SYNTHETIC_SECRET_MARKER@api.mexc.com",
    ],
)
async def test_account_probe_rejects_noncanonical_client_before_network(
    monkeypatch, capsys, client_url
):
    calls = []

    class Client:
        def __init__(self, base_url, api_key, api_secret):
            assert base_url == MEXC_URL
            assert api_key == "synthetic-key"
            assert api_secret == "synthetic-secret"
            self.base_url = client_url

        async def ping(self):
            raise AssertionError("client endpoint must be checked before network access")

        async def assets(self):
            raise AssertionError("client endpoint must be checked before account access")

        async def aclose(self):
            calls.append("close")

    monkeypatch.setattr(mexc_spike, "get_settings", _settings)
    monkeypatch.setattr(mexc_spike, "MexcClient", Client)

    assert await mexc_spike.run(with_account=True) == 2
    assert calls == ["close"]
    output = capsys.readouterr().out
    assert "constructed client" in output
    assert "SYNTHETIC_SECRET_MARKER" not in output


@pytest.mark.asyncio
async def test_default_probe_uses_no_credentials_or_private_account_calls(monkeypatch):
    calls = []

    class Client:
        def __init__(self, base_url, api_key, api_secret):
            assert base_url == MEXC_URL
            assert api_key == ""
            assert api_secret == ""
            self.base_url = base_url
            self.closed = False

        async def ping(self):
            calls.append("ping")
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            calls.append(("contract_meta", symbol))
            return SimpleNamespace(
                symbol=symbol,
                api_allowed=True,
                contract_size=0.0001,
                price_unit=0.1,
                vol_unit=1,
                min_vol=1,
                max_vol=100_000,
                max_leverage=50,
            )

        async def ticker(self, symbol):
            calls.append(("ticker", symbol))
            return SimpleNamespace(symbol=symbol, last_price=50_000.0)

        async def assets(self):
            raise AssertionError("default probe must not read private assets")

        async def positions(self):
            raise AssertionError("default probe must not read private positions")

        async def aclose(self):
            self.closed = True

    client = Client(
        MEXC_URL,
        "",
        "",
    )
    monkeypatch.setattr(mexc_spike, "get_settings", _settings)
    monkeypatch.setattr(mexc_spike, "MexcClient", lambda *args: client)

    assert await mexc_spike.run(with_account=False) == 0
    assert calls == [
        "ping",
        ("contract_meta", "BTC_USDT"),
        ("ticker", "BTC_USDT"),
    ]
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_source", ["contract", "ticker"])
async def test_probe_rejects_cross_symbol_market_response_without_echo(
    monkeypatch, capsys, mismatched_source
):
    marker = "SYNTHETIC_PRIVATE_WRONG_SYMBOL"

    class Client:
        base_url = MEXC_URL

        def __init__(self, *args):
            self.closed = False

        async def ping(self):
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            return SimpleNamespace(
                symbol=marker if mismatched_source == "contract" else symbol,
                api_allowed=True,
                contract_size=0.0001,
                price_unit=0.1,
                vol_unit=1,
                min_vol=1,
                max_vol=100_000,
                max_leverage=50,
            )

        async def ticker(self, symbol):
            return SimpleNamespace(
                symbol=marker if mismatched_source == "ticker" else symbol,
                last_price=50_000.0,
            )

        async def assets(self):
            raise AssertionError("cross-symbol market data must stop account reads")

        async def positions(self):
            raise AssertionError("cross-symbol market data must stop account reads")

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(mexc_spike, "get_settings", _settings)
    monkeypatch.setattr(mexc_spike, "MexcClient", lambda *args: client)

    assert await mexc_spike.run(with_account=False) == 1
    assert client.closed is True
    output = capsys.readouterr().out
    assert "MexcError; provider details suppressed" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_explicit_account_probe_performs_reads_without_mutations(
    monkeypatch, capsys
):
    calls = []

    class Client:
        def __init__(self, base_url, api_key, api_secret):
            assert api_key == "synthetic-key"
            assert api_secret == "synthetic-secret"
            self.base_url = base_url

        async def ping(self):
            calls.append("ping")
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            calls.append("contract_meta")
            return SimpleNamespace(
                symbol=symbol,
                api_allowed=True,
                contract_size=0.0001,
                price_unit=0.1,
                vol_unit=1,
                min_vol=1,
                max_vol=100_000,
                max_leverage=50,
            )

        async def ticker(self, symbol):
            calls.append("ticker")
            return SimpleNamespace(symbol=symbol, last_price=50_000.0)

        async def assets(self):
            calls.append("assets")
            return [
                {
                    "currency": "USDT",
                    "equity": 100.0,
                    "availableBalance": 80.0,
                    "privateDiagnostic": "SYNTHETIC_PRIVATE_ACCOUNT_RAW",
                }
            ]

        async def positions(self):
            calls.append("positions")
            return [
                {
                    "symbol": "BTC_USDT",
                    "positionType": 1,
                    "holdVol": 1,
                    "holdAvgPrice": 50_000.0,
                    "leverage": "SYNTHETIC_PRIVATE_POSITION_RAW",
                    "openType": 1,
                }
            ]

        async def set_leverage(self, *args, **kwargs):
            raise AssertionError("read-only probe must never set leverage")

        async def place_order(self, *args, **kwargs):
            raise AssertionError("read-only probe must never place orders")

        async def cancel_order(self, *args, **kwargs):
            raise AssertionError("read-only probe must never cancel orders")

        async def aclose(self):
            calls.append("close")

    monkeypatch.setattr(mexc_spike, "get_settings", _settings)
    monkeypatch.setattr(mexc_spike, "MexcClient", Client)

    assert await mexc_spike.run(with_account=True) == 0
    assert calls == [
        "ping",
        "contract_meta",
        "ticker",
        "assets",
        "positions",
        "close",
    ]
    output = capsys.readouterr().out
    assert "'present': True" in output
    assert "'equity': 100.0" in output
    assert "'availableBalance': 80.0" in output
    assert "'leverage': None" in output
    assert "SYNTHETIC_PRIVATE_ACCOUNT_RAW" not in output
    assert "SYNTHETIC_PRIVATE_POSITION_RAW" not in output
    assert "synthetic-key" not in output
    assert "synthetic-secret" not in output


@pytest.mark.parametrize(
    "assets",
    [
        None,
        ["SYNTHETIC_SECRET_MARKER"],
        [
            {
                "currency": "USDT",
                "equity": "SYNTHETIC_SECRET_MARKER",
                "availableBalance": 80.0,
            }
        ],
        [
            {"currency": "USDT", "equity": 100.0, "availableBalance": 80.0},
            {"currency": "USDT", "equity": 90.0, "availableBalance": 70.0},
        ],
    ],
)
def test_asset_summary_rejects_malformed_private_data(assets):
    with pytest.raises(mexc_spike.MexcError):
        mexc_spike._asset_summary(assets)


@pytest.mark.parametrize(
    "positions",
    [
        None,
        ["SYNTHETIC_SECRET_MARKER"],
        [
            {
                "symbol": "SYNTHETIC_SECRET_MARKER",
                "positionType": 1,
                "holdVol": 1,
                "holdAvgPrice": 50_000.0,
                "openType": 1,
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "positionType": 1,
                "holdVol": "SYNTHETIC_SECRET_MARKER",
                "holdAvgPrice": 50_000.0,
                "openType": 1,
            }
        ],
        [
            {
                "symbol": "BTC_USDT",
                "positionType": True,
                "holdVol": 1,
                "holdAvgPrice": 50_000.0,
                "openType": 1,
            }
        ],
    ],
)
def test_position_summaries_reject_malformed_private_data(positions):
    with pytest.raises(mexc_spike.MexcError):
        mexc_spike._position_summaries(positions)


@pytest.mark.asyncio
async def test_read_failure_redacts_provider_detail_and_closes_client(
    monkeypatch, capsys
):
    marker = "SYNTHETIC_SECRET_MARKER"

    class Client:
        def __init__(self, *args):
            self.base_url = MEXC_URL
            self.closed = False

        async def ping(self):
            raise mexc_spike.MexcError(marker)

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(mexc_spike, "get_settings", _settings)
    monkeypatch.setattr(mexc_spike, "MexcClient", lambda *args: client)

    assert await mexc_spike.run(with_account=False) == 1
    assert client.closed is True
    output = capsys.readouterr().out
    assert "MexcError; provider details suppressed" in output
    assert marker not in output


def test_redaction_covers_nested_credential_shapes():
    assert mexc_spike._redact(
        {
            "apiKey": "one",
            "nested": {
                "private-key": "two",
                "accessToken": "three",
                "value": 4,
            },
        }
    ) == {
        "apiKey": "***",
        "nested": {
            "private-key": "***",
            "accessToken": "***",
            "value": 4,
        },
    }


def test_redaction_removes_configured_secrets_regardless_of_field_name():
    access_key = "synthetic-access-key"
    secret = "synthetic-api-secret"
    redacted = mexc_spike._redact(
        {
            "accessKey": access_key,
            "note": f"provider echoed {secret}",
            secret: "secret appeared in a key",
            "nested": [
                {"passphrase": "one"},
                {"clientCredential": "two"},
                {"setCookie": "three"},
            ],
        },
        secret_values=(access_key, secret),
    )

    rendered = str(redacted)
    assert access_key not in rendered
    assert secret not in rendered
    assert "provider echoed [redacted]" in rendered
    assert redacted["accessKey"] == "***"
    assert all(next(iter(item.values())) == "***" for item in redacted["nested"])


def test_redaction_replaces_overlapping_secrets_longest_first():
    prefix = "synthetic-overlap"
    full_secret = f"{prefix}-private-suffix"

    assert mexc_spike._redact(
        {"note": full_secret},
        secret_values=(prefix, full_secret),
    ) == {"note": "[redacted]"}


@pytest.mark.asyncio
async def test_unexpected_client_setup_failure_is_redacted(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_MEXC_CLIENT_SETUP_ERROR"
    monkeypatch.setattr(mexc_spike, "get_settings", _settings)

    def failing_client(*args):
        raise RuntimeError(marker)

    monkeypatch.setattr(mexc_spike, "MexcClient", failing_client)

    assert await mexc_spike.run(with_account=False) == 1
    output = capsys.readouterr().out
    assert "MEXC read-only probe failed: RuntimeError" in output
    assert marker not in output


@pytest.mark.asyncio
async def test_client_close_failure_changes_exit_code_without_detail(monkeypatch, capsys):
    marker = "SYNTHETIC_PRIVATE_MEXC_CLIENT_CLOSE_ERROR"

    class Client:
        base_url = MEXC_URL

        async def ping(self):
            return 1_700_000_000_000

        async def contract_meta(self, symbol):
            return SimpleNamespace(
                symbol=symbol,
                api_allowed=True,
                contract_size=0.0001,
                price_unit=0.1,
                vol_unit=1,
                min_vol=1,
                max_vol=100_000,
                max_leverage=50,
            )

        async def ticker(self, symbol):
            return SimpleNamespace(symbol=symbol, last_price=50_000.0)

        async def aclose(self):
            raise RuntimeError(marker)

    monkeypatch.setattr(mexc_spike, "get_settings", _settings)
    monkeypatch.setattr(mexc_spike, "MexcClient", lambda *args: Client())

    assert await mexc_spike.run(with_account=False) == 1
    output = capsys.readouterr().out
    assert "MEXC client close failed: RuntimeError" in output
    assert marker not in output
