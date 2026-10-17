# Operating guide

[Quick start](../README.md#quick-start-on-windows) ·
[Diagnostics](DIAGNOSTICS.md) · [Troubleshooting](../SUPPORT.md) ·
[Security](../SECURITY.md)

Obsidian Live Trader runs locally for one user, process and worker. Start with
Hyperliquid Testnet. MEXC has no Testnet mode in this application; its account
reads and trading target the live venue.

## Startup and configuration

Run `start.bat` from the extracted or cloned repository. The launcher verifies
the project's `.venv` interpreter and installs the exact, hash-verified runtime
lockfile there. The browser opens guided setup on a fresh installation.

- `setup.bat` runs terminal setup instead of the browser assistant.
- `start.bat --no-browser` starts without opening a browser.
- `start.bat --port 8788` selects a bootstrap port on a fresh installation.
  For an existing configuration, change `PORT` in the private `.env` and restart.
- `start.bat --skip-install` reuses an already prepared environment. Do not use
  it to skip applying a changed dependency lock after an update.

The default address is `http://127.0.0.1:8787`. Keep the server on loopback and
do not expose it through a public reverse proxy, tunnel or multi-worker service.
The launcher checks the health identity of an existing listener and refuses to
treat a foreign listener or HTTP redirect as this application.

On Windows, `start.bat` and `start.ps1` bind a newly started server to their
launcher and calling start window. Keep that original window open. **Ctrl+C**
allows up to ten seconds for graceful server shutdown; closing/killing the
start window ends its guarded server process tree, including reload children.
Abrupt closure can interrupt cleanup, so prefer Ctrl+C and wait for completion.
Closing the browser does not stop the server. A second launcher that finds an
existing instance only opens its browser and does not acquire shutdown authority
over that server. This binding does not apply to manually launched Uvicorn or
service installations. No process exit automatically closes exchange positions
or cancels their orders.

Use [`.env.example`](../.env.example) as a field reference and
[`app/config.py`](../app/config.py) for authoritative defaults and validation.
Guided setup creates a safe bootstrap configuration. Copying `.env.example`
sets `SETUP_COMPLETE=true`, so it locks the first-run write path. Once setup is
locked, change exchange configuration in the private `.env` and restart; do not
clear the setup marker to reopen an unauthenticated configuration write path.

Risk profiles provide presets only for fields without explicit overrides.
An active `MAX_*`, `MIN_*` or `STRICT_*` assignment overrides the profile.
Review effective values in the UI instead of assuming a profile name changed
every limit.

## Trading and position controls

Every entry follows `Preview -> Confirm`. A passing preview does not place an
order. Its token is short-lived and single-use; Confirm reads fresh market and
account data and reruns the server gates. The server also binds that authority
to the account, venue and network. Review again after a context change,
expired token or changed ticket.

| Control | Effect | What it does not do |
| --- | --- | --- |
| `TRADING_ENABLED=false` | Blocks entry confirmation, Close and SL modification | Does not close positions or cancel existing orders; authenticated Cancel remains available |
| Per-position rule opt-in | Enables the selected supported automatic rule | Does not authorize entries or bypass server gates |
| Position-management kill switch | Disarms automatic rules on all managed open positions | Does not flatten, cancel orders or change `TRADING_ENABLED` |
| Stop the local process | Stops local polling and automation | Does not close exchange positions or remove resting exchange orders |

Automatic break-even and ATR trailing are Hyperliquid-only and require explicit
per-position opt-in. Missing, partial or unverifiable stop coverage pauses
automatic stop movement and raises a coverage alert. That alert clears when the
full position is verifiably protected. Stop replacement follows
`create -> verify -> remove old`; do not assume a move succeeded from a submitted
request alone.

Entries require exchange-side protection by default. Manual trigger mode is
disabled by default because it can leave an entry without exchange SL/TP. It
requires deliberate configuration and acknowledgement; closing the browser
does not provide an exit mechanism.

## Unknown outcomes and delayed exchange state

A timeout or server error after submission may mean an order is already live.
Inspect the exact order, open orders, filled volume and position at the exchange
before trying another entry. Re-previewing is not an idempotent retry of the
same order.

MEXC submissions keep a durable same-side reservation while exchange state
converges. A partial position, an empty lagging order list, a restart or elapsed
time cannot release it. Reconciliation requires exact terminal order evidence
and accounted-for exposure; a positive fill must identify the current position.
A cancel acknowledgement alone is insufficient if a fill raced with the cancel.
Unknown or missing evidence keeps new same-side entries blocked. Preserve the
database and inspect the exchange; do not delete or edit local state to dismiss
the block. See [support](../SUPPORT.md#orders-protection-and-automation).

The entry thesis is assigned only from a verified order/position link in the
current account and network. A new analysis, later add-on or old same-side order
cannot substitute for it. Missing provenance is displayed as unavailable.

## AI privacy and journal interpretation

AI analysis is optional and advisory. Provider API access is configured
separately; a model response cannot authorize an order or override a risk gate.
The **AI keys** panel provides named, provider-specific model presets and shows
the exact API ID before saving. Custom IDs and local Ollama tags remain editable.
Opening the list or selecting a preset makes no provider request and does not
change the saved model until **Save**. Cloud access depends on the account;
Ollama examples must already be installed locally. A listed model is not a
trading-quality recommendation or a guarantee of availability.
Ambiguous provider POST outcomes are not automatically repeated, so an outage
can require a deliberate later request.

`INCLUDE_ACCOUNT_IN_LLM=false` is the default. External position reevaluation
requires explicit account-data opt-in. Local Ollama can use the reduced position
context without external disclosure. Treat enabling the opt-in as permission
to disclose the included financial/position context to the selected provider.
Review it again when switching providers.

The journal and calibration pages measure advisory shadow outcomes from market
history. They are not a broker statement, realized account PnL or a profitability
certificate. Missing history can leave an outcome unresolved or excluded.

## Changing account, network or version

Before restarting or changing configuration, verify existing positions and their
protection directly at the exchange. Local automation stops with the process.

1. Stop the application normally. Keep a private backup of `.env` and `data/`
   while the process is stopped; protect those copies like credentials.
2. Apply the intended source update or account/network configuration change.
   Keep `TRADING_ENABLED=false` during setup and validation.
3. Run the normal launcher so it can verify the Venv and updated hash-lockfile.
   Configuration, database and backups stay private and out of Git.
4. Check the account, venue and network in the UI. Use the appropriate
   [public or explicitly account-backed diagnostics](DIAGNOSTICS.md).
5. Review existing exposure and automation settings before deliberately arming.

For Hyperliquid Mainnet, `HL_TESTNET=false` selects real funds. Armed startup
also requires the explicit `MAINNET_ACK=true` acknowledgement. `HL_BASE_URL`, if
overridden, must match the configured network. MEXC is live regardless of the
Hyperliquid-only `HL_TESTNET` flag.

## State to retain privately

Keep `.env`, keys, account identities, `data/`, SQLite sidecars, logs and backups
out of GitHub issues and commits. Preserve unresolved-order and position state.
The instance lock is a persistent file held by the process through an OS lock;
its presence alone does not mean a process is running. Do not unlink it to
bypass an instance warning.

Passing offline tests verifies the tested behavior. It does not certify live
execution, future returns or readiness for real funds.
