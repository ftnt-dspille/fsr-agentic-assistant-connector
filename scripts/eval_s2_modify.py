"""S2 — can the assistant modify an existing playbook correctly?

The first scenario that grades the ASSISTANT rather than the plumbing. Same
shape as `eval_selfcheck.py`, with one substitution: the edit is made by a real
build-intent agentic turn instead of by pyfsr. So when the self-check is green
and this is red, the finding is about the assistant — which is the only reason
any of this exists.

    deploy fixture -> clone -> run (marker=ALPHA)
      -> ASK: "make it emit BRAVO"                    a build turn, on a real box
      -> extract the assistant's YAML, compile, update    the widget's push path
      -> the AI's own ai-pre-edit snapshot            P1 regression, for free
      -> snapshot AFTER
      -> diff (ONLY Emit.arguments changed)           did it change what was asked
      -> run (marker=BRAVO)                           and does the playbook RUN

S2 first, of the eight, because it has the sharpest oracle: the ask is a single
field, so anything else the diff shows is collateral damage, unambiguously.

**The assistant does not write; the WIDGET does.** `update_playbook` is a
connector operation the widget calls when the user hits push — it is in no
intent's tool slice, so the assistant cannot call it and never will. The first
draft of this scenario asserted "no snapshot => the AI never wrote" and reported
0/3 against the assistant; it was grading it for something structurally
impossible. This script therefore does what `view.controller.js#pushPlaybook`
does: take the assistant's YAML, `compile_yaml`, then `update_playbook` with the
open playbook's IRI. The assistant is graded on the YAML it authored — which is
the only thing it actually controls.

The BEFORE side of the diff is deliberately the snapshot the CONNECTOR took, not
one this script takes. That makes a passing S2 prove two things at once: the edit
landed, and a rollback point exists — P1's fail-closed gate, regression-tested by
a scenario that is not about it.

**LLM turns are stochastic.** One green run is not a fix and one red run is not
a defect — use RUNS=n and grade the pattern, not the sample.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.206; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_s2_modify.py [--runs 3]
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid as _uuid

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_canon  # noqa: E402

from eval_harness import (  # noqa: E402
    Checks, EvalHarness, assert_diff_only, step_names,
)

from drive import detached_turn  # noqa: E402
from fsr_playbooks.harness.frames import assistant_text  # noqa: E402

FIXTURE = "marker_emitter.yaml"
PB = "ZZ Eval Marker Emitter"
EMITTED = "ZZ Eval emitted"
EMIT_STEP = "Emit"
V1, V2 = "ALPHA", "BRAVO"

# The ask. Deliberately phrased the way an analyst would: it names the OUTCOME
# ("the alert should say BRAVO"), not the mechanism ("set steps[1].arguments.
# resource.description"). An ask that spells out the mechanism grades the
# assistant's typing, not its understanding.
ASK = (f"The alert this playbook creates is stamped {V1}. Change it so it says "
       f"{V2} instead. Leave everything else exactly as it is.")

# --ground: the CONTROL for S2's headline finding, and now the rehearsal of its fix.
#
# The build slice has no tool that can read a live playbook — get_record and
# search_module_records are excluded from it by C5's triage-only scoping, and
# search_playbooks hits an offline pattern corpus, not the box. So the assistant
# is asked to edit a playbook it cannot see, and every ungrounded run dead-ends
# calling analyze_playbook (yaml_text) with nothing to pass it.
#
# This flag hands it the YAML a read tool WOULD have returned. It changes
# nothing else. If grounded runs pass and ungrounded ones fail, the finding is
# the missing read path — not the model, and not the prompt. That distinction is
# the difference between a fix and a guess, which is why P4 sits after S2.
#
# The YAML is DECOMPILED FROM THE LIVE PLAYBOOK, not read off the fixture file:
# that is byte-for-byte what `decompile_playbook` (the new connector op the
# widget calls at designer mount) puts in front of the model, so a green
# grounded run rehearses the shipped path rather than an idealised one. It also
# matters for correctness — the decompile carries the CLONE's real name, whereas
# the fixture file carries the original's, and that difference alone decides
# whether the push 409s (F2).
GROUND_PREFIX = """Here is the current YAML of the playbook you have open:

```yaml
{yaml}
```

