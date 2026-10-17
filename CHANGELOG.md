# Changelog

This log describes user-visible changes on `main`; it is not a live-trading
certification or a record of private account performance.

## 2026-10-17 - Execution safety and reproducible verification

- Durable MEXC entry reservations now require terminal, order-bound
  reconciliation with visible position exposure; partial fills, stale snapshots
  and time alone cannot release the risk budget.
- Entry theses use verified order/position links scoped to account and network,
  rather than timestamp proximity or the latest proposal.
- Missing or partial stop coverage raises a warning and pauses automatic stop
  movement. Verified recovery clears the warning; transient snapshot gaps
  preserve position-management state.
- Preview and Confirm bind the reviewed execution network and account. Updated
  browser asset URLs prevent cached safety bundles from surviving an upgrade.
- Process ownership uses a retained OS file lock, including compatible handling
  of older lock markers. Shared SQLite work rolls back cancelled transactions.
- Launcher startup verifies the actual Venv interpreter and local app identity;
  health probes reject redirects and foreign listeners.
- Reevaluation validates optional financial fields before provider calls;
  ambiguous billable AI POST outcomes are not automatically repeated.
- Runtime and development locks now enforce SHA-256 artifact hashes. `urllib3`
  is updated to 2.8.0; dependency audits, deterministic regressions, committed
  whitespace checks and network-change UI smoke coverage are included in CI.
  Symbol-switch checks no longer mistake background polling for duplicate loads.
- Operating, diagnostic, support and maintainer documentation now distinguishes
  offline checks, private account reads and explicit Testnet order probes.

Execution fixes: [PR #5](https://github.com/Chap0815/ObsidianLiveTrader/pull/5).

## 2026-09-08 - Public source preparation

- English dashboard, setup, launchers, runtime messages and operating guide.
- Larger responsive chart and compact market statistics.
- Expanded risk, order-recovery and offline regression coverage.
- Guided Windows startup with project-local dependency installation.
- Publication documentation, license, third-party notices and automated checks.
- Git attribution consolidated under maintainer Chap0815.

See the [operating guide](README.md) for supported behavior and limitations.
