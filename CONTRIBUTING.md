# Maintenance and feedback

Obsidian Live Trader is maintained by **Chap0815** under the source-available
terms in [LICENSE](LICENSE).

Bug reports and suggestions are welcome through [Issues](https://github.com/Chap0815/ObsidianLiveTrader/issues);
usage questions belong in [Discussions](https://github.com/Chap0815/ObsidianLiveTrader/discussions).
Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

Official development and releases are maintained exclusively by the project
owner. External code contributions and unsolicited code pull requests are not
accepted. Local modifications remain permitted within the project license;
this maintenance policy does not restrict rights independently granted by it.

## Maintainer workflow

- Read `AGENTS.md` and inspect the current worktree before editing.
- Use small patches with evidence and relevant regression coverage.
- Run the offline tests, Ruff, JavaScript syntax checks and publication check.
  UI changes also require `scripts/ui_smoke.py`.
- Use synthetic credentials and isolated data. Never send real orders,
  cancellations or provider LLM requests in automated tests.
- Review every staged file. Keep keys, `.env`, account data, logs, backups and
  local session files out of Git, including its history.
- Preserve `Preview -> Confirm`, stop protection, fresh risk gates and locks.

The official `main` branch requires a pull request with passing checks. Force
pushes and deletion are blocked; version tags matching `v*` cannot be rewritten
or deleted. Only the owner has repository write access. Administrative settings
remain under the owner's control.

The public repository includes the offline regression suite. Passing it checks
implemented behavior; it does not certify live exchange execution or returns.
