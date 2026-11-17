#!/usr/bin/env python
"""Seed a PERSISTENT manual-input record-action playbook on `ztpf_devices` so the
live matrix (and a real analyst) can drive the widget's run -> manual-input form
-> results round-trip. Unlike scripts/repro_two_manual_inputs.py (a throwaway,
self-deleting diagnostic), this LEAVES a stable, deterministically-named playbook
on the box so a matrix scenario prompt ("run the Manual Input Demo playbook")
resolves the same way every run.

Shape: a `start` bound to `module: ztpf_devices` (a cybersponse.action
Execute-menu trigger, i.e. record-action — the trigger class that gets the clean
`awaiting_input` seam; a MANUAL-trigger playbook does NOT) -> ONE `manual_input`
gate collecting a note -> stamp it back. One gate keeps the proof simple: run ->
form displayed -> fillable -> submit -> results. Pass --two-gate for a chain.

Idempotent: purges any prior collection of the same name (incl. recycle bin)
before pushing, so re-running just refreshes it. Commit-safe: NO box/creds here
(EnvConfig-less — uses the same FSR_* env the repro script does). Point at a box:

    set -a; . <harness>/.env.206; set +a
    .venv/bin/python scripts/seed_manual_input_playbook.py            # push
    .venv/bin/python scripts/seed_manual_input_playbook.py --two-gate # 2 gates
    .venv/bin/python scripts/seed_manual_input_playbook.py --remove   # delete only
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "connector-fsr-agentic-assistant"))  # fsr_soc_triage (unused but parity)

from pyfsr import FortiSOAR
import fsr_playbooks

# STABLE name — a matrix scenario references it verbatim; do not add a timestamp.
NAME = os.environ.get("MI_PLAYBOOK_NAME", "Manual Input Demo - Collect Note")
# The record-action subject. Defaults to the ZTPF module the live matrix uses,
# but a box without ztpf_devices (a stock 8.0 appliance has none) can seed the
# same round-trip against any module it does have -- MI_MODULE=alerts.
MODULE = os.environ.get("MI_MODULE", "ztpf_devices")
REF_DB = os.path.join(os.path.dirname(fsr_playbooks.__file__), "_data", "fsr_reference.db")
HARD = {"$hardDelete": "true", "$showDeleted": "true"}


def _yaml(two_gate: bool) -> str:
    gate2 = """
      - name: Gate2
        type: manual_input
        title: Second gate — enter an approver
        inputs:
          - {name: approver, kind: text, label: Approver, required: true}
        options:
          - {option: Submit, primary: true}
        next: Done""" if two_gate else ""
    first_next = "Gate2" if two_gate else "Done"
    return f"""\
collection: {NAME}
description: Persistent manual-input demo — record-action on a device, pauses for a form.
visible: true

playbooks:
  - name: {NAME}
    description: Run from a {MODULE} record; pauses for a manual-input form, then stamps it back.
    steps:
      - name: Start
        type: start
        module: {MODULE}
        next: Gate1
      - name: Gate1
        type: manual_input
        title: Enter a note for this device
        inputs:
          - {{name: note, kind: text, label: Note, required: true}}
        options:
          - {{option: Submit, primary: true}}
        next: {first_next}{gate2}
      - name: Done
        type: set_variable
        vars:
          collected_note: "{{{{ vars.steps.Gate1.note | default('') }}}}"
          finished: true
"""


def _client() -> FortiSOAR:
    url = os.environ["FSR_BASE_URL"].rstrip("/")
    if os.environ.get("FSR_PORT"):
        url = f"{url}:{os.environ['FSR_PORT']}"
    return FortiSOAR(url, username=os.environ["FSR_USERNAME"],
                     password=os.environ["FSR_PASSWORD"], verify_ssl=False)


def _find_collection_uuid(c: FortiSOAR) -> str | None:
    """Match a workflow_collection by exact name."""
    try:
        resp = c.get("/api/3/workflow_collections", params={"name": NAME, "$limit": 5})
        for m in (resp.get("hydra:member") if isinstance(resp, dict) else []) or []:
            if m.get("name") == NAME:
                return m.get("uuid")
    except Exception as e:  # noqa: BLE001
        print(f"    (lookup warn: {e})")
    return None


def _purge(c: FortiSOAR) -> None:
    uuid = _find_collection_uuid(c)
    if not uuid:
        return
    try:
        c.delete(f"/api/3/workflow_collections/{uuid}", params=HARD)
        print(f"    purged prior collection {uuid}")
    except Exception as e:  # noqa: BLE001
        print(f"    (purge warn: {e})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--two-gate", action="store_true", help="two sequential manual-input gates")
    ap.add_argument("--remove", action="store_true", help="delete the seeded playbook and exit")
    args = ap.parse_args()

    c = _client()
    print(f"box  = {c.base_url if hasattr(c,'base_url') else os.environ.get('FSR_BASE_URL')}")
    print(f"name = {NAME!r}")

    if args.remove:
        _purge(c)
        print("removed.")
        return 0

    res = fsr_playbooks.compile_yaml(_yaml(args.two_gate), REF_DB)
    if not res.ok:
        print("COMPILE FAILED:")
        for e in (getattr(res, "errors", None) or []):
            print("  -", getattr(e, "code", ""), getattr(e, "message", e))
        return 1
    collection = res.fsr_json["data"][0]
    collection["name"] = NAME

    _purge(c)  # idempotent refresh
    created = c.post("/api/3/workflow_collections", data=collection)
    coll_uuid = created.get("uuid") if isinstance(created, dict) else None
    wfs = (created or {}).get("workflows") or []
    if not wfs and coll_uuid:
        wfs = (c.get(f"/api/3/workflow_collections/{coll_uuid}") or {}).get("workflows") or []
    wf_uuid = (wfs[0] if wfs else {}).get("uuid")
    gates = 2 if args.two_gate else 1
    print(f"pushed: collection={coll_uuid} workflow={wf_uuid} gates={gates}")
    if not wf_uuid:
        print("  WARN: could not resolve workflow uuid")
        return 1
    print("Done. Run it from a ztpf_devices record's Execute menu, or ask the "
          f"assistant on a device page to run {NAME!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
