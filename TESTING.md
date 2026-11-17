# Connector testing notes

The cross-repo guide is `TESTING.md` in the fsr_all_widgets checkout. This file
covers the connector's release gates.

## Release quality gate (box-free tiers)

A connector release (`make deploy FW=...`, or any install with
`FSR_RELEASE_INSTALL=1`) refuses unless the current commit has a quality stamp.
`make quality-gate` runs the three tiers below on a clean tree and writes the
stamp, keyed by the commit sha, under the git common dir (shared by worktrees,
never tracked). Any new commit, or a dirty tracked tree, voids it.
`python3 scripts/quality_gate.py check` is the gate itself (exit 1 without a
stamp); `scripts/deploy_chain.sh` runs it before any PyPI step and
`scripts/deploy.sh` re-checks it.

Measured run times (one laptop, warm env, 10 cores):

| Tier | Command | Time | Needs |
|---|---|---|---|
| tool-sweep strict | `make tool-sweep STRICT=1` | about 8 s | nothing |
| t1-scenarios, fake model | `make t1-scenarios LLM=fake` | about 11 s | nothing; contract and structural graders only (33 disposition graders report NOT RUN) |
| known-answer | `make known-answer LLM=frank` | about 8.3 min (497 s; 12 scenarios x 2 runs, 3 jobs) | the free gateway; the model is stochastic, so a run can fail a scenario on model noise |
| whole gate | `make quality-gate` | about 8.5 min | the free gateway |

For comparison `make test` (pytest, already part of every ship) is about 22 s
parallel.

A skipped tier is not a pass. `known-answer` on the free model failed 3 of 24
conversations in the measuring run (a verdict card not delivered, and two
policy-confidence results just under the 0.9 threshold); the gate treats that as
red, so re-run or fix the cause rather than bypass.

## Skipping a gate on the release path

Every skip flag needs a reason, is appended to `.ship-ungated.log` (gitignored)
and is listed in the final summary of the run (`== release summary: GATES
SKIPPED ==`). Without the reason variable the run exits 1 before building or
installing anything. Implementation: `scripts/release_skips.sh`.

| Skip | Reason variable |
|---|---|
| `--no-test`, `SKIP_TESTS=1` | `SKIP_TESTS_REASON` |
| `--no-verify`, `NOVERIFY=1` | `SKIP_VERIFY_REASON` |
| `--no-warmup` | `SKIP_WARMUP_REASON` |
| `--no-deps` | `SKIP_DEPS_REASON` |
| `--skip-version-check` | `SKIP_VERSION_CHECK_REASON` |
| `SKIP_LIVE_SANITY=1` | `SKIP_LIVE_SANITY_REASON` |
| `SKIP_CI_WATCH=1` (framework release) | `SKIP_CI_WATCH_REASON` |
| `SKIP_QUALITY_GATE=1` | `SKIP_QUALITY_GATE_REASON` |

`--no-install` only builds and is not a skip. The dev loop's own switches
(`SKIP_LOCAL`, `NO_AGENT`) are not on the release path and are unchanged.
