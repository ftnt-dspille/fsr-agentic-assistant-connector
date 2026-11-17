"""S1 — can the assistant CREATE a runnable playbook from natural language?

The mirror of S2 (modify) on the create side. S2 grades an edit to an open
playbook; S1 grades a playbook authored from nothing but a plain-English ask, and
against the plan's one bar: **it runs on a real box and produces the right
observable result** — not "the right tools were called", not "the turn reached
finished".

    ASK (no OPEN PLAYBOOK): "make a playbook that creates an alert saying CHARLIE"
      -> a build turn, on a real box, authoring from scratch
      -> extract the final YAML the turn produced                the deliverable
      -> Accept on the offer card (chat_resume)                   the analyst's create path
      -> read the saved playbook back (the offered steps)        the write, not the card
      -> activate + trigger the created playbook                 does it RUN
      -> read the alert it created (marker == CHARLIE)           did it do the asked-for thing

Where S1 differs from S2, and why:

- **No fixture, no clone.** There is nothing to modify — the assistant starts
  from an empty designer. So there is also no `diff_versions` oracle: a create
  has no "before". The oracle is purely behavioural (the marker record), which is
  exactly what the scenario matrix specifies for S1.

- **The deliverable rides in `emit_playbook_offer`, not a bare fence.** For a NEW
  playbook the build prompt ends by calling
  `emit_playbook_offer(..., yaml=<final verified YAML>)` — that card's Deploy
  button is what compiles+pushes (`_resume_playbook_offer_accept` reads
  `final_yaml`). So the YAML lives in the tool call's `input`, and only falls back
  to a trailing ```yaml fence. `_offer_yaml` reads the offer first for that
  reason; a turn that emits neither produced nothing the Deploy button could ship.

- **The push is `push_playbook` (create), not `update_playbook` (edit).** Same
  compile step; different terminal op — mirrors what the offer-accept path does
  for a brand-new playbook.

**The assistant does not write; the WIDGET does.** As in S2, `push_playbook`/
`update_playbook` are connector ops the widget calls on Deploy — in no intent's
tool slice, so the assistant cannot call them. This script does what the Deploy
button does with the assistant's authored YAML, and grades the assistant on that
YAML — the only thing it controls.

**LLM turns are stochastic.** One green run is not a fix and one red run is not a
defect — use --runs n and grade the pattern.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.206; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_s1_create.py [--runs 3]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid as _uuid

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_canon  # noqa: E402

from eval_harness import Checks, EvalHarness, step_names  # noqa: E402
from drive import detached_turn  # noqa: E402
from fsr_playbooks.harness.frames import assistant_text  # noqa: E402

MARKER = "CHARLIE"
REC_NAME = "ZZ Eval S1 Alert"

# The ask. Phrased the way an analyst would — it names the OUTCOME (an alert that
# says CHARLIE) and the trigger surface (manually run on the alerts module), but
# NOT the mechanism (no "use a create_record step", no field paths). Naming the
# trigger surface is legitimate authoring intent, not spelling out the answer:
# without a *manual* trigger the playbook cannot be fired by uuid on the notrigger
# route at all, so it is part of "a runnable playbook", which is the ask.
#
# Both strings are FIXED and simple on purpose. An earlier draft made the alert
# name a per-run unique timestamp for isolation — but that asks the model to
# transcribe a 10-digit hyphenated literal exactly, and it occasionally mangles it
# (`...1791-2` -> `...1791_2`), reading as a behavioural FAIL when the playbook in
# fact ran and created the alert correctly. Run isolation is not the model's job:
# `emit_marker` snapshots the existing records BEFORE the run and returns only the
# NEW one, so a fixed name is unambiguous across sequential runs and any prior
# leftover. Grade the playbook, not the model's copy-typing.
ASK_TMPL = (
    "Create a new playbook that I can run manually from the alerts module. "
    "When it runs it should create a new alert named {name!r} whose description "
    "is exactly {marker!r}. It doesn't need any inputs — just create that one "
    "alert. Give me the finished playbook."
)

_FENCE = re.compile(r"```ya?ml\s*\n([\s\S]*?)```", re.I)


def _events(transcript: list, *types: str) -> list:
    return [e for e in transcript or [] if e.get("type") in types]


def _tools(transcript: list) -> list[str]:
    # Constituent names: a union call (emit_card / find) is graded as what
    # it did -- see eval_canon.
    return eval_canon.tool_names(transcript)


_said = assistant_text  # the shared reader: streamed tokens joined, stripped


def _offer_yaml(transcript: list) -> str:
    """The YAML the Deploy button would ship.

    For a NEW playbook the build prompt ends with
    `emit_playbook_offer(..., yaml=<final YAML>)`, and `_resume_playbook_offer_accept`
    compiles+pushes that `final_yaml`. So read the offer's `yaml` input first —
    that is the exact text the accept path uses. Fall back to the LAST ```yaml
    fence (streamed deltas are concatenated first, and the last fence is the final
    revision, mirroring `view.controller.js#_extractYaml`) for a turn that showed
    the playbook in prose without emitting the card.
    """
    # Through the canonicalizer: the model offers via emit_card(card_type=
    # "playbook_offer"), which a literal name match never saw -- the grader
    # then scored the last prose fence, not the offered bytes.
    offered = eval_canon.offered_yaml(transcript)
    if offered:
        return offered
    combined = "".join(e.get("text", "") for e in transcript or []
                       if e.get("type") == "text")
    found = _FENCE.findall(combined)
    return found[-1].strip() if found else ""


def _build_turn(h: EvalHarness, ask: str, session_id: str, timeout: int) -> list:
    """One build-intent turn with NO open playbook — authoring from scratch."""
    turn, transcript, stop = detached_turn(
        h.live, session_id=session_id, intent="build",
        messages=[{"role": "user", "content": ask}], timeout=timeout)
    h.checks.note(f"turn={turn} stop={stop} tools={_tools(transcript)}")
    h.checks.note(f"assistant: {_said(transcript)[:600]}")
    return transcript or []


def _push(h: EvalHarness, yaml_text: str, session_id: str) -> str | None:
    """The Deploy button for a NEW playbook: compile_yaml -> push_playbook.

    Returns the created workflow uuid, or None (with the failure recorded) — a
    compile error is an assistant finding (it authored YAML that doesn't compile),
    a push error is reported honestly rather than raised.
    """
    compiled = h.live.execute("compile_yaml", {"yaml": yaml_text})
    if not compiled.ok or not (compiled.data or {}).get("workflow_json"):
        h.checks.failures.append(
            f"the assistant's YAML does not compile: "
            f"{compiled.error or (compiled.data or {}).get('errors')}")
        return None
    res = h.live.execute("push_playbook", {
        "workflow_json": compiled.data["workflow_json"],
        "session_id": session_id,
    })
    if not res.ok:
        h.checks.failures.append(f"push_playbook failed: {res.error or res.data}")
        return None
    data = res.data or {}
    # Register for teardown BEFORE anything downstream can fail.
    h.track(collection=data.get("collection_uuid"))
    wf_uuids = data.get("workflow_uuids") or []
    if not wf_uuids or not wf_uuids[0]:
        h.checks.failures.append(
            f"push_playbook reported ok but returned no workflow uuid: {data}")
        return None
    wf_uuid = wf_uuids[0]
    h.track(playbook=wf_uuid)
    h.checks.note(f"push_playbook ok collection={data.get('collection_uuid')} "
                  f"workflow={wf_uuid}")
    return wf_uuid


def _offer_card(transcript: list) -> dict | None:
    """The last playbook offer card the turn showed: what the analyst Accepts."""
    offers = _events(transcript, "playbook_offer")
    return offers[-1] if offers else None


def _accept(h: EvalHarness, offer: dict, session_id: str) -> str | None:
    """Press Accept on the offer card, exactly as the widget does.

    This is the path the analyst uses and the one that never saved: every
    Accept answered "Could not save playbook" while an eval that pushed the
    YAML itself graded PASS (N2). So the eval goes through chat_resume and
    reads the saved playbook from the `playbook_pushed` card it returns.
    """
    res = h.live.execute("chat_resume", {"session_id": session_id, "decision": "accept",
                                         "card_id": offer.get("id")}, timeout=300)
    data = res.data or {}
    frames = data.get("transcript") or []
    pushed = next((e for e in frames if e.get("type") == "info_card"
                   and e.get("variant") == "playbook_pushed"), None)
    if pushed is None:
        errs = [e.get("errors") for e in frames if e.get("variant") == "compile_errors"]
        h.checks.failures.append(
            f"Accept did not save the playbook: {_said(frames)[:300]!r}"
            + (f" errors={errs}" if errs else "") + (f" tags={data.get('tags')}" if not frames else ""))
        return None
    h.track(collection=pushed.get("collection_uuid"))
    wf_uuids = pushed.get("workflow_uuids") or []
    if not wf_uuids:
        h.checks.failures.append(f"Accept saved but named no workflow: {pushed}")
        return None
    h.track(playbook=wf_uuids[0])
    h.checks.note(f"Accept saved collection={pushed.get('collection_uuid')} workflow={wf_uuids[0]}")
    return wf_uuids[0]


def _saved_steps_match(h: EvalHarness, wf_uuid: str, yaml_text: str) -> None:
    """Read the saved playbook back: it must hold the steps the offer showed."""
    import yaml as _yaml
    try:
        doc = _yaml.safe_load(yaml_text) or {}
    except Exception:  # noqa: BLE001
        return
    pbs = doc.get("playbooks") if isinstance(doc, dict) else None
    pb = (pbs[0] if isinstance(pbs, list) and pbs else doc) or {}
    offered = {s.get("name") for s in pb.get("steps") or [] if isinstance(s, dict)}
    wf = h.client.playbooks.get_definition(wf_uuid, relationships=True).to_dict(by_alias=True)
    saved = set(step_names(wf).values())
    missing = sorted(n for n in offered if n and n not in saved)
    h.checks.ok(not missing, f"saved playbook lacks offered steps {missing} (saved: {sorted(saved)})")


def _diagnose_miss(h: EvalHarness, wf_uuid: str, yaml_text: str) -> None:
    """A run that reached `finished` but emitted no marker is the exact failure
    this plan exists to catch — so make it self-explaining rather than a bare
    red. The run tree's per-step outcomes answer both questions at once: whether
    a step FAILED (a runtime authoring defect, e.g. args placed outside
    `arguments:`), and what record a succeeded step actually created (so an eval
    false-negative is visible too). The authored YAML is dumped alongside."""
    print("\n--- S1 behavioural miss: authored YAML ---")
    print(yaml_text)
    print("--- run's actual step outcomes ---")
    pk = h.runs[-1] if h.runs else None
    try:
        # detailed=True surfaces per-step timing on every node (root + children),
        # so a miss buried in a sub-playbook run is visible too.
        tree = h.client.playbooks.run_tree(pk, steps=True, detailed=True) if pk else None

        def _dump(node, indent="  "):
            for s in (getattr(node, "steps", None) or []):
                ms = getattr(s, "duration_ms", None)
                dur = f" {ms}ms" if ms is not None else ""
                print(f"{indent}step={getattr(s, 'name', None)!r} "
                      f"status={getattr(s, 'status', None)!r}{dur} "
                      f"result={str(getattr(s, 'result_preview', None))[:200]}")
            for ch in (getattr(node, "children", None) or []):
                print(f"{indent}child={getattr(ch, 'name', None)!r} "
                      f"({getattr(ch, 'status', None)!r})")
                _dump(ch, indent + "  ")

        if tree:
            _dump(tree)
    except Exception as e:  # noqa: BLE001
        print(f"  (run_tree unavailable: {e})")
    print("--- end diagnostic ---\n")


def _once(run_idx: int, timeout: int) -> int:
    checks = Checks(f"S1 create a playbook from natural language (run {run_idx})")
    h = EvalHarness(checks)
    session_id = f"eval-s1-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    rec_name = REC_NAME
    checks.note(f"box={h.live.base} connector={h.live.version} session={session_id}")

    try:
        ask = ASK_TMPL.format(name=rec_name, marker=MARKER)
        transcript = _build_turn(h, ask, session_id, timeout)

        # 1. the deliverable: YAML for a new playbook. No YAML (neither an offer
        #    card nor a fence) => nothing for the Deploy button to ship, so the
        #    turn produced nothing usable however good the prose was.
        yaml_text = _offer_yaml(transcript)
        if not checks.ok(yaml_text,
                         "the turn produced no playbook YAML — no emit_playbook_offer "
                         "and no ```yaml fence, so the Deploy button has nothing to push"):
            return checks.report()

        # 2. the analyst's create path: Accept on the offer card. A turn that
        #    only showed a fence goes through the widget's fence Deploy instead
        #    (compile + push), noted so the two are never confused.
        offer = _offer_card(transcript)
        if offer is not None:
            wf_uuid = _accept(h, offer, session_id)
        else:
            checks.note("no offer card: deploying the ```yaml fence the way the widget does")
            wf_uuid = _push(h, yaml_text, session_id)
        if not wf_uuid:
            return checks.report()
        _saved_steps_match(h, wf_uuid, yaml_text)

        # A pushed playbook may land inactive depending on the authored YAML;
        # the Execute/notrigger route needs it active. This is platform plumbing
        # the designer's Save handles, not part of what the assistant is graded
        # on, so do it for the turn rather than fail it.
        try:
            h.client.playbooks.update(wf_uuid, isActive=True)
        except Exception as e:  # noqa: BLE001
            checks.note(f"activate warning (continuing): {e}")

        # 3. the bar: it RUNS, and creates the alert the ask described.
        got = h.emit_marker(wf_uuid, module="alerts", record_name=rec_name)
        if not checks.eq(got, MARKER,
                         "description on the alert the created playbook emitted"):
            _diagnose_miss(h, wf_uuid, yaml_text)
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
        print(f"\n=== S1: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
