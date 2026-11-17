#!/usr/bin/env python3
"""Push → prompt → wait → export → investigate. A repeatable test loop for the
agent against the LIVE box.

It drives chat_turn (detached) + chat_poll exactly as the widget does — including
the `since_turn` fence — to completion, captures a widget-shaped export
(manifest + messages with paired tool_use/tool_result), then runs a failure /
optimization analysis: every tool error grouped by (tool, code), and the known
anti-patterns flagged with a concrete fix hint.

Usage:
  # one prompt against the current deployment (triage)
  scripts/prompt_loop.py --prompt "triage incident 65f9... and pivot on the IPs"

  # push a fresh connector build first, then run a named scenario
  scripts/prompt_loop.py --push --scenario ip_triage

  # multi-turn: each --prompt is its own turn, conversation threaded
  scripts/prompt_loop.py --prompt "look up ip 1.1.1.1" --prompt "now build a playbook"

  # ground the turn in a record (entity context, like the drawer does)
  scripts/prompt_loop.py --scenario ip_triage --entity /api/3/incidents/<uuid>

Env: creds from the BOX=<alias> env file (pyfsr registry, scripts/box.sh), same as deploy.sh.
Run with the fsr-playbook-framework venv (it has `requests`):
  set -a; source ../../fsr-playbook-framework/.env; set +a
  ../../fsr-playbook-framework/.venv/bin/python scripts/prompt_loop.py ...
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsr_live import LiveFSR  # noqa: E402
from drive import DriveError, FsrTransport, drive  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Named multi-turn scenarios. Each is a list of user messages (one per turn).
SCENARIOS = {
    "ip_triage": {
        "intent": "triage",
        "turns": ["Triage this incident. Pull the SIEM events, pivot on every "
                  "external IP you find, and enrich them."],
    },
    "ip_lookup_then_build": {
        "intent": "triage",
        "turns": [
            "look up ip 1.1.1.1",
            "Design a re-runnable playbook that automates that enrichment. "
            "Produce the YAML.",
        ],
    },
    "host_pivot": {
        "intent": "triage",
        "turns": ["Investigate the host in this incident — pull its recent SIEM "
                  "activity and any related alerts."],
    },
}


# ── analysis ────────────────────────────────────────────────────────────────

def _parse_result(content):
    """A tool_result's content is usually a JSON string. Return (ok, code, obj)."""
    obj = content
    if isinstance(content, str):
        try:
            obj = json.loads(content)
        except ValueError:
            return True, None, content  # plain text → treat as success
    if isinstance(obj, dict):
        ok = obj.get("ok")
        return (ok is not False), obj.get("code") or obj.get("status"), obj
    return True, None, obj


def _pair_and_classify(transcript):
    """Pair tool_use with its tool_result and classify ok/error. Returns a list
    of {name, input, ok, code, result_obj}."""
    results = {}
    for ev in transcript or []:
        if ev.get("type") == "tool_result":
            results[ev.get("tool_use_id")] = ev.get("content")
    calls = []
    for ev in transcript or []:
        if ev.get("type") == "tool_use":
            ok, code, obj = _parse_result(results.get(ev.get("id")))
            calls.append({"name": ev.get("name"), "input": ev.get("input"),
                          "ok": ok, "code": code, "result": obj})
    return calls


def _investigate(calls):
    """Print a failure / optimization report and return the failure count."""
    by_name, errors = {}, []
    for c in calls:
        by_name[c["name"]] = by_name.get(c["name"], 0) + 1
        if not c["ok"]:
            errors.append(c)

    print(f"\n  tool calls: {len(calls)} across {len(by_name)} tools "
          f"({sum(1 for c in calls if not c['ok'])} failed)")
    for n, k in sorted(by_name.items(), key=lambda kv: -kv[1]):
        fails = sum(1 for c in calls if c["name"] == n and not c["ok"])
        print(f"    {n:<26} {k:>2} call(s)" + (f"  ✗{fails}" if fails else ""))

    if not errors:
        print("\n  ✓ no tool failures")
        return 0

    # Group errors by (tool, code).
    groups = {}
    for c in errors:
        groups.setdefault((c["name"], c["code"]), []).append(c)
    print("\n  failures by (tool, code):")
    for (name, code), cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"    ✗ {name} / {code}  ×{len(cs)}")

    # Known anti-patterns → concrete fix hint.
    print("\n  diagnosis:")
    for (name, code), cs in groups.items():
        ex = cs[0]["result"] if isinstance(cs[0]["result"], dict) else {}
        issues = ex.get("issues") or []
        unknown = [i for i in issues if i.get("problem") == "unknown"]
        missing = [i for i in issues if i.get("problem") == "missing_required"]
        if code == "bad_params" and unknown and missing:
            uks = ", ".join(i["param"] for i in unknown)
            reqs = ", ".join(i["param"] for i in missing)
            kind = ("should auto-remap now" if len(unknown) == 1 and len(missing) == 1
                    else "ambiguous — agent needs the param sig at discovery")
            print(f"    • {name}: PARAM-NAME GUESS — passed [{uks}], op wants "
                  f"[{reqs}] ({kind}). Surface sig via find_operation/get_op_schema.")
        elif code == "bad_params" and not issues:
            print(f"    • {name}: OPAQUE bad_params — no `issues` in the error. "
                  f"The tool should propagate the underlying detail so the agent "
                  f"can self-correct (siem first-class tools).")
        elif code == "op_request_rejected":
            print(f"    • {name}: OP REJECTED by backend — likely an ID/value "
                  f"mismatch (e.g. FortiSOAR record id vs the connector's own id). "
                  f"Resolve the right id before the call.")
        else:
            msg = (ex.get("message") or "")[:120]
            print(f"    • {name} / {code}: {msg}")
    return len(errors)


