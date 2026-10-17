Official changes are maintained by Chap0815. External code pull requests are
not accepted; please use an issue for suggestions or reproducible defects.

## Problem and change

Describe the concrete behavior and the smallest change that addresses it.

## Verification

List relevant tests and results, any migration consequences, and remaining
limitations. Never include secrets, live account data or real order requests.

- Runtime/UI change: targeted regressions, full isolated offline suite and
  applicable responsive smoke.
- Documentation-only change: factual commands, links/anchors, whitespace and
  publication boundary; required CI must still pass.
- Confirm the explicit staged scope excludes `.env`, runtime/account data,
  generated artifacts and session material.

Passing offline checks does not certify live trading or profitability. Follow
the [maintainer workflow](https://github.com/Chap0815/ObsidianLiveTrader/blob/main/CONTRIBUTING.md); merge normally after the current
PR head's required checks pass.
