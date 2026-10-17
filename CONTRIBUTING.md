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

- Read the [operating guide](docs/OPERATIONS.md), [security policy](SECURITY.md)
  and current worktree before editing. Preserve unrelated local changes.
- Use small patches with evidence and relevant regression coverage.
- Use a clean source checkout without a real `.env`, account database or private
  runtime data. Install both `requirements.lock` and `requirements-dev.lock`
  with `--no-deps --require-hashes`, after installing the pinned pip version
  from [Development checks](README.md#development-checks), then run
  `pip check`, both dependency audits, the offline tests, Ruff, JavaScript
  syntax checks and the publication check. UI changes also require
  `scripts/ui_smoke.py`.
- Use synthetic credentials and isolated data. Never send real orders,
  cancellations or provider LLM requests in automated tests.
- Review every staged file. Keep keys, `.env`, account data, logs, backups and
  local session files out of Git, including its history.
- Preserve `Preview -> Confirm`, stop protection, fresh risk gates and locks.

For runtime or UI changes, run targeted regression tests first, then the full
offline suite. Synchronize race tests with explicit events/barriers rather than
fixed sleeps. UI changes also need the responsive smoke with blocked states,
keyboard/modal interactions and network-context changes. Keep generated
screenshots and test artifacts out of the publication tree.

Documentation-only changes need factual/command checks, local link and anchor
validation, whitespace review and the publication check. They do not need a
local runtime test rerun unless behavior or test inputs change. GitHub's required
workflow still runs before a merge.

## Publishing official changes

Use a work branch, review the explicit staged file list and diff, commit and
push that branch, then open a pull request. Wait for all required checks on the
current PR head; the owner performs a regular merge. Fast-forward the local
`main` afterward. Do not force-push, bypass checks or weaken branch protection.
Never add private configuration, runtime data, backups or session material to
the index, including with force-add.

The official `main` branch requires a pull request with passing checks. Force
pushes and deletion are blocked; version tags matching `v*` cannot be rewritten
or deleted. Only the owner has repository write access. Administrative settings
remain under the owner's control.

The public repository includes the offline regression suite. Passing it checks
implemented behavior; it does not certify live exchange execution or returns.
