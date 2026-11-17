"""Post-ship card smoke: emit one card of EACH type on the box; fail the ship
if any refuses (#147).

The ship gate already proves artifacts (worker versions, build fingerprint,
reference DB). This proves behavior for the card surface: every card the
widget can render is emitted through the DEPLOYED `emit_card` path via the
`call_mcp_tool` operation, so the exact normalize→validate→envelope code the
model hits at runtime is the code under test. It would have failed the ship
the day the 5-of-5 first-attempt refusals landed, and again on both #146
refusal classes (`op` dialect unmapped, complete payload bounced for a
missing id) -- each payload below is sent twice, once in the declared
vocabulary and once in the MODEL'S dialect from real traces.

`enhancement_offer` is special: its `verified_id` is process-local to one
worker, and two `execute` calls load-balance independently. The smoke pairs
verify_enhancement → emit_card in rounds until one pair lands on the same
worker. If every emit misses with exactly `unknown_verified_id` (the typed,
designed miss) while verify itself passed, that is a DEGRADED pass -- both
halves are alive; only the cross-call pairing failed. Any other refusal code
fails the ship.

Usage:
    python scripts/card_smoke.py                    # active version
    python scripts/card_smoke.py --version 0.5.134  # pin (deploy.sh does this)

Env: FSR_BASE_URL + creds, same as deploy.sh. Exit 0 = every card type
emitted; non-zero = at least one refusal.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

_SMOKE_YAML = """collection: Card Smoke
playbooks:
- name: Card Smoke PB
  trigger_step_id: start
  steps:
  - type: start
    name: Start
    module: alerts
    next: Block IP
  - type: connector
    name: Block IP
    connector: cyops_utilities
    operation: no_op
    params: {}
"""

# The gate goes IN FRONT of the block: Start routes into it. Appended with no
# route in, it would be an orphan that never runs, and verify refuses that.
_SMOKE_YAML_AFTER = _SMOKE_YAML.replace("    next: Block IP\n",
                                        "    next: Prompt Block IP\n",
                                        1).rstrip("\n") + """
  - type: manual_input
    name: Prompt Block IP
    title: Confirm the block
    description: Approve blocking this address.
    inputs:
    - {name: ip_to_block, kind: ipv4, label: IP to block, required: true}
    options:
    - {display: Confirm, primary: true, next: Block IP}
