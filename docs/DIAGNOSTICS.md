# Diagnostics and Testnet checks

[Quick start](../README.md#quick-start-on-windows) ·
[Operating guide](OPERATIONS.md) · [Support](../SUPPORT.md)

Use the repository root and its prepared `.venv`. The automated regression suite
and responsive UI smoke are offline/mock-based. The diagnostics below are
different: they contact exchange endpoints. Do not run private reads or trading
probes unless you deliberately intend that access for the configured account.

## Choose the access level

| Tool and mode | Network | Account access | Order mutation |
| --- | --- | --- | --- |
| `python -m pytest tests/ -q` | Mocked exchanges/providers | Synthetic data | None |
| `scripts/ui_smoke.py` | Mocked browser responses | Synthetic data | None |
| `scripts/hl_spike.py` | Hyperliquid Testnet only | None by default; `--with-account` opts into private reads | None |
| `scripts/mexc_spike.py` | MEXC live endpoints | None by default; `--with-account` opts into private reads | None |
| `scripts/hl_smoke_trade.py` | Hyperliquid Testnet only | None by default; `--with-account` runs private Preview checks | None in those two modes |
| `scripts/hl_smoke_trade.py --confirm TESTNET` | Hyperliquid Testnet only | Authenticated Testnet account | Places, protects and cleans up a bounded probe |

MEXC is not a substitute for Hyperliquid Testnet. `HL_TESTNET=true` does not turn
MEXC into a simulated or Testnet venue.

## Public connectivity

Keep trading disabled. For Hyperliquid, configure `EXCHANGE=hyperliquid` and
`HL_TESTNET=true`, then run:

```powershell
.\.venv\Scripts\python.exe scripts/hl_spike.py
.\.venv\Scripts\python.exe scripts/hl_smoke_trade.py
```

Both default modes construct a credential-free client and refuse Mainnet.
They validate the constructed client's Testnet flag and canonical HTTPS endpoint
before the first exchange request. A successful public preflight proves neither
private-key permissions nor order execution.

For public MEXC connectivity, configure `EXCHANGE=mexc` and run:

```powershell
.\.venv\Scripts\python.exe scripts/mexc_spike.py
```

The tool validates the canonical MEXC HTTPS endpoint and selected symbol before
trusting responses. It remains read-only, but the endpoint is the live venue.

## Explicit private reads

Only when account access is intended, add `--with-account` to the selected tool:

```powershell
.\.venv\Scripts\python.exe scripts/hl_spike.py --with-account
.\.venv\Scripts\python.exe scripts/hl_smoke_trade.py --with-account
```

Hyperliquid account-backed modes require a valid private key and, when set, a
valid account address before client creation or network access. The lifecycle
tool's `--with-account` mode can run private Preview checks but never confirms
an order. `scripts/mexc_spike.py --with-account` opts into real MEXC asset and
position reads; it requires the configured MEXC key and secret and never places
or cancels an order.

Private read results can contain financial information. Keep output local and
sanitize any excerpt before asking for support.

## Explicit Testnet order lifecycle

This is a manual exchange integration probe, not an offline regression test.
Use a dedicated Hyperliquid Testnet account and stop the dashboard process first.
Do not operate the same account manually while the probe runs.

The script requires `EXCHANGE=hyperliquid`, `HL_TESTNET=true`, a valid account
identity, `TRADING_ENABLED=true` and the exact acknowledgement:

```powershell
.\.venv\Scripts\python.exe scripts/hl_smoke_trade.py --confirm TESTNET
```

`--confirm TESTNET` and `--with-account` are mutually exclusive. Before mutation,
the tool checks the canonical Testnet client, takes the app instance lock, opens
the local audit database and requires a clean selected-coin position/order
baseline. It uses the authoritative Preview/Confirm service, targets about
$20 notional at low leverage and rejects a rounded probe above $50.

The exercised entry is a protected market order. The Hyperliquid limit-entry
case checks that the app refuses it; the probe does not enable limit entries.
Cleanup only acts on probe-owned evidence and returned order IDs. Inspect the
Testnet account after a failed or uncertain run; do not assume cleanup succeeded
or rerun blindly. Restore the intended disarmed configuration afterward.

For the synthetic automated checks and their install commands, use
[Development checks](../README.md#development-checks). Offline success does not
certify Mainnet execution or profitability.
