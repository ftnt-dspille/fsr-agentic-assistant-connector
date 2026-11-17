# connector-fsr-agentic-assistant -- project instructions

The in-platform FortiSOAR connector behind the Agentic assistant widget. It wraps
the `fsr_playbooks` framework (sibling repo `../../fsr-playbook-framework`,
published to PyPI as `fsr-playbooks`) and is pinned to an exact version of it in
`connector-fsr-agentic-assistant/requirements.txt`.

## Verify before you trust

- Trackers and docs drift. Before acting on "release owed" or "is broken", check the
  framework tag, this repo's `requirements.txt` pin, `info.json` and `git log`.
- Box, version and env state come from `make doctor` (`LOCAL=1` for no box,
  `RELEASE=1` for strict). Run it before believing any live result.
- `PLAN.md` here is reference only. It predates the rename, so don't plan from it.

## North star -- this repo is the engine

The widget is a window; **the connector does the work**. Deck thesis: **"AI finds
the pattern. A playbook runs it forever."** This repo owns three of the four
promises, so most product cards land here:

- **P1 investigate** -- ground on evidence, hunt SIEM/FAZ, enrich with **cited** verdicts.
- **P2 gating** -- tier >=3 suspends and cannot dispatch until a human decision is
  carried back. Structural, not a prompt instruction.
- **P3 reach** -- drive agent-bound on-prem targets and return the real result.
- (**P4 bottle it** lives mostly in the framework; `chat_turn` -> offer card is the delivery path.)

## Where work is tracked

- **Board:** https://github.com/users/ftnt-dspille/projects/1. **Claim a card before
  working it** (several agents run in parallel). Card ops: the framework's
  `scripts/tracker.sh`, never raw `gh issue`.
- **Roadmap:** `ROADMAP.md` in `~/PycharmProjects/agentic-assistant-tracker` is the source;
  edit it there, the user pushes. Box-specific plans: `fsr_all_widgets/docs/plans/` (gitignored).

## Deploying: `make deploy`. Releases are the user's.

```
make deploy              # ship the current pin (releases nothing)
make deploy FW=next      # release the next framework patch, then ship
make deploy FW=0.6.4     # release that exact version, then ship
make deploy FW=next BUMP=minor BOX=<alias>
```

Every step no-ops when already satisfied. Agents don't run this, or `make ship`,
unless the user asks. Run `make help` for the rest of the targets. Don't hand-run
`scripts/deploy.sh`, `scripts/deploy_chain.sh`, raw `ssh`, or ad-hoc `pyfsr`
snippets. The Makefile carries the box, env and venv wiring, and the deploy scripts
carry gates that a hand-run bypasses.

Why one command: every outage in the framework/connector seam was an **ordering**
mistake (shipping before PyPI served the pin; pinning a version missing an imported
symbol). `deploy` enforces release, PyPI poll, symbol-verified pin bump, ship.
Details: `RELEASING.md` in the framework repo.

### Never hand-install a wheel onto a box

No `ssh … pip install`, no `--find-links <install_dir>/wheels`. It papers over a bad
release and leaves a box no customer install can reproduce. If a dependency won't
install, the release is wrong; fix that and re-run. A `Connector.requirements_installed
= 'In-Progress'` flag is a symptom of a failed pip run, not the cause. Several built-in
connectors sit at `In-Progress` permanently and are fine.

## Python env: one declared env, one resolver

Every script, hook and make target gets its python from `scripts/dev_env.sh`
(`venv | python | check | status | setup`); never probe for a venv or hardcode
`.venv/bin/python` (`tests/test_dev_env_resolver.py` greps for it). The env is at
`$FSR_DEV_VENV`, default the main checkout's `.venv`; worktrees share it rather than
building their own (the framework is one editable sibling checkout either way).
`make setup` builds it (`uv`, from `pyproject.toml` + `requirements.txt`, framework
editable) and stamps the inputs' hash; `FRESH=1 make setup` recreates it. Missing,
unstamped or stale means a loud error naming that fix, never a fallback; `make doctor`
reports it. `box.sh --python` is a thin alias. `make test` prints its shuffle seed.

## Gates that must stay wired

- **The pin has ONE source:** the `fsr-playbooks==` line in `requirements.txt`.
  `operations.py` asserts the imported build matches it at runtime. The deploy scripts
  and `scripts/verify_workers.py` read it back from that same line. Never hardcode a copy.
