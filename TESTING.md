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
| whole gate | `make quality-gate` | about 30 s | nothing (known-answer is not a gate tier) |

For comparison `make test` (the whole pytest suite) is about 22 s parallel. Day to day
you run `make check` (the tests your diff touches or imports, `scripts/changed_tests.sh`, the
same selection pre-push uses); CI runs the full suite and `make ship` waits for it.

A skipped tier is not a pass.

`known-answer` is the assistant's quality report, not a gate tier: it grades the
live model's investigations, so its result moves with the model rather than the
commit (the same rows failed on framework v0.13.0 and v0.14.0, 84 conversations,
about 25 min at 3 jobs). Run it before an assistant release and read every
failure; a framework release does not wait on it.

## Write checks run offline first

A check that reads back what an approved write saved runs on a `writable: true`
fixture bundle (a sweep row graded by `world_after`, `writes_match_calls` and
`update_cards_show_overwrite`; `make chat-sweep CATEGORY=triage_act`) before it
goes to a box. The box refuses what the platform refuses: a value over its
field's length, a close with no closure reason, a picklist item the list does
not hold. `make live MODE=writes` keeps only what needs the platform:

| Live row | Why it stays live |
|---|---|
| `ztpf_author_render_interface_descriptions` | Runs `Render Script - Template` on a real device and compares the output with an oracle collected from that device; no fixture has a device or the playbook engine. |
| the sweep rows that carry `world_after` | They also run live to compare the saved record on a real box with the offline world; the offline pass is the gate. |

Of the three platform-side defects the live tier was built for, the fixture
reproduces two: a card that executes the call's preview (every long string
saved cut at 600 characters) shows as a saved value that differs from the
model's call, and an update card with no overwrite detail fails
`update_cards_show_overwrite`. `test_template` rendering against an empty
context stays live: the box's renderer is not the local sandbox the fixture
uses.

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
