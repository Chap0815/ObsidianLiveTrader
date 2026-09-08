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

For exposed credentials, revoke or rotate them with the provider. Removing a
file or rewriting Git history does not invalidate a credential.

## Operating boundaries

Keep the server on loopback and use one process/worker. Use trade-only exchange
credentials without withdrawal permissions. Keep trading disabled during setup;
use Hyperliquid Testnet for acceptance. Review updates and dependencies before
using real funds. Check positions directly at the exchange when behavior is
unexpected; a GitHub report is not account supervision.