- `scripts/ensure_framework.sh --mode preflight` runs before install (can the box's pip
  resolve the pin?). `--mode verify` runs after (did it land?).
- `scripts/verify_workers.py` asserts every worker is on `info.json`'s version and the
  pinned framework. Connector workers memoize the module at first import.
- `scripts/preflight_framework.py` (build time) checks the pinned wheel defines every
  symbol the connector imports from `fsr_playbooks`.

## Editing the framework

Edit `../../fsr-playbook-framework/fsr_playbooks/`, never a copy under this repo. Then
`make deploy FW=next` (user-run) releases it and ships in one go.

## Dev loop: box testing without a release

- `make loop` is the default. It runs local tests and a Frank smoke, pushes the working
  trees to the box, then runs a ~35s live smoke. `LOCAL_ONLY=1` skips the box.
- `make dev-push BOX=<alias>` copies working trees and recycles once (`scripts/dev_push.sh`).
- **A new operation needs an install, not a release.** FortiSOAR registers operations
  at install, so an op new in `info.json` returns "Operation matching query does not
  exist" after a plain copy. A bumped install registers it without the public release:
  `make ship BUMP=patch` (bump -> build -> platform install -> warmup re-syncs the op
  catalog). `dev_push.sh` detects a new op and AUTO-ESCALATES to that install, then
  re-applies the framework dev-patch (a connector install reinstalls the pinned
  fsr-playbooks wheel and would otherwise clear it).
- `make live MODE=quick` is a ~35s smoke of the installed connector.
- Before you believe a result from a box, run `make ship-dev ACTION=status`. A dev-patched box
  reports the wheel's version while it runs other code. `make ship-dev ACTION=restore` undoes it.

## Testing

The inner loop runs in-process with no box. Run `FSRPB_DEV=1 .venv/bin/python -m pytest`
for the suite. Never `uv run` (it builds an env without `fsr_playbooks`). To install
packages use `uv pip install --python .venv/bin/python ...`; plain `pip` fails here.

| target | tier | what it answers |
|---|---|---|
| `make tool-sweep` | T0, no LLM, ~2s | contract invariants over **every** tool (61). An LLM run can't guarantee it touched all of them. |
| `make test` | T0, deterministic | THE gate (also the ship gate, and what CI runs): whole suite, parallel, shuffled, 120s per-test timeout. `PYTEST_ARGS=tests/x.py` for a subset. |
| `make mutants` | T0, ~2-3 min | reverts each fix named in its manifest and expects its test red |
| `make local-turn` (`PLAYBOOK=` mounts a playbook) | T1, one turn | drive one turn by hand while iterating |
| `make t1-scenarios` | T1, single-turn invariants | `JOBS=`, `S=` |
| `make conv-suite` | T1, multi-turn | continuity, halt/resume, known-defect scenarios |
| `make chat-sweep` | T1-wide | breadth across surfaces. `SAMPLE=`, `CATEGORY=`, `JOBS=` |
| `make known-answer` | T1, verdicts | triage verdicts against recorded evidence (`MODEL=`, `RUNS=2`) |

Rules these tiers enforce:

- **LLM tiers default to `LLM=frank`** (needs `FRANK_MODEL`). `LLM=fake` returns a canned
  string, so use it only for a no-network smoke.
- **Frank is shared lab infrastructure.** `_MAX_JOBS = 3` in `scripts/t1_scenarios.py`
  caps concurrent sessions. Every runner goes through `cap_jobs`. Don't bypass it.
- **Model policy.** Frank first when reachable (VPN on); otherwise, or when the run
  needs the Anthropic side, paid Haiku without asking; live Sonnet for verification
  only; routine verification is fast (small samples, time budget); full sims only for
  sprint gates.
- **Native MCP needs `--mcp` locally.** Off the box the connector's MCP client is the
  on-box HMAC adapter, which returns nothing, so a plain local turn never sees an
  `mcp_*` tool. `scripts/local_turn.py --llm frank --tools live --mcp all` runs the
  real All-servers discovery against the box over pyfsr (or `--mcp soc,utility`).
- **Start sweeps small.** `make chat-sweep SAMPLE=1` (one row per surface), then
  `CATEGORY=`, then the full corpus. The sample is deterministic.
