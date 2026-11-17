"""Live regression: the widget's manual-input resume handles a CHAIN of gates.

Originally written to CONFIRM a gap (the resume path surfaced only the first
manual_input gate and dead-ended); now guards the fix — resume_playbook re-emits
the `awaiting_input` seam when a run re-pauses, so gate 2+ keep surfacing a form.

Builds a throwaway ztpf_devices record-action playbook with TWO sequential
`manual_input` gates, uploads it to the box, then drives the exact connector
path the widget uses and asserts:

  GATE 1 (works)  run_playbook -> code:awaiting_input with the form ->
                  _manual_input_card_from_awaiting() yields a manual_input card
                  (this is what the widget renders as an in-chat form).

  SUBMIT GATE 1   resume_playbook(inputs=...) submits the first form and drives
                  the run onward (the widget's submitManualInput -> respond_
                  manual_input path, same pyfsr mi.resume underneath).

  GATE 2 (the gap) the run genuinely PAUSES again on the second manual_input
                  (proven by reading the box: _pending_input finds note_two),
                  but resume_playbook's RESULT carries no `awaiting`/`code:
                  awaiting_input` block, so _manual_input_card_from_awaiting()
                  returns None -> the widget is handed NO second form. The chain
                  dead-ends after the first submission.

No ship: runs the CURRENT working-tree connector code (editable, FSRPB_DEV=1)
against the box, so it validates the gap on the code as it stands. Self-cleaning
(hard-deletes the test collection, incl. the recycle bin).

Run (point the env at a box that has the ztpf_devices module):
  cd <connector-repo>
  set -a; . <harness>/.env.<box>; set +a
  REPRO_DEVICE_IRI=/api/3/ztpf_devices/<uuid> \\
    FSRPB_DEV=1 .venv/bin/python scripts/repro_two_manual_inputs.py
"""
from __future__ import annotations

import os
import sys
import time
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))            # tool_probe helpers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "connector-fsr-agentic-assistant"))         # fsr_soc_triage

from pyfsr import FortiSOAR
import fsr_playbooks                                                    # editable (FSRPB_DEV=1)
import tool_probe as tp                                                 # _bind_local / _mount

# A real ztpf_devices record on the target box (has a record-action Execute
# menu). Override per box via REPRO_DEVICE_IRI; the default is a seeded device.
DEVICE_IRI = os.environ.get(
    "REPRO_DEVICE_IRI", "/api/3/ztpf_devices/5b23794a-a657-4169-97d3-98698126d59f")
TEST_NAME = f"REPRO-2MI-{int(time.time())}"
REF_DB = os.path.join(os.path.dirname(fsr_playbooks.__file__), "_data", "fsr_reference.db")
HARD = {"$hardDelete": "true", "$showDeleted": "true"}

YAML = f"""\
collection: {TEST_NAME}
description: Throwaway repro — two sequential manual-input gates on a device.
visible: true

playbooks:
  - name: {TEST_NAME}
    description: Pauses twice for manual input to expose the single-gate resume limit.
    steps:
      - name: Start
        type: start
        module: ztpf_devices          # binds a cybersponse.action Execute-menu trigger
        next: Gate1
      - name: Gate1
        type: manual_input
        title: First gate — enter note one
        inputs:
          - {{name: note_one, kind: text, label: Note One, required: true}}
        options:
          - {{option: Submit, primary: true}}
        next: Gate2
      - name: Gate2
        type: manual_input
        title: Second gate — enter note two
        inputs:
          - {{name: note_two, kind: text, label: Note Two, required: true}}
        options:
          - {{option: Submit, primary: true}}
        next: Done
      - name: Done
        type: set_variable
        vars:
          finished: true
"""


def _client() -> FortiSOAR:
    url = os.environ["FSR_BASE_URL"].rstrip("/")
    if os.environ.get("FSR_PORT"):
        url = f"{url}:{os.environ['FSR_PORT']}"
    return FortiSOAR(url, username=os.environ["FSR_USERNAME"],
                     password=os.environ["FSR_PASSWORD"], verify_ssl=False)


def _purge(c: FortiSOAR, coll_uuid: str | None) -> None:
    if not coll_uuid:
        return
    try:
        c.delete(f"/api/3/workflow_collections/{coll_uuid}", params=HARD)
    except Exception as e:  # noqa: BLE001
        print(f"    (cleanup warn: {e})")