"""

# (card_type, payload) pairs, two per type: the declared vocabulary, then the
# model's dialect (aliases + no id) that _normalize_card_payload / the
# auto-id fill must accept. Every entry must come back {ok: True, card: ...}.
# enhancement_offer is driven separately (needs a live verified_id).
PAYLOADS: list[tuple[str, str, dict]] = [
    ("choice", "canonical", {
        "id": "smoke-choice", "prompt": "Card smoke: pick one",
        "options": [{"label": "Alpha", "value": "a"},
                    {"label": "Beta", "value": "b"}],
    }),
    ("choice", "dialect", {
        # question→prompt; option id→value, text→label, detail→hint; no id
        "question": "Card smoke: pick one (dialect)",
        "options": [{"id": "a", "text": "Alpha", "detail": "first"},
                    {"id": "b", "text": "Beta", "detail": "second"}],
    }),
    ("action", "canonical", {
        "id": "smoke-action", "connector": "cyops_utilities",
        "operation": "no_op", "summary": "Card smoke action",
        "args": {"ip": "192.0.2.1"}, "editable_fields": ["ip"],
    }),
    ("action", "dialect", {
        # op→operation, title→summary, params→args; no id, no editable_fields
        "connector": "cyops_utilities", "op": "no_op",
        "title": "Card smoke action (dialect)", "params": {"ip": "192.0.2.1"},
    }),
    ("manual_input", "canonical", {
        "id": "smoke-mi", "workflow_run_iri": "/api/wf/api/smoke-run",
        "question": "Card smoke manual input",
        "fields": [{"name": "note", "label": "Note"}],
    }),
    ("manual_input", "dialect", {
        # no id -- must be auto-filled, not refused
        "workflow_run_iri": "/api/wf/api/smoke-run",
        "question": "Card smoke manual input (dialect)",
        "fields": [{"name": "note", "label": "Note"}],
    }),
    ("capability_gap", "canonical", {
        "id": "smoke-gap", "missing": "card smoke capability",
        "why": "smoke probe, nothing is actually missing",
        "fix_steps": ["No action needed -- this is the post-ship smoke."],
        "resume": {"label": "Re-check", "value": "smoke_recheck"},
    }),
    ("capability_gap", "dialect", {
        # gap→missing; no id
        "gap": "card smoke capability (dialect)",
        "why": "smoke probe, nothing is actually missing",
        "fix_steps": ["No action needed -- this is the post-ship smoke."],
        "resume": {"label": "Re-check", "value": "smoke_recheck"},
    }),
    ("playbook_offer", "canonical", {
        "id": "smoke-offer", "summary": "Card smoke playbook offer",
        "yaml": _SMOKE_YAML,
    }),
    ("playbook_offer", "dialect", {
        # no id; title_suggestion rides through
        "summary": "Card smoke playbook offer (dialect)",
        "title_suggestion": "Card Smoke PB", "yaml": _SMOKE_YAML,
    }),
]

# `verdict` cannot be emitted outside a chat turn: every finding must cite a
# tool_use id from THIS turn, and a bare call_mcp_tool has no turn, so the
# citation gate refuses it with the typed `no_turn_evidence` by design. What
# the box CAN prove is everything up to that gate -- emit_card routes the
# card_type, and the payload clears every structural check. Reaching the gate
# with exactly that code is a GATED pass; any other refusal fails the ship.
# (tests/test_card_smoke_payloads.py proves the same payload emits once its
# evidence id is registered in a turn.)
VERDICT_EVIDENCE_ID = "toolu_smoke_evidence"
VERDICT_PAYLOAD: dict = {
    "id": "smoke-verdict", "disposition": "benign", "severity": "low",
    "confidence": 0.9, "summary": "Card smoke verdict -- nothing was investigated.",
    "findings": [{"claim": "Smoke probe only.",
                  "evidence": [VERDICT_EVIDENCE_ID]}],
    "recommended_actions": [{"label": "No action needed"}],
}
VERDICT_GATE_CODE = "no_turn_evidence"

# Every renderable card type; the gate refuses to pass if any is missing from
# the run (a smoke that silently skips a type is the failure mode #147 exists
# to stop). Pinned against fsr_playbooks' CARD_TYPES by the offline test, which
# is how `verdict` stops being the one type nobody smoked.
ALL_CARD_TYPES = {"choice", "action", "manual_input", "capability_gap",
                  "playbook_offer", "enhancement_offer",
                  "verdict"}


def _call_tool(fsr, tool: str, args: dict) -> dict:
    res = fsr.execute("call_mcp_tool", {"tool": tool, "args": args})
    data = res.data or {}
    # call_mcp_tool returns the tool's own envelope; a transport/op failure
    # surfaces as res.error with no ok field.
    if res.error and not isinstance(data.get("ok"), bool):
        return {"ok": False, "code": "execute_failed", "message": str(res.error)}
    return data


def _emit(fsr, card_type: str, payload: dict) -> dict:
    return _call_tool(fsr, "emit_card",
                      {"card_type": card_type, "payload": payload})


def _drive_enhancement_offer(fsr, rounds: int) -> tuple[str, str]:
    """Returns (verdict, detail): PASS / PASS-DEGRADED / FAIL."""
    verify_ok = False
    last: dict = {}
    for i in range(rounds):
        v = _call_tool(fsr, "verify_enhancement", {
            "before_yaml": _SMOKE_YAML, "after_yaml": _SMOKE_YAML_AFTER,
            "user_message": "add a manual input step",
        })
        vid = v.get("verified_id")
        if not (v.get("ready_to_push") and vid):
            return "FAIL", (f"verify_enhancement refused on round {i + 1}: "
                            f"{v.get('code') or v.get('required_fixes') or v}")
        verify_ok = True
        last = _emit(fsr, "enhancement_offer", {
            "id": "smoke-enh", "summary": "Card smoke enhancement offer",
            "verified_id": vid,
        })
        if last.get("ok"):
            return "PASS", f"paired on round {i + 1}"
        if last.get("code") != "unknown_verified_id":
            return "FAIL", f"refused: {last.get('code')}: {last.get('message')}"
    if verify_ok:
        return "PASS-DEGRADED", (
            f"verify passed every round but no verify+emit pair hit the same "
            f"worker in {rounds} rounds; emit's miss was the typed "
            f"unknown_verified_id each time (both halves alive)")
    return "FAIL", "verify_enhancement never passed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="",
                    help="pin execute() to this connector version row")
    ap.add_argument("--rounds", type=int, default=8,
                    help="verify+emit pairing attempts for enhancement_offer")
    args = ap.parse_args(argv)

    from fsr_live import LiveFSR
    fsr = LiveFSR.from_env()
    if args.version:
        fsr.version = args.version
    print(f">> card smoke on {fsr.base} "
          f"(version={args.version or '<active>'})")

    failures: list[str] = []
    seen: set[str] = set()
    for card_type, flavor, payload in PAYLOADS:
        out = _emit(fsr, card_type, payload)
        ok = bool(out.get("ok")) and isinstance(out.get("card"), dict)
        verdict = "PASS" if ok else "FAIL"
        detail = "" if ok else f"  {out.get('code')}: {out.get('message')}"
        print(f"  {verdict:5} emit_card {card_type:17} [{flavor}]{detail}")
        if ok:
            seen.add(card_type)
        else:
            failures.append(f"{card_type} [{flavor}]: "
                            f"{out.get('code')}: {out.get('message')}")

    verdict, detail = _drive_enhancement_offer(fsr, args.rounds)
    print(f"  {verdict:5} emit_card enhancement_offer  {detail}")
    if verdict == "FAIL":
        failures.append(f"enhancement_offer: {detail}")
    else:
        seen.add("enhancement_offer")

    out = _emit(fsr, "verdict", dict(VERDICT_PAYLOAD))
    if out.get("ok"):
        print("  PASS  emit_card verdict           emitted")
        seen.add("verdict")
    elif out.get("code") == VERDICT_GATE_CODE:
        print("  PASS-GATED emit_card verdict      routed + payload valid; "
              "refused at the citation gate (no turn), as designed")
        seen.add("verdict")
    else:
        print(f"  FAIL  emit_card verdict           {out.get('code')}: "
              f"{out.get('message')}")
        failures.append(f"verdict: {out.get('code')}: {out.get('message')}")

    missing = ALL_CARD_TYPES - seen
    if missing:
        failures.append(f"card types never emitted: {sorted(missing)}")

    if failures:
        print(f"\n!! CARD SMOKE FAILED ({len(failures)}):")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"\n>> card smoke: all {len(ALL_CARD_TYPES)} card types emitted on the box.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
