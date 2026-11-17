#!/usr/bin/env python3
"""Full build→see→run proof for the playbook-builder agent against the LIVE box.

Where prompt_loop.py stops at the agent's behavior (drive turns, export, report
tool failures), this script proves the *outcome* the analyst cares about:

  1. BUILD  — drive a scenario whose final turn ends in `awaiting_playbook_offer`
              (the agent emits a `playbook_offer` card from the recorded trace).
  2. ACCEPT — `chat_resume {decision: accept}` on the SAME session, exactly as
              the widget's "Save as Playbook" CTA does. The connector compiles
              the session trace and pushes it (push_playbook).
  3. SEE    — GET /api/3/workflow_collections/<uuid> and /api/3/workflows/<uuid>
              to prove the playbook really landed in FortiSOAR (not just that the
              accept echoed a uuid).
  4. RUN    — trigger the pushed workflow and poll to terminal; PASS only when it
              reaches `finished`.

Exit 0 only when all four steps pass (CI-able). Exit nonzero otherwise.

Usage (from the connector dir, with the FSRPlaybookYaml venv — it has requests):
  set -a; source ../../FSRPlaybookYaml/.env; set +a
  ../../FSRPlaybookYaml/.venv/bin/python scripts/build_run_proof.py \
      --file scripts/scenarios.json --scenario enrich_then_build

  # custom turns (last turn should ask to SAVE/build the playbook):
  scripts/build_run_proof.py \
      --prompt "look up ip 1.1.1.1" \
      --prompt "Save this investigation as a re-runnable playbook."

  --title       playbook name to save under (default: derived from the offer)
  --no-run      stop after SEE (push + verify), don't trigger the playbook
  --keep        do NOT purge the pushed collection at the end (default: purge)
  --timeout     per-turn poll timeout (default 420s)
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsr_live import LiveFSR  # noqa: E402
# Reuse the exact drive loop the loop harness uses (detached chat_turn + the
# since_turn-fenced chat_poll), so "build" here behaves identically to a real
# widget conversation.
from prompt_loop import (  # noqa: E402
    SCENARIOS, _assistant_summary, _pair_and_classify,
)
from drive import FsrTransport, drive  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# fsr_playbooks is symlinked into the connector package; the python/ helpers
# (probes._env, cli, e2e.runner) live in the FSRPlaybookYaml repo. run_playbook
# needs both on the path.
_FSRPB = (ROOT.parent.parent / "FSRPlaybookYaml").resolve()


def _fetch_entity(fsr, iri: str) -> dict | None:
    """Build the widget-shaped `entity` for a record IRI: {iri, module, uuid,
    id, fields}. `fields` mirrors what the drawer widget sends (the record's
    scalar field map) — the connector stamps it onto the trace so the
    trace-compiler can parameterize a matched IOC to vars.input.records[0].*.
    Picklist/related dict fields collapse to their itemValue/@id for matching."""
    parts = iri.split("/")
    if len(parts) <= 3:
        return None
    # /api/3/<module>/<uuid> → the module is the segment before the id.
    module = parts[-2]
    import requests
    r = requests.get(f"{fsr.base}{iri}", headers=fsr.headers,
                     verify=fsr.verify, timeout=30)
    if r.status_code != 200:
        return {"iri": iri, "module": module}
    rec = r.json()
    fields: dict = {}
    for k, v in rec.items():
        if k.startswith("@") or k in ("__self", "modelMetadata"):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            fields[k] = v
        elif isinstance(v, dict):
            val = v.get("itemValue") or v.get("@id")
            if val is not None:
                fields[k] = val
    return {"iri": iri, "module": module,
            "uuid": rec.get("uuid"), "id": rec.get("id"), "fields": fields}


def _find_offer(transcript) -> dict | None:
    """Return the playbook_offer card emitted in a turn, or None."""
    for ev in transcript or []:
        if ev.get("type") == "playbook_offer":
            return ev
    return None


def _drive_turns(fsr, session_id, intent, turns, entity, timeout):
    """Run each user message as a detached turn; return the final transcript."""
    transport = FsrTransport(fsr, session_id=session_id, detached=True, poll_timeout=timeout)
    steps = [{"user": text, "n": i} for i, text in enumerate(turns, 1)]
    final = {"transcript": None}

    def announce(raw: dict) -> dict:
        print(f"  [turn {raw['n']}/{len(turns)}] {raw['user'][:70]!r}")
        return {}

    def record(step) -> None:
        transcript = (step.envelope or {}).get("transcript")
        fails = sum(1 for c in _pair_and_classify(transcript) if not c["ok"])
        print(f"     done: turn={transport.turn_no} stop={step.stop} tool_failures={fails}")
        final["transcript"] = transcript

    drive(transport, {"steps": steps}, steps=steps, intent=intent, entity=entity,
          history=lambda st: _assistant_summary((st.envelope or {}).get("transcript")),
          turn_params=announce, on_step=record)
    return final["transcript"]


def _run_pushed(wf_uuid: str, record: str | None = None,
                timeout_s: int = 180) -> dict:
    """Trigger the pushed workflow via fsr_playbooks's run_playbook and poll to
    terminal. `record` ("<module>:<uuid>") supplies the per-record trigger
    context so a record-parameterized playbook's vars.input.records[0]
    resolves — without it a module-bound playbook fails with blank inputs."""
    sys.path.insert(0, str(ROOT / "fsr-playbook-builder"))  # fsr_playbooks symlink
    sys.path.insert(0, str(_FSRPB))                         # fsr_playbooks (canonical)
    sys.path.insert(0, str(_FSRPB / "python"))             # probes/cli/e2e
    from fsr_playbooks.mcp_server.tools_execution import run_playbook
    return run_playbook(wf_uuid, record=record, follow=True, timeout_s=timeout_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--file")
    ap.add_argument("--prompt", action="append", default=[])
    ap.add_argument("--intent", default="triage")
    ap.add_argument("--entity")
    ap.add_argument("--title")
    ap.add_argument("--no-run", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args(argv)

    # Resolve turns: --prompt wins, else --scenario from --file or built-ins.
    catalog = dict(SCENARIOS)
    if args.file:
        catalog.update(json.loads(Path(args.file).read_text()))
    ent_iri = args.entity
    if args.prompt:
        turns, intent = list(args.prompt), args.intent
    elif args.scenario:
        spec = catalog.get(args.scenario)
        if not isinstance(spec, dict) or not spec.get("turns"):
            print(f"unknown scenario {args.scenario!r}; have: "
                  f"{', '.join(k for k in catalog if not k.startswith('_'))}")
            return 2
        turns = list(spec["turns"])
        intent = args.intent or spec.get("intent", "triage")
        ent_iri = args.entity or spec.get("entity")
    else:
        print("pass --scenario or --prompt")
        return 2

    fsr = LiveFSR.from_env()
    # Fetch the record's fields so the connector can stamp them on the trace
    # (records[0] parameterization). Mirrors what the drawer widget sends.
    entity = _fetch_entity(fsr, ent_iri) if ent_iri else None
    session_id = "proof-" + uuid.uuid4().hex[:8]
    print(f"proof → {fsr.base}  connector={fsr.version}  session={session_id}")
    if entity:
        print(f"entity: {entity['module']} fields={len(entity.get('fields') or {})}")

    # ── 1. BUILD ─────────────────────────────────────────────────────────────
    print("\n[1/4] BUILD — driving turns to a playbook_offer")
    transcript = _drive_turns(fsr, session_id, intent, turns, entity, args.timeout)
    offer = _find_offer(transcript)
    if not offer:
        print("  ✗ FAIL: final turn emitted no playbook_offer card. The last "
              "turn must ask the agent to SAVE the investigation as a playbook.")
        return 1
    offer_id = offer.get("id") or "offer"
    title = args.title or offer.get("title_suggestion") or "Loop Proof Playbook"
    print(f"  ✓ offer {offer_id!r}  steps={len(offer.get('draft_steps') or [])}"
          f"  mutating={offer.get('has_mutating_action')}  title={title!r}")

    # ── 2. ACCEPT ────────────────────────────────────────────────────────────
    print("\n[2/4] ACCEPT — chat_resume{decision:accept} (widget 'Save' CTA)")
    r = fsr.execute("chat_resume", {"session_id": session_id, "decision": "accept",
                                    "offer_id": offer_id, "title": title})
    if not r.ok:
        print(f"  ✗ FAIL: accept errored: {r.error or r.data}")
        return 1
    pushed = next((e for e in (r.data.get("transcript") or [])
                   if e.get("variant") == "playbook_pushed"), None)
    if not pushed or not pushed.get("collection_uuid"):
        print(f"  ✗ FAIL: accept did not push a playbook: "
              f"{json.dumps(r.data, default=str)[:400]}")
        return 1
    coll = pushed["collection_uuid"]
    wfs = pushed.get("workflow_uuids") or []
    print(f"  ✓ pushed: collection={coll}  workflows={wfs}")

    # ── 3. SEE ───────────────────────────────────────────────────────────────
    print("\n[3/4] SEE — confirm it really landed in FortiSOAR")
    ok_coll = fsr.collection_exists(coll)
    import requests
    wf_ok = {}
    for wf in wfs:
        resp = requests.get(f"{fsr.base}/api/3/workflows/{wf}",
                            headers=fsr.headers, verify=fsr.verify, timeout=30)
        wf_ok[wf] = resp.status_code == 200
    print(f"  collection_exists={ok_coll}  workflows={wf_ok}")
    if not (ok_coll and wfs and all(wf_ok.values())):
        print("  ✗ FAIL: playbook not retrievable from FortiSOAR")
        return 1
    print("  ✓ visible in FortiSOAR")

    rc = 0
    # When triage ran on a record, assert the playbook is genuinely re-runnable:
    # a manual (cybersponse.action) per-record trigger + record-sourced inputs
    # (vars.input.records[0]), not a baked-in literal / Referenced-only trigger.
    if entity and entity.get("module"):
        wj = requests.get(f"{fsr.base}/api/3/workflows/{wfs[0]}?$relationships=true",
                          headers=fsr.headers, verify=fsr.verify, timeout=30).json()
        blob = json.dumps(wj, default=str)
        start = next((s for s in wj.get("steps", []) if s.get("name") == "Start"), {})
        st = start.get("stepType") or {}
        trig = st.get("name") if isinstance(st, dict) else st
        uses_records = "vars.input.records[0]" in blob
        manual = trig == "cybersponse.action"
        print(f"  re-runnable check: trigger={trig}  uses_records[0]={uses_records}")
        if not (manual and uses_records):
            print("  ✗ FAIL: not record-parameterized "
                  "(want cybersponse.action + vars.input.records[0])")
            rc = 1
        else:
            print("  ✓ manual per-record trigger + record-sourced inputs")
    # ── 4. RUN ───────────────────────────────────────────────────────────────
    if args.no_run:
        print("\n[4/4] RUN — skipped (--no-run)")
    else:
        print("\n[4/4] RUN — trigger the pushed playbook, poll to terminal")
        # A record-parameterized (module-bound) playbook needs the triggering
        # record so vars.input.records[0] resolves; pass the triaged entity.
        record = None
        if entity and entity.get("module") and entity.get("uuid"):
            record = f"{entity['module']}:{entity['uuid']}"
            print(f"  record context: {record}")
        run = _run_pushed(wfs[0], record=record)
        print(f"  result: {json.dumps(run, default=str)[:400]}")
        if run.get("ok") and run.get("status") == "finished":
            print(f"  ✓ ran clean: status={run.get('status')} "
                  f"task_id={run.get('task_id')} wf_pk={run.get('wf_pk')}")
        else:
            print(f"  ✗ FAIL: run did not finish cleanly "
                  f"(status={run.get('status')})")
            rc = 1

    # cleanup
    if not args.keep:
        print(f"\ncleanup: purge collection {coll} → "
              f"{fsr.purge_collection(coll)}")
    else:
        print(f"\n(kept collection {coll} — inspect it in the playbook library)")

    print("\n" + "=" * 64)
    print(f"BUILD→SEE→RUN proof: {'PASS' if rc == 0 else 'FAIL'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
