"""S1b -- refine the playbook this conversation SAVED, and Apply it in place.

The offline sweep proves the refine-a-draft path (edit_playbook -> next Create
card) but never presses Accept, so the SAVED path had no LLM-driven proof:
after Accept the conversation records the saved playbook's id, a later build
turn with nothing open grounds on it, and Apply must write to THAT playbook --
not save a second copy beside it.

    turn 1  "make a playbook that creates an alert saying CHARLIE"
            -> Accept on the offer card                      saved, id recorded
    turn 2  same session, nothing open: "change the description to DELTA"
            -> an enhancement_offer (Apply), not a playbook_offer (Create)
            -> Apply (chat_resume accept)                    update_playbook
    read back: same workflow uuid, still exactly one playbook by that name,
               every saved step still there
    run it:    the alert it creates says DELTA               the write took

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.<box>; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_s1_refine_saved.py [--runs 2]
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid as _uuid

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_harness import Checks, EvalHarness, step_names  # noqa: E402
from eval_s1_create import (  # noqa: E402
    ASK_TMPL,
    MARKER,
    REC_NAME,
    _accept,
    _diagnose_miss,
    _events,
    _offer_card,
    _offer_yaml,
    _said,
    _tools,
)
from drive import detached_turn  # noqa: E402

NEW_MARKER = "DELTA"
REFINE = (f"Change that playbook so the alert's description is exactly "
          f"{NEW_MARKER!r} instead. Keep everything else the same.")


def _turn(h: EvalHarness, messages: list, session_id: str, since: int,
          timeout: int) -> tuple[int, list]:
    """One build turn with NO open playbook (no entity), as the widget sends
    it from the chat with nothing open in the designer."""
    turn, transcript, stop = detached_turn(
        h.live, session_id=session_id, intent="build", messages=messages,
        since_turn=since, timeout=timeout)
    h.checks.note(f"turn={turn} stop={stop} tools={_tools(transcript)}")
    h.checks.note(f"assistant: {_said(transcript)[:400]}")
    return turn, transcript or []


def _apply(h: EvalHarness, card: dict, session_id: str) -> bool:
    res = h.live.execute("chat_resume", {"session_id": session_id, "decision": "accept",
                                         "card_id": card.get("id")}, timeout=300)
    frames = (res.data or {}).get("transcript") or []
    updated = next((e for e in frames if e.get("type") == "info_card"
                    and e.get("variant") == "playbook_updated"), None)
    return h.checks.ok(updated is not None,
                       f"Apply did not update the saved playbook: {_said(frames)[:300]!r}")


def _once(run_idx: int, timeout: int) -> int:
    checks = Checks(f"S1b refine the saved playbook, Apply in place (run {run_idx})")
    h = EvalHarness(checks)
    session_id = f"eval-s1b-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    checks.note(f"connector={h.live.version} session={session_id}")
    try:
        ask = ASK_TMPL.format(name=REC_NAME, marker=MARKER)
        turn, t1 = _turn(h, [{"role": "user", "content": ask}], session_id, 0, timeout)
        offer = _offer_card(t1)
        if not checks.ok(offer, "turn 1 offered no playbook to Accept"):
            return checks.report()
        wf_uuid = _accept(h, offer, session_id)
        if not wf_uuid:
            return checks.report()
        before = h.definition(wf_uuid)
        name = before.get("name")
        steps_before = set(step_names(before).values())
        checks.note(f"saved {name!r} steps={sorted(steps_before)}")

        msgs = [{"role": "user", "content": ask},
                {"role": "assistant", "content": _said(t1) or "Here is the playbook."},
                {"role": "user", "content": REFINE}]
        _, t2 = _turn(h, msgs, session_id, turn, timeout)
        tools = _tools(t2)
        checks.ok("edit_playbook" in tools,
                  f"turn 2 re-typed the playbook instead of editing it (tools={tools})")
        if not checks.ok(not _events(t2, "playbook_offer"),
                         "turn 2 offered the saved playbook as a NEW one (Create would duplicate it)"):
            return checks.report()
        enh = _events(t2, "enhancement_offer")
        if not checks.ok(enh, "turn 2 delivered no Apply card for the saved playbook"):
            return checks.report()
        if not _apply(h, enh[-1], session_id):
            return checks.report()

        after = h.definition(wf_uuid)
        steps_after = set(step_names(after).values())
        checks.ok(not (steps_before - steps_after),
                  f"Apply lost saved steps {sorted(steps_before - steps_after)}")
        same_name = h.client.playbooks.list(name=name, limit=10)
        checks.eq(len(same_name), 1, f"playbooks named {name!r} after Apply")
        for pb in same_name:
            if pb.get("uuid") != wf_uuid:
                h.track(playbook=pb.get("uuid"))

        try:
            h.client.playbooks.update(wf_uuid, isActive=True)
        except Exception as e:  # noqa: BLE001
            checks.note(f"activate warning (continuing): {e}")
        got = h.emit_marker(wf_uuid, module="alerts", record_name=REC_NAME)
        if not checks.eq(got, NEW_MARKER, "description on the alert the APPLIED playbook emitted"):
            _diagnose_miss(h, wf_uuid, _offer_yaml(t2))
    finally:
        h.cleanup()
    return checks.report()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args(argv)
    codes = [_once(i, args.timeout) for i in range(1, args.runs + 1)]
    passed = codes.count(0)
    if args.runs > 1:
        print(f"\n=== S1b: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