# ── drive ───────────────────────────────────────────────────────────────────

def _assistant_summary(transcript):
    """Collapse a turn's text events into a short assistant message for threading
    the next turn (mirrors what the widget sends in messages[])."""
    parts = [e.get("text", "") for e in transcript or [] if e.get("type") == "text"]
    return ("\n".join(p for p in parts if p))[:4000] or "(investigation performed)"


def _run_scenario(fsr, name, intent, turns, entity_iri, timeout,
                  approve=False) -> int:
    """Run one scenario (its own session) end to end: turns → export →
    investigate. Returns the failure count."""
    session_id = "loop-" + uuid.uuid4().hex[:8]
    # Module is the segment before the record id: /api/3/<module>/<uuid> → [-2].
    # (Not [2] — that's the API version "3"; the persona lookup keys on module.)
    entity = {"iri": entity_iri, "module": entity_iri.split("/")[-2]} \
        if entity_iri and len(entity_iri.split("/")) > 3 else None
    print(f"\n{'#' * 64}\n# scenario {name!r}  session={session_id}  "
          f"intent={intent}  turns={len(turns)}"
          + (f"  entity={entity_iri}" if entity else "") + f"\n{'#' * 64}")

    all_calls, export_msgs = [], []
    steps = []
    for i, turn in enumerate(turns, 1):
        # A turn is a plain string (uses the scenario intent) or a
        # {text, intent} dict so a single chain can switch intent mid-session
        if isinstance(turn, dict):
            user_text, turn_intent = turn.get("text", ""), turn.get("intent") or intent
        else:
            user_text, turn_intent = turn, intent
        steps.append({"user": user_text, "intent": turn_intent, "n": i})
    transport = FsrTransport(fsr, session_id=session_id, detached=True, poll_timeout=timeout)

    def announce(raw: dict) -> dict:
        print(f"\n[turn {raw['n']}/{len(turns)}] intent={raw['intent']} {raw['user'][:60]!r}")
        return {}

    # A turn that parked on a tier-gated write is NOT finished: the gated tool
    # never ran and has no tool_result. Feeding the next user message to a
    # session in that state silently abandons the approval, so every write
    # scenario "passes" having written nothing. --approve answers the gate the
    # way the widget's Approve button does, and the rest of the turn runs.
    def approve_gate(halts: list[dict], env: dict):
        if not approve or env.get("stop_reason") != "approval_required":
            return None
        pending = [e for e in env.get("transcript") or [] if e.get("type") == "approval_request"]
        if not pending:
            print("   ! stop=approval_required but no approval_request in "
                  "the transcript -- cannot answer the gate")
            return None
        req = pending[-1]
        print(f"   → approving {req.get('tool')} "
              f"(tier={req.get('tier')} id={req.get('approval_id')})")
        # approval_id ONLY: chat_resume routes on which id you send, and a
        # card_id alongside falls through to the action-card branch.
        res = transport.resume({"session_id": session_id, "decision": "approve",
                                "approval_id": req.get("approval_id")})
        if not res.get("ok"):
            print(f"   ✗ chat_resume failed: {res.get('error') or res}")
            return None
        # chat_resume re-enters the agent loop synchronously and returns the
        # finished transcript; it does not mint a new turn to poll for.
        print(f"   resumed inline: stop={res.get('stop_reason')} "
              f"events={len(res.get('transcript') or [])}")
        return res

    def record(step) -> None:
        t = transport.turn_no
        transcript = (step.envelope or {}).get("transcript") or []
        for env in step.envelopes:
            all_calls.extend(_pair_and_classify(env.get("transcript")))
        fails = sum(1 for c in _pair_and_classify(transcript) if not c["ok"])
        print(f"   done: turn={t} stop={step.stop} tool_failures={fails}")
        export_msgs.append({"role": "user", "text": step.sent})
        export_msgs.append({"role": "assistant", "transcript": transcript,
                            "stop_reason": step.stop, "turn": t})

    try:
        drive(transport, {"steps": steps}, steps=steps, intent=intent, entity=entity,
              history=lambda st: _assistant_summary((st.envelope or {}).get("transcript")),
              answer=approve_gate, turn_params=announce, on_step=record)
    except DriveError as e:
        print(f"   ✗ {e}")
        return 1

    by_name = {}
    for c in all_calls:
        by_name[c["name"]] = by_name.get(c["name"], 0) + 1
    export = {
        "manifest": {
            "session": session_id, "scenario": name, "mode": "live",
            "intent": intent,
            "connector": {"name": "connector-fsr-agentic-assistant", "version": fsr.version},
            "entity": entity_iri, "turns": len(turns),
            "toolCallCount": len(all_calls), "toolCallsByName": by_name,
            "errorCount": sum(1 for c in all_calls if not c["ok"]),
        },
        "messages": export_msgs,
    }
    out_dir = ROOT / "exports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"loop-{name}-{session_id}-{int(time.time())}.json"
    out_path.write_text(json.dumps(export, indent=2, default=str))
    print(f"\n>> export: {out_path.relative_to(ROOT)}")

    print("=" * 64)
    n_fail = _investigate(all_calls)
    print("\n" + "=" * 64)
    print(f"[{name}] {'FAIL' if n_fail else 'PASS'} — "
          f"{len(all_calls)} calls, {n_fail} failed")
    return n_fail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true",
                    help="run scripts/deploy.sh (bump patch) ONCE before testing")
    ap.add_argument("--prompt", action="append", default=[],
                    help="a user message = one turn (repeatable)")
    ap.add_argument("--scenario", help="named scenario to run (from --file or the "
                    f"built-ins: {', '.join(SCENARIOS)})")
    ap.add_argument("--file", help="JSON file of scenarios "
                    "{name: {intent, entity?, turns:[...]}} — see scenarios.json")
    ap.add_argument("--all", action="store_true",
                    help="run every scenario in --file (or the built-ins) in order")
    ap.add_argument("--intent", default=None, help="triage|build (overrides scenario)")
    ap.add_argument("--entity", help="record IRI to ground the turn (overrides "
                    "scenario; e.g. /api/3/incidents/<uuid>)")
    ap.add_argument("--timeout", type=int, default=480)
    ap.add_argument("--approve", action="store_true",
                    help="answer tier-gated approval prompts with approve, as the "
                         "widget's Approve button does, and keep polling. Without "
                         "this a gated write never runs and the next user turn "
                         "silently abandons the pending approval.")
    args = ap.parse_args(argv)

    # Scenario catalog: built-ins, overlaid by --file if given.
    catalog = dict(SCENARIOS)
    if args.file:
        try:
            loaded = json.loads(Path(args.file).read_text())
        except Exception as e:  # noqa: BLE001
            print(f"could not read --file {args.file!r}: {e}")
            return 2
        if not isinstance(loaded, dict):
            print("--file must be a JSON object {name: {intent, turns:[...]}}")
            return 2
        catalog.update(loaded)

    # Build the run list: --all (whole catalog), --scenario (one), or --prompt.
    runs: list[tuple[str, str, list[str], str | None]] = []
    if args.all:
        for nm, spec in catalog.items():
            # Skip doc/placeholder keys (e.g. "_README") and turn-less entries.
            if nm.startswith("_") or not isinstance(spec, dict) \
                    or not spec.get("turns"):
                continue
            runs.append((nm, args.intent or spec.get("intent", "triage"),
                         list(spec.get("turns", [])),
                         args.entity or spec.get("entity")))
    elif args.scenario:
        spec = catalog.get(args.scenario)
        if not spec:
            print(f"unknown scenario {args.scenario!r}; have: {', '.join(catalog)}")
            return 2
        runs.append((args.scenario, args.intent or spec.get("intent", "triage"),
                     list(spec.get("turns", [])), args.entity or spec.get("entity")))
    elif args.prompt:
        runs.append(("adhoc", args.intent or "triage", list(args.prompt),
                     args.entity))
    if not runs or not any(r[2] for r in runs):
        print("nothing to run: pass --prompt, --scenario, or --all (with --file)")
        return 2

    if args.push:
        print(">> push: scripts/deploy.sh --bump patch\n")
        rc = subprocess.call([str(ROOT / "scripts" / "deploy.sh"), "--bump", "patch"])
        if rc != 0:
            print(f"deploy failed (rc={rc}) — aborting")
            return rc

    fsr = LiveFSR.from_env()
    print(f"loop → {fsr.base}  connector={fsr.version}  scenarios={len(runs)}")

    totals = []
    for nm, intent, turns, entity_iri in runs:
        n_fail = _run_scenario(fsr, nm, intent, turns, entity_iri, args.timeout,
                               approve=args.approve)
        totals.append((nm, n_fail))

    if len(totals) > 1:
        print(f"\n{'=' * 64}\nSUMMARY")
        for nm, n in totals:
            print(f"  {'✗' if n else '✓'} {nm:<24} {n} failed")
    return 1 if any(n for _, n in totals) else 0


if __name__ == "__main__":
    raise SystemExit(main())