- **Two runs per arm, not five.** A defect seen twice is a defect. Two clean runs
  still don't prove a fix.
- **Re-grade, never re-drive.** `make regrade DUMP=<file>` re-grades stored transcripts
  with no model and no network, in about 0.2s. Re-driving the model to test a grader
  is wrong. A dump without transcripts is a hard error. Don't work around it.
- **Grade structure, never disposition.** Frank and the box model differ. Continuity and
  groundedness transfer between them; caution and verbosity don't.
- **Grade the defect, not the symptom.** A clean LLM run is not proof of a fix. Put
  determinism-requiring gates outside the LLM turn.
- **A grader must be able to fail.** Pin a passing and a failing case. Build the fixture
  by calling the producer (`pending_halt(...)`, the live tool schema), never by hand. A
  grader that reaches into a structure by name fails silently when the name changes.
  A grader that graded nothing is worse than none.
- **Red before fix.** A test for a fix must fail with the fix removed: revert it in a
  scratch worktree, see the test red, restore. Tests written from the same model as the
  fix inherit its blind spots. Add the fix to the `make mutants` manifest.
- **A skip is not a pass.** Report skipped tests as not run, with the reason. New skips
  must be listed in `tests/skip_allowlist.yaml`, or `make test` fails.
- **Order leaks.** `make test` shuffles. A test that patches a global, env var or
  ContextVar must undo it (fixture teardown or `monkeypatch`).
- **Shell.** Scripts use `#!/usr/bin/env bash`. zsh doesn't word-split `$FLAGS`, so
  `FLAGS="--model x"; cmd $FLAGS` passes one argument and quietly fakes a regression.
  Use arrays.
- **Shared-checkout WIP.** Never `git stash` or `pop` over another agent's uncommitted
  changes. Use `git worktree add` for branch work.

## Conversation driver

`scripts/drive.py` re-exports the shared loop from `fsr_playbooks.harness.drive`
(framework repo) and keeps the connector-only `mount_entity`. `LocalTransport`
stays in `scripts/local_conversation.py`. Change the loop in the framework, not here.
Until the next framework release bumps the pin, the connector cannot import
`fsr_playbooks.harness.drive`, so this change ships with that release.

## Guards and approvals

- The approval decision is one function: `fsr_playbooks/llm/authorization.py` `authorize()`
  (framework repo). Guards live in `fsr_playbooks/llm/agent_loop.py` and
  `fsr_playbooks/llm/_loop_helpers.py`. Before you add or remove one, read
  `docs/GUARDS.md` in the framework. Prefer a tool-contract change that makes the mistake
  impossible, and delete the guard it replaces.
- On build or fix-playbook turns, dispatch refuses non-playbook tools
  (`TurnPlan.gate_refusal`). Don't rely on prompt wording.
- No regex or phrase-list intent detection. Gate on structure: cards emitted, draft
  contents, grounded state.

## Public-repo hygiene and analyst copy

- No lab IPs, hostnames, box ports, credentials or capture dates in tracked files
  (comments and test headers included). Use placeholders; box details go only in
  gitignored `.env*`, `scratch/`, `docs/plans/` or memory.
- Text the analyst sees has no tool names, ids, JSON paths, emoji or em dashes; the
  widget renders what this connector emits.

## Which box a script talks to (no default box)

Every script that reaches a box resolves its target through `scripts/box.sh`, which asks
the pyfsr registry (`~/.pyfsr/instances.toml`, via `python -m pyfsr.cli instances env`).
Pass `BOX=<alias>`; `scripts/box.sh --list` prints the agent-targetable aliases. The env
file and the ssh target (the alias's `[instances.<alias>.appliance]` subtable) both come
from that one answer: box.sh writes an ssh config and puts an ssh/scp shim on PATH, so
`ssh "$BOX_SSH"` and rsync reach the box with no `~/.ssh/config` alias. A missing BOX, an
alias not on the list or not in the registry, or an ssh target with no ssh profile is
refused before anything runs; the first line printed is `>> target box: <alias>`. The old
`ENV=`/`FSR_ENV=`/`SSH_HOST=` knobs are retired (one release: humans get a warning and a
registered env file maps to its alias; agents are refused). Never add a default box to a
script or Makefile variable; add the box to the registry and `FSR_BOX_ALLOW` instead.
`make doctor` lists registry problems (alias missing, no answer, no ssh profile).
