# Support

Start with the [quick start](README.md#quick-start-on-windows).
For usage and installation questions, use [Discussions](https://github.com/Chap0815/ObsidianLiveTrader/discussions).
Report reproducible defects in [Issues](https://github.com/Chap0815/ObsidianLiveTrader/issues).

Include your OS, Python version, commit/version, exchange, Testnet/Mainnet mode,
reproduction steps and a short sanitized error excerpt. Never attach `.env`,
private keys, raw databases, account screenshots or unredacted runtime folders.
Security reports follow [SECURITY.md](SECURITY.md).

## Common startup problems

- **Python opens the Microsoft Store:** install Python 3.11+ and disable its
  Windows app-execution aliases. Then run `start.bat` again.
- **Venv identity/ABI is invalid:** the launcher checks the actual Venv Python,
  not just its path. Stop the app and prepare a compatible project environment;
  do not point pip at the system interpreter or remove `.env`/`data/`.
- **Port already in use or foreign listener:** the default port is 8787. The
  launcher reopens only a listener with the expected app health identity and
  refuses redirects. Stop a known old instance normally, or choose a free port
  as described in the [operating guide](docs/OPERATIONS.md#startup-and-configuration).
  Do not start a second process to work around a lock warning.
- **Python keeps running after closing a window:** closing the browser is not
  shutdown. The Windows launchers bind a new server to the original start
  window; use Ctrl+C there and wait for completion. A second launcher only
  opens an existing server and explicitly does not own it. Manual Uvicorn or
  service starts have their own lifecycle. Do not terminate unrelated Python
  processes or delete the instance lock; restarting/stopping is not a position
  close or order cancellation.
- **Browser shows old UI:** restart the server, then refresh with Ctrl+F5.
- **Dependency installation fails:** verify network access and Python version.
  A hash mismatch must be investigated, not bypassed by removing hash checks.
  `start.bat --skip-install` can reuse a successful install, but does not apply
  changed locks after an update.
- **Setup is locked:** completed setup or stored sensitive configuration closes
  the unauthenticated setup path. Update the private `.env` and restart; do not
  clear setup markers or delete credentials to reopen it.
- **AI unavailable:** check the configured provider and its API access. Manual
  chart viewing and the order ticket do not require AI analysis. A timeout or
  gateway error may represent an accepted provider request; immediate automatic
  retries are intentionally limited. External position review also requires
  `INCLUDE_ACCOUNT_IN_LLM=true`.

## Orders, protection and automation

- **Execution network changed:** the reviewed token no longer matches the
  current venue, account or network. Review the intended context and create a
  fresh preview; do not try to reuse the token.
- **Entry is still converging:** a MEXC submission is waiting for exact terminal
  order and position evidence. Partial fills and delayed open-order snapshots
  can keep it blocked across restarts. Verify order status, executed quantity
  and position identity at the exchange. Preserve local state instead of
  deleting the reservation or database.
- **Confirm/Close/SL request timed out:** an action may already have reached the
  exchange. Inspect its actual result before issuing another mutation. A cancel
  acknowledgement can race with a fill and is not by itself a flat position.
- **Stop coverage is incomplete or unknown:** verify the full position size is
  protected. The app pauses automatic stop movement until it can verify that
  coverage; a tighter partial stop is not complete protection.
- **Automatic break-even/trailing is unavailable:** these rules are
  Hyperliquid-only and require per-position opt-in. Reconcile unknown stop moves
  before re-arming. The position-management kill switch only disarms those
  rules; it does not flatten or cancel.
- **Entry thesis is missing:** no verified order/position provenance is
  available. The app deliberately does not borrow an old or newer proposal.

For the full control boundaries, see the [operating guide](docs/OPERATIONS.md).
For public connectivity, private reads and explicit Testnet integration probes,
see [Diagnostics](docs/DIAGNOSTICS.md). Never share the raw private output of
those tools in a public report.

This project offers no guaranteed response time or managed trading service.
If positions are affected, inspect orders and exposure directly at the exchange.
Stopping the application does not itself close positions. Preserve local state;
do not delete the database to dismiss an error.