def main() -> int:
    # 1. Compile the 2-gate YAML locally.
    res = fsr_playbooks.compile_yaml(YAML, REF_DB)
    if not res.ok:
        print("COMPILE FAILED:")
        for e in (getattr(res, "errors", None) or []):
            print("  -", getattr(e, "code", ""), getattr(e, "message", e))
        return 1
    collection = res.fsr_json["data"][0]
    collection["name"] = TEST_NAME

    c = _client()
    coll_uuid = None
    try:
        # 2. Upload — create the collection on the box.
        created = c.post("/api/3/workflow_collections", data=collection)
        coll_uuid = created.get("uuid") if isinstance(created, dict) else None
        wfs = (created or {}).get("workflows") or []
        if not wfs and coll_uuid:
            wfs = (c.get(f"/api/3/workflow_collections/{coll_uuid}") or {}).get("workflows") or []
        wf_uuid = (wfs[0] if wfs else {}).get("uuid")
        print(f"[upload] collection={coll_uuid} workflow={wf_uuid} name={TEST_NAME!r}")
        if not wf_uuid:
            print("  could not resolve workflow uuid — abort"); return 1

        # 3. Bind the device persona + mount FG1 (what a real turn does).
        tp._bind_local(None)
        tp._mount(DEVICE_IRI, "ztpf_devices", None)
        from fsr_playbooks.llm.tools import REGISTRY
        from fsr_playbooks.mcp_server._shared import _live_client

        # ---- GATE 1: run the playbook, expect an awaiting_input FORM ----------
        print("\n[gate1] run_playbook ...")
        r1 = REGISTRY["run_playbook"].fn(playbook=TEST_NAME, timeout=25)
        run_pk = r1.get("run_pk") or (r1.get("run") or {}).get("pk")
        print(f"  run_playbook -> ok={r1.get('ok')} code={r1.get('code')} run_pk={run_pk} status={r1.get('status')}")
        card1 = _card_from(r1)
        gate1_form = bool(r1.get("code") == "awaiting_input" and (r1.get("awaiting") or {}).get("fields"))
        print(f"  awaiting form present: {gate1_form} | widget manual_input card synthesized: {bool(card1)}")
        if card1:
            print(f"    card fields: {[f.get('name') for f in card1.get('fields', [])]}")

        if not run_pk:
            print("  no run_pk — the run did not pause as expected; abort"); return 1

        # ---- SUBMIT GATE 1 (the widget's resume) ------------------------------
        print("\n[submit gate1] resume_playbook(inputs={note_one}) ...")
        r2 = REGISTRY["resume_playbook"].fn(run=str(run_pk), decision="approve",
                                            inputs={"note_one": "repro one"}, timeout=25)
        print(f"  resume_playbook -> ok={r2.get('ok')} code={r2.get('code')} status={r2.get('status')} resumed={r2.get('resumed')}")

        # ---- GATE 2: the gap --------------------------------------------------
        # (a) The run really pauses again — poll the box for the SECOND pending input.
        print("\n[gate2] polling the box for the second pending manual input ...")
        from fsr_playbooks.mcp_server.tools_playbook_run import _pending_input
        playbooks = getattr(_live_client(), "playbooks", None)
        gate2_form = None
        for i in range(12):
            time.sleep(3)
            form = _pending_input(_live_client(), playbooks, str(run_pk))
            names = [f.get("name") for f in (form or {}).get("fields", [])] if form else []
            print(f"  poll {i+1}: pending fields = {names}")
            if names and "note_two" in names:
                gate2_form = form
                break

        # (b) Does the resume RESULT surface the second form to the widget? With
        # the fix, resume_playbook re-emits the awaiting_input seam on re-pause,
        # so _manual_input_card_from_awaiting synthesizes the gate-2 card.
        card2_from_resume = _card_from(r2)
        resume_has_awaiting = r2.get("code") == "awaiting_input" or bool(r2.get("awaiting"))
        gate2_surfaced = bool(card2_from_resume) or resume_has_awaiting

        print("\n================ RESULT ================")
        print(f"  GATE 1 rendered in widget (card synthesized) : {bool(card1)}")
        print(f"  GATE 1 submitted / run advanced (resumed)    : {bool(r2.get('resumed'))}")
        print(f"  GATE 2 genuinely pending on the box          : {bool(gate2_form)}")
        print(f"  GATE 2 surfaced by resume_playbook to widget : {gate2_surfaced}")
        if card2_from_resume:
            print(f"    gate-2 card fields: {[f.get('name') for f in card2_from_resume.get('fields', [])]}")
        chain_ok = (bool(card1) and bool(r2.get("resumed"))
                    and bool(gate2_form) and gate2_surfaced)
        if chain_ok:
            print("\n  ✓ CHAIN OK: the second manual_input gate is surfaced to the widget as")
            print("    a fresh form — a multi-gate playbook no longer dead-ends after gate 1.")
        elif bool(gate2_form) and not gate2_surfaced:
            print("\n  ✗ GAP: the run paused on a SECOND manual input, but the resume path")
            print("    returned no form — the widget dead-ends after gate 1 (the pre-fix bug).")
        else:
            print("\n  ? inconclusive: the run did not re-pause on a second gate this run.")
        print("=======================================")
        return 0 if chain_ok else 2
    finally:
        _purge(c, coll_uuid)
        print(f"[cleanup] hard-deleted collection {coll_uuid}")


_OPS = None


def _load_operations():
    """Import the connector's operations.py (which imports the on-box `connectors`
    SDK) with a minimal SDK stub — the same shim the connector's own unit tests
    use — so the repro runs the REAL _manual_input_card_from_awaiting."""
    global _OPS
    if _OPS is not None:
        return _OPS
    import importlib.util
    import types
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                        "connector-fsr-agentic-assistant"))
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [root]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", os.path.join(root, "operations.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    _OPS = mod
    return mod


def _card_from(tool_result: dict):
    """Run the connector's REAL card synth over a run/resume result, exactly as
    _prepare_transcript does when it decides whether to render a manual_input card."""
    ops = _load_operations()
    fn = getattr(ops, "_manual_input_card_from_awaiting", None)
    if fn is None:
        return None
    return fn(tool_result, "tool_use_repro", 0)


if __name__ == "__main__":
    raise SystemExit(main())
