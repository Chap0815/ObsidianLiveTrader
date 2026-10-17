# Security policy

The current `main` branch is the maintenance target. No response-time or
long-term-support commitment is offered.

## Report privately

Use [Report a vulnerability](https://github.com/Chap0815/ObsidianLiveTrader/security/advisories/new).
If unavailable, request a private channel from the maintainer without including
exploit details in a public issue. Never post credentials, private account
identifiers, raw `.env` files or unredacted logs/screenshots.

Include the affected commit, prerequisites, expected and observed behavior,
and a minimal reproduction with synthetic data. Relevant issues include
credential exposure, authentication bypass, unintended order authority,
unsafe stop handling and disclosure of private runtime data.
Explain the failing safety boundary and whether the observation is from an
offline mock or an explicitly authorized Testnet probe. Do not reproduce an
unintended mutation against Mainnet or a funded live MEXC account.

For exposed credentials, revoke or rotate them with the provider. Removing a
file or rewriting Git history does not invalidate a credential.

## Operating boundaries

Keep the server on loopback and use one process/worker. Use trade-only exchange
credentials without withdrawal permissions. Keep trading disabled during setup;
use Hyperliquid Testnet for exchange acceptance. MEXC has no Testnet mode in this
application. Review updates and dependencies before using real funds. Check
positions directly at the exchange when behavior is unexpected; a GitHub report
is not account supervision.

The application is a local, single-user cockpit, not a hosted multi-tenant
service. Do not publish the server through a public proxy or tunnel or bypass
the instance lock. Preview/Confirm authority, fresh server risk gates and
verified stop protection remain mandatory regardless of browser or AI output.

`INCLUDE_ACCOUNT_IN_LLM=false` is the privacy default. External position review
requires explicit opt-in; local Ollama can use the reduced context. Treat keys,
account identities, financial context, databases and backups as private even
when trading is disarmed. See the [operating guide](docs/OPERATIONS.md).

## Dependency and verification policy

Runtime and development dependencies use exact, SHA-256-hashed lockfiles. The
launcher and documented installs enforce `--require-hashes --no-deps`. CI checks
fresh installation, both OSV audits, secret scanning, regressions, responsive UI
behavior, lint, JavaScript syntax and the tracked publication boundary. A clean
audit reports known advisories at scan time; it is not proof that a dependency
has no undiscovered vulnerabilities.

Offline tests and passing GitHub checks do not certify live order execution,
exchange availability, profitability or readiness for real funds. Manual
diagnostics have distinct public, private-read and Testnet-mutation modes; use
the [diagnostics guide](docs/DIAGNOSTICS.md) before running one.
