#!/usr/bin/env python
"""S-effect: the runtime last mile of effect verification.

The library's `verify_playbook` can now say WHAT a playbook writes and WHERE each
written value comes from (`evidence.effects` -- the effect inventory). What it
cannot prove, by design, is that the value actually lands on the record at run
time: static analysis with branch-pinning reaches every step, so "does the
verdict reach the alert" has no offline answer. This closes that loop against a
real box.

The shape, end to end:
  1. A fixture playbook computes a verdict (a set_variable derived from the run
     input) and writes it into a NEW alert's `description` via create_record.
  2. We run `verify_playbook` LOCALLY and read the inventory: it must report the
     create_record, the field it writes, and that the field is fed by the
     compute step -- not a literal. The inventory is what tells us which field
     to read back.
  3. We push + activate the playbook on the box, run it with a known input, and
     read the created alert's `description` back. It must equal the verdict the
     compute step derived from that input -- proving the value flowed through
     the step chain into the persisted record, not just that a record appeared.

No LLM: the playbook is a fixture, so this is free and deterministic. No
containment: the only effect is a throwaway alert, hard-deleted on teardown.

Run: `make eval-effect` (or `python scripts/eval_effect_readback.py`).
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_harness import Checks, EvalHarness  # noqa: E402
from eval_s1_create import _push  # noqa: E402

# The input the run carries; the compute step folds it into the verdict so the
# read-back value is unique to THIS run (a stale leftover can't pass as fresh).
_TAG = "CHARLIE"


def _fixture(record_name: str) -> str:
    """A compute-then-write playbook. `Decide` derives a verdict from the run
    input; `Emit` writes it into a new alert's description. The link
    `Emit.description <- Decide` is exactly what the inventory should report."""
    return f"""
collection: Effect Readback Eval
playbooks:
  - name: Effect Readback
    steps:
      - {{name: Start, type: start_on_create, module: alerts, next: Decide}}
      - name: Decide
        type: set_variable
        vars:
          verdict: "verdict-{{{{ vars.inputs.tag }}}}"
        next: Emit
      - name: Emit
        type: create_record
        module: alerts
        resource:
          name: "{record_name}"
          description: "{{{{ vars.steps.Decide.verdict }}}}"
        next: Done
      - {{name: Done, type: end}}
"""


def _inventory(yaml_text: str, checks: Checks) -> dict | None:
    """Run the library gate locally and return the record-write entry whose
    value is fed by an upstream step -- the inventory half of the proof."""
    try:
        from fsr_playbooks.mcp_server.tools_verify import verify_playbook
    except Exception as e:  # noqa: BLE001
        checks.failures.append(f"could not import verify_playbook locally: {e}")
        return None
    r = verify_playbook(yaml_text)
    effects = (r.get("evidence") or {}).get("effects") or []
    writes = [e for e in effects if e.get("kind") == "record_write"]
    if not checks.ok(writes, "verify_playbook reported a record-write effect"):
        return None
    w = writes[0]
    checks.eq(w.get("module"), "alerts", "inventory: the write targets alerts")
    checks.eq(w.get("writes", {}).get("description"), ["Decide"],
              "inventory: description is fed by the Decide step (not a literal)")
    checks.ok("Decide" in (w.get("sources") or []),
              "inventory: Decide is a source feeding the write")
    return w


def _once(run_idx: int, timeout: int) -> int:
    checks = Checks(f"S-effect runtime effect readback (run {run_idx})")
    h = EvalHarness(checks)
    record_name = f"effect-readback-{uuid.uuid4().hex[:8]}"
    yaml_text = _fixture(record_name)
    try:
        # 1. the library half: the inventory names the field to read back.
        w = _inventory(yaml_text, checks)
        if w is None:
            return checks.report()
        field = next((f for f, src in w["writes"].items() if src), None)
        if not checks.ok(field, "inventory names a linked field to read back"):
            return checks.report()

        # 2. push + activate on the box.
        session_id = f"effect-readback-{run_idx}"
        wf_uuid = _push(h, yaml_text, session_id)
        if not wf_uuid:
            return checks.report()
        try:
            h.client.playbooks.update(wf_uuid, isActive=True)
        except Exception as e:  # noqa: BLE001
            checks.note(f"activate warning (continuing): {e}")

        # 3. the bar: run it, read the inventory's field back off the created
        #    record, and confirm the value the compute step derived landed.
        time.sleep(1)
        got = h.emit_marker(wf_uuid, module="alerts", record_name=record_name,
                            field=field, inputs={"tag": _TAG}, timeout=timeout)
        checks.eq(got, f"verdict-{_TAG}",
                  f"{field!r} on the emitted alert carries the computed verdict")
    finally:
        h.cleanup()
    return checks.report()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args(argv)
    codes = [_once(i, args.timeout) for i in range(1, args.runs + 1)]
    passed = codes.count(0)
    if args.runs > 1:
        print(f"\n=== S-effect: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
