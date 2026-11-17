"""S6 — does the assistant's proposed fix, applied, make the playbook RUN?

S5's sibling and its natural next step. S5 grades the DIAGNOSIS (the assistant
names the failing step and the root cause); S6 grades the FIX (apply what it
proposes and the playbook does the thing it couldn't before): from "here's why
it's broken" to a green run.

    deploy broken fixture -> clone -> run  (it FAILS, creates nothing)   the baseline
      -> ASK: "why did it fail, and can you fix it so it runs?"          a build turn
      -> extract the assistant's corrected YAML, compile, update         the widget push path
      -> the connector's ai-pre-edit snapshot                           P1 regression, free
      -> diff (ONLY Emit.arguments changed)                             minimal fix, no collateral
      -> run again -> it creates a record (uuid diff)                  THE BAR: it now runs

**The bar is behavioural, not the diagnosis.** Whether the turn calls
`why_did_playbook_fail` is S5's question; S6 asks only whether the YAML it hands
back, pushed and run, produces the record the broken one never did. So the
diagnosis tool call is NOTED, not graded — the oracle is the created record.

**Why S6 does NOT share S5's fixture — the box taught this the hard way.** S5's
anchor (`broken_create_record_bad_args.yaml`) puts `resource:` OUTSIDE
`arguments:`, and that key is DESTROYED at DEPLOY-compile time (the parser drops
it). The stored playbook, and therefore the `decompile_playbook` seed the model
sees, then has no record body at all — so applying the fix there cannot recreate
the original record: its name/fields exist nowhere, and the model is forced to
INVENT a body. Live on 206, three runs: the model correctly nested a `resource:`
every time, but half the time invented a placeholder (`field_name: field_value`)
that fails at runtime — the run went 1/3 green, grading the model's GUESS, not
its fix. (It also fixates on an `ALPHA`->`BRAVO` rename found nowhere in the
prompt or seed — pure confabulation, and it ignores any name the ASK tries to
pin.) So S6 uses `broken_create_record_missing_module.yaml`: the body is intact
and only Emit's `module:` is missing. The fix is purely STRUCTURAL — add
`module: alerts` — and the record it produces is DETERMINISTIC.

**The oracle is a uuid diff, because the box model renames the record.** The
fix is a one-key add (`module`), and the fixture carries the body intact, so in
principle the created record is deterministic (`ZZ Eval emitted`). In practice
the box's model compulsively rewrites the record body anyway — a confabulated
`ALPHA`→`BRAVO` rename it applies no matter what the prompt says — so a name-based
oracle is defeated. S6 therefore grades the BEHAVIOUR: the fixed run creates a
record (any record) that the broken run did not, detected by uuid diff across the
run and swept by uuid. In compiled step terms adding `module` changes Emit's
`arguments` and nothing else (`create_record`/`insert_record` share one step-type
uuid, so the type rename the model often makes is not a diff), so
`assert_diff_only` still checks "did it fix ONLY the bug, or rewrite the
playbook" — S2's collateral check.

**Why the box path, not S5's in-process turn.** S5 runs in-process because it was
written before `why_did_playbook_fail` shipped; it is on the box now (0.4.75), so
S6 drives the real deployed worker via `chat_turn` — the shipped-worker proof,
reusing S2's `_build_turn`/`_push`/`_extract_yaml` verbatim so the two write
scenarios stay honest about what "the widget's push" means.

**Widget-faithful context.** The entity seeds `playbook_yaml` exactly as the
shipped widget does (the `decompile_playbook` channel F1 added). The model needs
it twice over: to see the missing `module:` and to author a corrected YAML that
is the SAME playbook plus that one key — including the record body it must keep.

**LLM turns are stochastic.** Grade the pattern over RUNS=n, not one sample.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.206; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_s6_apply_fix.py [--runs 3]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid as _uuid

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_harness import (  # noqa: E402
    Checks, EvalHarness, assert_diff_only, step_names,
)
# Reuse S2's write path verbatim (the widget's push button) and S2's entity /
# decompile grounding. Same rails => S6 can only diverge from S2 in the ASK and
# the oracle, which is exactly what distinguishes "fix a broken playbook" from
# "modify a working one".
from eval_s2_modify import (  # noqa: E402
    _build_turn, _decompile_live, _entity, _extract_yaml, _push, _tools,
)

FIXTURE = "broken_create_record_missing_module.yaml"
PB = "ZZ Eval Missing Module"
EMIT_STEP = "Emit"
MODULE = "alerts"  # the module Emit's create_record writes to

# A failed run reaches one of these; `finished` does not. The broken fixture's
# Emit errors at runtime, so a healthy precondition run lands here — if it
# doesn't, the fixture or the box changed and there is nothing to fix.
FAILING_STATUSES = {"failed", "finished_with_error", "terminated", "errored"}

# The ask. How an analyst actually talks: names the SYMPTOM ("it failed, didn't
# create the record") and asks to fix it — never the mechanism ("module is
# missing"), and never "give me the YAML" (emitting the corrected playbook is the
# build persona's job, not something a user requests; S2's ask doesn't mention
# YAML either). "without changing anything else" keeps the diff oracle sharp
# without leaking the fix.
ASK = ("The last run of this playbook failed and it didn't create the record "
       "it's supposed to. Why did it fail, and can you fix it so it runs "
       "without changing anything else?")


def _record_uuids(h: EvalHarness) -> set[str]:
    """Every record uuid currently in MODULE.

    S6 detects the created record by uuid difference across the run, NOT by name:
    the box's model compulsively rewrites the record body (a confabulated
    ALPHA→BRAVO rename it applies regardless of the prompt), so the created
    record's name/fields are not predictable. The bar is behavioural — a record
    the broken run did not make — not its contents, so a uuid diff is the right,
    rename-proof oracle. The box is a quiet dev appliance and the window is a few
    seconds, so a concurrent unrelated record is not a real risk.
    """
    rows = h.client.get(f"/api/3/{MODULE}", params={"$limit": 500}
                        ).get("hydra:member") or []
    return {r["uuid"] for r in rows if r.get("uuid")}


def _new_records(h: EvalHarness, before: set[str]) -> set[str]:
    """Records that appeared since `before`, registered for teardown by uuid so
    cleanup is exact whatever the model named them."""
    fresh = _record_uuids(h) - before
    for u in fresh:
        h._records.append((MODULE, u))
    return fresh


def _once(run_idx: int, timeout: int) -> int:
    checks = Checks(f"S6 apply the fix so the playbook runs (run {run_idx})")
    h = EvalHarness(checks)
    session_id = f"eval-s6-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    checks.note(f"box={h.live.base} connector={h.live.version} session={session_id}")

    try:
        h.deploy(FIXTURE)
        wf = h.clone(PB, f"{PB} s6 {h.stamp}-{run_idx}")
        wf_uuid = wf["uuid"]

        # PRECONDITION, not a check: the fixture must be broken the way S6
        # assumes — the run fails AND creates no record at all. If either is
        # false the scenario has nothing to fix; that is a harness/box problem,
        # reported loudly, not an AI verdict.
        before_recs = _record_uuids(h)
        summary = h.run(wf_uuid)
        status = getattr(summary, "status", "?")
        time.sleep(3)  # commit lag, same reason as emit_marker's _COMMIT_LAG_S
        if not checks.ok(status in FAILING_STATUSES,
                         f"PRECONDITION: the broken fixture's run did not fail "
                         f"(status={status!r}); nothing to fix"):
            return checks.report()
        if not checks.ok(not _new_records(h, before_recs),
                         "PRECONDITION: the broken fixture already created a record "
                         "— it is not broken the way S6 needs; the fix would be a no-op"):
            return checks.report()

        # Widget-faithful context: iri + module + the decompiled OPEN PLAYBOOK
        # the shipped widget seeds. The body is intact in this seed; the model
        # reads it and authors a copy with the missing `module` restored.
        entity = _entity(h, wf_uuid, playbook_yaml=_decompile_live(h, wf_uuid))
        transcript = _build_turn(h, entity, session_id, timeout)
        checks.note(f"tools called: {_tools(transcript)}")  # noted, not graded (S5's bar)

        # 1. the deliverable: corrected YAML for the push button.
        yaml_text = _extract_yaml(transcript)
        if not checks.ok(yaml_text,
                         "the turn emitted no ```yaml fence — there is nothing to "
                         "push, so no fix was applied"):
            return checks.report()

        # 2. the push: compile + update. A compile failure here is an assistant
        #    finding — it authored a fix that does not even compile.
        if not _push(h, yaml_text, wf_uuid, session_id):
            return checks.report()

        # 3. P1, for free: update_playbook must have taken a restore point before
        #    writing. Same fail-closed gate S2 regression-tests.
        snaps = h.ai_snapshots(wf_uuid)
        if not checks.ok(snaps, "update_playbook reported ok but left no ai-pre-edit "
                                "snapshot — the write had no rollback point (P1 gate open)"):
            return checks.report()
        before = snaps[0]

        # 4. minimal fix: the corrected YAML changed ONLY Emit's arguments (it
        #    added the missing `module:`) and touched nothing else. A turn that
        #    "helpfully" rewrote other steps fails here even if the run goes green.
        names = step_names(h.version_json(before["uuid"]))
        after = h.snapshot(wf_uuid, note=f"s6-after-{h.stamp}")
        diff = h.diff(before["uuid"], after.uuid)
        checks.note(f"diff: changed={[(names.get(d.step), d.field) for d in diff.changed]} "
                    f"added={len(diff.added)} removed={len(diff.removed)}")
        assert_diff_only(checks, diff, names, changed_steps={EMIT_STEP: {"arguments"}})

        # 5. THE BAR: run the fixed playbook — it now creates a record the broken
        #    run never did. Detected by uuid diff (rename-proof), not by name.
        before_fixed = _record_uuids(h)
        fixed_summary = h.run(wf_uuid)
        time.sleep(3)
        made = _new_records(h, before_fixed)  # registers created records for teardown
        checks.note(f"fixed run status={getattr(fixed_summary, 'status', '?')} "
                    f"created {MODULE}: {made or '{}'}")
        checks.ok(made,
                  f"the fixed playbook created no {MODULE} record — the assistant's "
                  f"corrected YAML did not make create_record write a record (the "
                  f"broken run created nothing; the fix must)")
    finally:
        h.cleanup()

    return checks.report()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat n times; an LLM turn is stochastic, so a single "
                         "sample grades nothing")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args(argv)

    codes = [_once(i, args.timeout) for i in range(1, args.runs + 1)]
    passed = codes.count(0)
    if args.runs > 1:
        print(f"\n=== S6: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
