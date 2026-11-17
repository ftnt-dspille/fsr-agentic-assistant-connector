"""P2 self-check: does the eval harness itself measure what it claims to?

The whole point of this plan is that a green test proved nothing about a broken
feature. So before any scenario trusts the harness, the harness gets graded the
same way it grades: against a live box, on an outcome, not on its own say-so.

This runs the full S2 SHAPE with the AI taken out of it — the edit is made
deterministically through pyfsr instead of by an agentic turn:

    deploy fixture -> clone -> run (marker=ALPHA)          behavioural oracle works
      -> snapshot BEFORE
      -> edit ALPHA->BRAVO via pyfsr                       a known-good edit
      -> snapshot AFTER
      -> diff (ONLY Emit.arguments changed)                diff oracle works
      -> run (marker=BRAVO)                                the oracle sees the change
      -> cleanup, and prove it cleaned up

A failure here is a harness bug. S2 is this script with step 4 replaced by a
build-intent chat turn — so if S2 then fails, the finding is about the assistant,
which is the only reason any of this exists.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.206; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_selfcheck.py
"""
from __future__ import annotations

import os
import sys

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_harness import (  # noqa: E402
    Checks, EvalHarness, assert_diff_only, step_names,
)

FIXTURE = "marker_emitter.yaml"
PB = "ZZ Eval Marker Emitter"
EMITTED = "ZZ Eval emitted"
EMIT_STEP = "Emit"
V1, V2 = "ALPHA", "BRAVO"


def _set_marker(h: EvalHarness, wf_uuid: str, marker: str) -> None:
    """The known-good edit: one field, through pyfsr's own update path.

    pyfsr's `update()` works where the connector's once didn't, because its steps
    come from a GET and already carry `@id` — the identity the platform decides
    create-vs-update on. That is exactly why this is the control: it isolates a
    harness bug from a connector bug.
    """
    wf = h.client.playbooks.get_definition(wf_uuid, relationships=True).to_dict(by_alias=True)
    for step in wf["steps"]:
        if step["name"] == EMIT_STEP:
            step["arguments"]["resource"]["description"] = marker
    h.client.playbooks.update(wf_uuid, steps=wf["steps"])


def main() -> int:
    checks = Checks("P2 harness self-check")
    h = EvalHarness(checks)
    checks.note(f"box={h.live.base} connector={h.live.version}")

    try:
        h.deploy(FIXTURE)
        wf = h.clone(PB, f"{PB} selfcheck {h.stamp}")
        wf_uuid = wf["uuid"]

        # 1. behavioural oracle: the clone emits what the fixture ships with.
        got = h.emit_marker(wf_uuid, module="alerts", record_name=EMITTED)
        checks.eq(got, V1, "fixture's emitted marker")

        # 2. freeze, edit, freeze. The BEFORE snapshot is also the name map's
        #    source — a step the edit removed has no name on the after side.
        before = h.snapshot(wf_uuid, note=f"selfcheck-before-{h.stamp}")
        names = step_names(h.version_json(before.uuid))
        checks.ok(EMIT_STEP in names.values(),
                  f"snapshot blob has no {EMIT_STEP!r} step — it read back empty "
                  f"(get_version withholds `json` without $includeData=true)")

        _set_marker(h, wf_uuid, V2)
        after = h.snapshot(wf_uuid, note=f"selfcheck-after-{h.stamp}")

        # 3. diff oracle: it must see the asked-for change AND nothing else.
        diff = h.diff(before.uuid, after.uuid)
        assert_diff_only(checks, diff, names,
                         changed_steps={EMIT_STEP: {"arguments"}})
        checks.note(f"diff: changed={[(names.get(d.step), d.field) for d in diff.changed]} "
                    f"added={len(diff.added)} removed={len(diff.removed)}")

        # 4. and the change is real where it counts — in a RUN, not a diff.
        got = h.emit_marker(wf_uuid, module="alerts", record_name=EMITTED)
        checks.eq(got, V2, "emitted marker after the edit")
    finally:
        h.cleanup()

    # 5. cleanup is a claim like any other: check it. Both halves matter — a
    #    leftover alert would be read as a later run's own output, and the run
    #    DELETE route was inferred, so an unverified "no exception" is not
    #    evidence it removed anything.
    leftover = h.existing("alerts", EMITTED)
    checks.ok(not leftover,
              f"cleanup left {len(leftover)} {EMITTED!r} alert(s) behind — "
              f"a later run would read one of these as its own output")

    checks.ok(h.runs, "no runs were registered for cleanup — emit_marker ran twice, "
                      "so the pk never made it off the trigger response")
    still = [pk for pk in h.runs if h.run_exists(pk)]
    checks.ok(not still,
              f"cleanup left run(s) {still} on the box — DELETE /api/wf/api/workflows/<pk>/ "
              f"returned success without deleting")

    return checks.report()


if __name__ == "__main__":
    sys.exit(main())
