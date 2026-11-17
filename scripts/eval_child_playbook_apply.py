"""Live: Apply on an offer that adds a reusable child playbook saves the child.

    deploy + clone the marker emitter            the open playbook
    ASK: a new reusable playbook, called from this one   one build turn
      -> enhancement_offer carrying playbooks_added       the card says so
      -> Apply (chat_resume accept, designer open)        the child is upserted
    read back: the child exists on the box, in the open playbook's collection,
               with its trigger and its declared parameter
    the staged edit calls it by workflowReference (not by a YAML-local name)

Offline this path is stubbed (`offline_designer` records the upsert), so this
is the only place the create-on-box write is proven.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.<box>; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_child_playbook_apply.py [--runs 1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid as _uuid

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_harness import Checks, EvalHarness  # noqa: E402
from eval_s2_modify import _decompile_live, _entity, _said, _tools  # noqa: E402
from drive import detached_turn  # noqa: E402

FIXTURE = "marker_emitter.yaml"
PB = "ZZ Eval Marker Emitter"


def _ask(child: str) -> str:
    return (f"Make a new reusable playbook named {child!r} that takes one input "
            f"parameter called note and just stores it in a variable. Then have "
            f"this playbook call it after the Emit step, passing note = 'hello'. "
            f"Keep everything else the same.")


def _offer(frames: list) -> dict | None:
    return next((e for e in reversed(frames or [])
                 if e.get("type") == "enhancement_offer"), None)


def _once(run_idx: int, timeout: int) -> int:
    checks = Checks(f"child playbook created on Apply (run {run_idx})")
    h = EvalHarness(checks)
    session_id = f"eval-child-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    child = f"ZZ Eval Child {h.stamp}-{run_idx}"
    checks.note(f"box={h.live.base} connector={h.live.version} session={session_id}")
    try:
        coll = h.deploy(FIXTURE)
        wf = h.clone(PB, f"{PB} child {h.stamp}-{run_idx}")
        wf_uuid = wf["uuid"]
        entity = _entity(h, wf_uuid, playbook_yaml=_decompile_live(h, wf_uuid))

        _turn, frames, stop = detached_turn(
            h.live, session_id=session_id, intent="build", entity=entity,
            messages=[{"role": "user", "content": _ask(child)}], timeout=timeout)
        checks.note(f"stop={stop} tools={_tools(frames)}")
        checks.note(f"assistant: {_said(frames)[:400]}")

        card = _offer(frames)
        if not checks.ok(card, "the turn ended with no enhancement_offer (Apply) card"):
            return checks.report()
        checks.note(f"playbooks_added={card.get('playbooks_added')}")
        if not checks.ok(child in (card.get("playbooks_added") or []),
                         f"the offer does not add {child!r}: {card.get('playbooks_added')}"):
            return checks.report()

        res = h.live.execute("chat_resume", {
            "session_id": session_id, "decision": "accept",
            "card_id": card.get("id"), "workflow_iri": h.iri(wf_uuid)}, timeout=300)
        out = (res.data or {}).get("transcript") or []
        checks.note(f"apply said: {_said(out)[:400]}")

        rows = h.client.get("/api/3/workflows", params={
            "name": child, "$limit": 5, "$relationships": "true"}).get("hydra:member") or []
        for r in rows:
            h.track(playbook=r["uuid"])
        if not checks.ok(len(rows) == 1, f"expected one {child!r} on the box, found {len(rows)}"):
            return checks.report()
        made = rows[0]
        made_coll = made.get("collection")
        made_coll = made_coll.get("@id") if isinstance(made_coll, dict) else made_coll
        checks.ok(str(made_coll or "").endswith(coll),
                  f"child landed in {made_coll!r}, not the open playbook's collection {coll}")
        checks.ok("note" in (made.get("parameters") or []),
                  f"child parameters are {made.get('parameters')!r}, want 'note'")
        steps = made.get("steps") or []
        checks.ok(len(steps) >= 2, f"child has {len(steps)} step(s)")
        # The saved parent, read back: its call step must name the child by id.
        # (Apply saves straight to the box here; the chat reply carries no YAML.)
        parent_steps = json.dumps(h.definition(wf_uuid).get("steps") or [])
        checks.ok(made["uuid"] in parent_steps,
                  "the saved parent does not call the child by its uuid "
                  "(workflowReference)")
    finally:
        h.cleanup()
    return checks.report()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args(argv)
    rc = 0
    for i in range(1, a.runs + 1):
        rc |= _once(i, a.timeout)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