"""


def _decompile_live(h: EvalHarness, wf_uuid: str) -> str:
    """The open playbook as authored YAML — what `decompile_playbook` returns.

    Run in-process against the live record rather than through the connector op,
    so the rehearsal needs no ship. Same helper, same decompiler, same live
    `?$relationships=true` shape the op reads.
    """
    from fsr_playbooks.compiler.decompiler import decompile_to_yaml
    from fsr_playbooks.llm.tools import _DB_PATH as db

    live = h.definition(wf_uuid)
    coll = live.get("collection")
    if isinstance(coll, str):
        coll = h.client.get(coll)
    coll = coll if isinstance(coll, dict) else {}
    envelope = {"type": "workflow_collections", "data": [{
        "name": coll.get("name") or "",
        "description": coll.get("description") or "",
        "visible": bool(coll.get("visible", True)),
        "workflows": [live],
    }]}
    return decompile_to_yaml(envelope, db)


def _entity(h: EvalHarness, wf_uuid: str, playbook_yaml: str = "") -> dict:
    """The widget-shaped record context for the open playbook.

    This is how the designer grounds a build turn (contract §2): module
    `workflows`, plus the iri the assistant passes to `update_playbook`. Without
    it the turn has no idea which playbook is open — which is the whole premise
    the build persona is allowed to assume.

    ``playbook_yaml`` mirrors exactly what the shipped widget seeds on every
    build turn (`entity.playbook_yaml`, read into the connector's OPEN PLAYBOOK
    block). Passing it here is the on-box proof of the REAL grounding channel —
    stronger than ``--ground``, which injects the YAML through the prompt.
    """
    rec = h.client.get(h.iri(wf_uuid))
    entity = {
        "iri": h.iri(wf_uuid),
        "module": "workflows",
        "uuid": wf_uuid,
        "id": rec.get("id"),
        "fields": {"name": rec.get("name"), "description": rec.get("description")},
    }
    if playbook_yaml:
        entity["playbook_yaml"] = playbook_yaml
    return entity


_said = assistant_text  # the shared reader: streamed tokens joined, stripped


def _tools(transcript: list) -> list[str]:
    """Which tools the turn actually called. When a scenario fails because
    nothing was written, this is the first question: did it try?"""
    # Constituent names: a union call (emit_card / find) is graded as what
    # it did -- see eval_canon.
    return eval_canon.tool_names(transcript)


# The turn reader (fsr_playbooks.harness.frames) owns the delivered-playbook
# rules. Re-exported here so existing importers (t1_scenarios, conv_scenarios,
# the S6 harness) keep their names.
from fsr_playbooks.harness.frames import (  # noqa: E402,F401 -- re-exported
    YAML_CARRIER as _YAML_CARRIER,
    delivered_yaml,
    extracted_yaml as _extract_yaml,
    is_offer_call as _is_offer_call,
    refused_tool_use_ids as _refused_tool_use_ids,
)


def _push(h: EvalHarness, yaml_text: str, wf_uuid: str, session_id: str) -> dict:
    """The push button: compile_yaml -> update_playbook against the open IRI.

    Exactly `fsrPbAgent.service.js#updatePlaybook`. A compile failure here is an
    assistant finding (it authored YAML that does not compile), not a harness
    one — so it is reported, not raised.
    """
    compiled = h.live.execute("compile_yaml", {"yaml": yaml_text})
    if not compiled.ok or not (compiled.data or {}).get("workflow_json"):
        h.checks.failures.append(
            f"the assistant's YAML does not compile: "
            f"{compiled.error or (compiled.data or {}).get('errors')}")
        return {}
    res = h.live.execute("update_playbook", {
        "workflow_json": compiled.data["workflow_json"],
        "workflow_iri": h.iri(wf_uuid),
        "session_id": session_id,
    })
    if not res.ok:
        h.checks.failures.append(f"update_playbook failed: {res.error or res.data}")
        return {}
    h.checks.note(f"update_playbook ok method={(res.data or {}).get('method')} "
                  f"version={(res.data or {}).get('version_uuid')}")
    return res.data or {}


def _build_turn(h: EvalHarness, entity: dict, session_id: str, timeout: int,
                *, ground: str = "") -> list:
    """Drive ONE build-intent turn the way the widget does: detached chat_turn +
    a since_turn-fenced chat_poll."""
    ask = (GROUND_PREFIX.format(yaml=ground) + ASK) if ground else ASK
    turn, transcript, stop = detached_turn(
        h.live, session_id=session_id, intent="build", entity=entity,
        messages=[{"role": "user", "content": ask}], timeout=timeout)
    h.checks.note(f"turn={turn} stop={stop} tools={_tools(transcript)}")
    h.checks.note(f"assistant: {_said(transcript)[:600]}")
    return transcript or []


def _once(run_idx: int, timeout: int, ground: bool, widget_path: bool = False) -> int:
    label = "S2 modify an existing playbook"
    mode = (", GROUNDED control" if ground else
            ", WIDGET-PATH entity.playbook_yaml" if widget_path else "")
    checks = Checks(f"{label} (run {run_idx}{mode})")
    h = EvalHarness(checks)
    session_id = f"eval-s2-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    checks.note(f"box={h.live.base} connector={h.live.version} session={session_id}")

    try:
        h.deploy(FIXTURE)
        wf = h.clone(PB, f"{PB} s2 {h.stamp}-{run_idx}")
        wf_uuid = wf["uuid"]

        # Precondition, not a check: if the clone doesn't emit ALPHA the ask
        # below is meaningless, so fail loudly here rather than blame the AI.
        got = h.emit_marker(wf_uuid, module="alerts", record_name=EMITTED)
        if not checks.eq(got, V1, "fixture's emitted marker (PRECONDITION)"):
            return checks.report()

        # The YAML the widget/decompiler would surface. --ground injects it via
        # the prompt (rehearsal); --widget-path carries it as entity.playbook_yaml
        # (the real shipped channel); neither = the ungrounded control.
        yaml_seed = _decompile_live(h, wf_uuid) if (ground or widget_path) else ""
        entity = _entity(h, wf_uuid,
                         playbook_yaml=(yaml_seed if widget_path else ""))
        transcript = _build_turn(h, entity, session_id, timeout,
                                 ground=(yaml_seed if ground else ""))

        # 1. the assistant's deliverable: YAML for the revised playbook. No
        #    fence => nothing for the push button to push, so the turn produced
        #    nothing usable however good the prose was.
        yaml_text = _extract_yaml(transcript)
        if not checks.ok(yaml_text,
                         "the turn emitted no ```yaml fence — there is nothing for "
                         "the push button to push"):
            return checks.report()
        if not _push(h, yaml_text, wf_uuid, session_id):
            return checks.report()

        # 2. P1, for free: the connector must have taken a restore point BEFORE
        #    it wrote. update_playbook is fail-closed on the snapshot, so an ok
        #    with no snapshot means the gate is open.
        snaps = h.ai_snapshots(wf_uuid)
        if not checks.ok(snaps, "update_playbook reported ok but left no ai-pre-edit "
                                "snapshot — the write had no rollback point (P1 gate open)"):
            return checks.report()
        before = snaps[0]
        checks.ok(session_id in str(before.get("note") or ""),
                  f"snapshot note {before.get('note')!r} is not attributable to "
                  f"this session")

        names = step_names(h.version_json(before["uuid"]))
        checks.ok(EMIT_STEP in names.values(),
                  f"the restore point has no {EMIT_STEP!r} step — it froze the wrong "
                  f"thing, so it would not restore the user's work")

        # 2. did it change exactly what was asked?
        after = h.snapshot(wf_uuid, note=f"s2-after-{h.stamp}")
        diff = h.diff(before["uuid"], after.uuid)
        checks.note(f"diff: changed={[(names.get(d.step), d.field) for d in diff.changed]} "
                    f"added={len(diff.added)} removed={len(diff.removed)}")
        assert_diff_only(checks, diff, names, changed_steps={EMIT_STEP: {"arguments"}})

        # 3. the bar: the playbook RUNS, and does the asked-for thing.
        got = h.emit_marker(wf_uuid, module="alerts", record_name=EMITTED)
        checks.eq(got, V2, "emitted marker after the AI's edit")
    finally:
        h.cleanup()

    return checks.report()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat n times; an LLM turn is stochastic, so a single "
                         "sample grades nothing")
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--ground", action="store_true",
                    help="CONTROL: hand the assistant the YAML a read tool would "
                         "have returned, via the prompt. Isolates the missing read "
                         "path from the model and the prompt.")
    ap.add_argument("--widget-path", action="store_true",
                    help="PROOF of the shipped path: seed entity.playbook_yaml "
                         "exactly as the deployed widget does (read into the "
                         "connector's OPEN PLAYBOOK block). Needs the F1+F3 "
                         "connector + the widget's grounding contract on the box.")
    args = ap.parse_args(argv)
    if args.ground and args.widget_path:
        ap.error("--ground and --widget-path are mutually exclusive")

    codes = [_once(i, args.timeout, args.ground, args.widget_path)
             for i in range(1, args.runs + 1)]
    passed = codes.count(0)
    if args.runs > 1:
        print(f"\n=== S2: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
