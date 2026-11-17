"""S5 — can the assistant diagnose why a playbook run failed?

The troubleshooting scenario. A designer has a playbook whose last run failed;
they ask the assistant why. The bar (scenario matrix S5): the assistant's
diagnosis matches the run's ground truth — it names the FAILING STEP and the
ROOT CAUSE, not a plausible guess.

    deploy broken fixture -> clone -> run  (it FAILS)          a real failed run
      -> ASK: "why did the last run fail? what do I fix?"      a build turn
      -> the model discovers the failed run + diagnoses it     why_did_playbook_fail
      -> grade: it named the failing step and the cause        against ground truth

**Why this scenario needed a connector/framework change (the S5 gap).** Exactly
like S2 had no channel to READ the open playbook (fixed by F1's decompile seed),
the build tool slice had no channel to REACH a failed run. It carried
`diagnose_yaml_against_pb_execution(yaml_text, pb_execution)` and
`get_run_env(pb_execution)` — both of which need a run id it had no way to
obtain — but NOT `list_recent_failed_runs` or the one-shot `why_did_playbook_fail`
(those were MCP-only, absent from the LLM registry). So a "why did my run fail?"
turn dead-ended the same way S2 did. The fix exposes `why_did_playbook_fail` to
the build slice: given the open playbook's NAME (which rides in the seeded
`playbook_yaml`), it finds the most recent failed run, pulls its execution env,
and renders every Jinja block against the run's vars. One tool call.

**Why the turn runs IN-PROCESS, not via `h.live.execute` like S2.** The tool is
an UNSHIPPED framework change. `h.live.execute("chat_turn")` hits the deployed
box connector (the shipped framework), which does not have the tool in its build
slice yet. So this eval drives `chat_turn` in-process against the EDITABLE
framework (`scripts/local_turn.py`, `--llm frank --tools live`), which is the
"half live" gate: real model + real tool slice + live box reads, but not inside
a SOAR worker. A green run here is the argument for shipping; the shipped-worker
proof still needs a real ship (as F1/F3 did before 0.4.73).

**Widget-faithful context is load-bearing for S5, not just S2.** The tool tells
the model Emit failed with an `insert_data` no-data error — but the ROOT CAUSE
(the record body `resource:` is placed outside `arguments:`, so the parser
dropped it) is only visible in the YAML. The model connects the run failure to
the misplaced key by reading the seeded OPEN PLAYBOOK block. So the entity here
carries `playbook_yaml` exactly as the shipped widget seeds it — the same
`decompile_playbook` channel S2's `--widget-path` proves.

**LLM turns are stochastic.** Grade the pattern over RUNS=n, not one sample.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.206; set +a          # FSR_* box creds
  set -a; . <...>/Miscellaneous/frank/.env; set +a   # OPENAI_ENDPOINT/_API_KEY/_MODEL
  FSRPB_DEV=1 .venv/bin/python scripts/eval_s5_diagnose.py [--runs 3]
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid as _uuid
from typing import Any

import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_harness import Checks, EvalHarness  # noqa: E402
# Reuse S2's grounding helpers verbatim: the entity shape and the live decompile
# are the SAME widget channel, so importing them keeps the two scenarios honest
# about what "the widget's context" means (and DRY). `_said` is defined locally
# (defensive) because the in-process transcript can carry non-dict items.
from eval_s2_modify import _decompile_live, _entity  # noqa: E402
from local_turn import _bootstrap  # noqa: E402
from drive import DriveError, drive  # noqa: E402
from fsr_playbooks.harness.classify import classify_turn  # noqa: E402

# Default Frank model. This has gone stale twice already (gb200/glm-5.2-fp8,
# then glm-5.2-nvfp4-highthroughput -- both eventually 403 with "not allowed
# for this virtual key" as the gateway rotates what a virtual key permits).
# Check `make models` (scripts/list_models.py) before trusting this literal;
# override with --model regardless. Picked by the 09-27 audit: best accuracy
# per second of the Frank backends (see local_turn.py's fallback note).
DEFAULT_FRANK_MODEL = "coding-b200/qwen3.8-27b-nvfp4"

FIXTURE = "broken_create_record_bad_args.yaml"
PB = "ZZ Eval Broken Create Record"
EMIT_STEP = "Emit"

# The runtime error the dropped `resource:` produces — Emit runs with empty
# arguments, so create_record's insert_data() is called with no data. This is
# the ground truth the tool must surface (proves it reached the real run, not a
# hallucination). Matched loosely: the wording is the platform's, not ours.
RUN_ERROR_SIGNATURE = "positional argument"
FAILING_STATUSES = {"failed", "finished_with_error", "terminated", "errored"}

# The ask. An analyst troubleshooting, naming the SYMPTOM ("it failed") and what
# they want ("why + the fix"), never the mechanism. An ask that says "the
# resource key is misplaced" would be grading our own hint back to us.
ASK = ("The last run of this playbook failed. Why did it fail, and what do I "
       "need to change to fix it?")


def _events(transcript: Any) -> list[dict]:
    """Normalize a turn's return into a flat list of dict events.

    In-process `chat_turn` returns a list of event dicts on the happy path, but a
    degraded turn (e.g. the provider erroring) can hand back plain strings, and
    some callers wrap the list in a dict. Grade defensively: never let transcript
    shape crash the grader — a malformed transcript is itself a finding, reported
    by the checks that then see no tool calls."""
    if isinstance(transcript, dict):
        transcript = (transcript.get("transcript")
                      or transcript.get("events") or [])
    return [e for e in (transcript or []) if isinstance(e, dict)]


def _said(transcript: Any) -> str:
    """The assistant's prose for this turn (streamed text events, joined bare)."""
    return "".join(e.get("text", "") for e in _events(transcript)
                   if e.get("type") == "text").strip()


def _tool_calls(transcript: Any) -> list[str]:
    """Tool names the turn called (tool_use events carry `name`)."""
    return [e.get("name") or "?" for e in _events(transcript)
            if e.get("type") in ("tool_use", "tool_call")]


def _tool_result_for(transcript: Any, name: str) -> str:
    """The stringified content of the tool_result paired to the FIRST call of
    `name`. tool_use carries the id+name; tool_result carries only tool_use_id,
    so pair them by id. Returns "" if the tool was never called or returned
    nothing."""
    events = _events(transcript)
    call_id = None
    for e in events:
        if e.get("type") in ("tool_use", "tool_call") and (e.get("name") == name):
            call_id = e.get("id") or e.get("call_id")
            break
    if not call_id:
        return ""
    for e in events:
        if e.get("type") == "tool_result" and e.get("tool_use_id") == call_id:
            return str(e.get("content"))
    return ""


def _build_slice_has_tool() -> bool:
    """Independent check that the build LLM slice advertises the tool. This is
    the framework contract the exposure change adds; verified here directly
    rather than off the provider echo (which local_turn only populates for the
    fake provider)."""
    from fsr_playbooks.llm.intents import tools_for_intent
    return "why_did_playbook_fail" in [t["name"] for t in tools_for_intent("build")]


def _diagnose_turn(entity: dict, session_id: str, llm: str, model: str | None) -> Any:
    """One build-intent troubleshooting turn, in-process against the editable
    framework, real model, live box reads. Built-in build persona
    (persona="none"), not a Key Store fixture."""
    from local_conversation import LocalTransport
    _bootstrap()
    from connector_fsr_agentic_assistant import operations as ops
    transport = LocalTransport(ops=ops, llm=llm, model=model, tools_backend="live",
                               module=None, persona="none", records=None,
                               playbook_yaml=None, entity=entity)
    transport.session_id = session_id
    (step,) = drive(transport, {"steps": [{"user": ASK}]}, intent="build",
                    entity=entity, history="text")
    transcript = step.envelope
    outcome = classify_turn(transcript)
    if outcome.kind not in ("answered", "halted"):
        raise DriveError(f"turn did not answer ({outcome.kind}): {outcome.detail}")
    print(f">> tools called: {_tool_calls(transcript)}")
    print(f">> assistant: {_said(transcript)[:800]}")
    return transcript


def _once(run_idx: int, llm: str, model: str | None) -> int:
    checks = Checks(f"S5 diagnose a broken playbook (run {run_idx})")
    h = EvalHarness(checks)
    session_id = f"eval-s5-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    checks.note(f"box={h.live.base} connector={h.live.version} session={session_id}")

    # The framework contract the exposure change adds — the prerequisite for
    # everything below. If the build slice doesn't advertise the tool, the model
    # cannot call it and the scenario is moot; assert it before spending a run.
    if not checks.ok(_build_slice_has_tool(),
                     "the build tool slice does not advertise why_did_playbook_fail "
                     "(framework exposure change not in effect — is FSRPB_DEV set / "
                     "the editable framework on the path?)"):
        return checks.report()

    try:
        h.deploy(FIXTURE)
        wf = h.clone(PB, f"{PB} s5 {h.stamp}-{run_idx}")
        wf_uuid = wf["uuid"]
        clone_name = wf["name"]

        # PRECONDITION, not a check: the run must actually FAIL, and reach a
        # failure status (not `finished`), or `why_did_playbook_fail`'s
        # failed-only search would never find it. If this trips, the fixture or
        # the box changed — a harness problem, reported loudly, not an AI verdict.
        summary = h.run(clone_name)
        status = getattr(summary, "status", "?")
        if not checks.ok(status in FAILING_STATUSES,
                         f"PRECONDITION: the broken fixture's run did not fail "
                         f"(status={status!r}); nothing for the assistant to diagnose"):
            return checks.report()

        # Widget-faithful context: iri + module + the decompiled YAML the shipped
        # widget seeds (entity.playbook_yaml). The playbook NAME the model feeds
        # to why_did_playbook_fail lives in this YAML, and so does the misplaced
        # `resource:` key it must spot to explain the failure.
        entity = _entity(h, wf_uuid, playbook_yaml=_decompile_live(h, wf_uuid))
        transcript = _diagnose_turn(entity, session_id, llm, model)

        # 1. the capability we added: did the model reach the failed run at all?
        called = _tool_calls(transcript)
        if not checks.ok("why_did_playbook_fail" in called,
                         "the model never called why_did_playbook_fail — it had no "
                         "other way to reach the failed run, so any 'diagnosis' is a "
                         "guess (this is the S5 gap the tool exposure closes)"):
            return checks.report()

        # 2. the tool reached the REAL run: its result carries the runtime error.
        #    KNOWN HALF-LIVE LIMITATION: why_did_playbook_fail's live-run branch
        #    needs the connector-BUNDLED `fsr_playbooks.mcp_server.tools_triage`
        #    (operations.py packages it); the EDITABLE dev framework this in-process
        #    turn runs against does not ship that module, so the tool degrades to
        #    `no_investigation_tools`. That branch is verified separately by a box
        #    probe against the deployed bundle — NOT a product defect here. If the
        #    tool DID reach a run (bundle present), tighten automatically.
        result = _tool_result_for(transcript, "why_did_playbook_fail")
        if "no_investigation_tools" in result:
            checks.note("NOTE: why_did_playbook_fail degraded to no_investigation_tools "
                        "(dev framework lacks the connector-bundled tools_triage) — "
                        "live-run branch is a box-probe concern, not graded here")
        else:
            checks.ok(RUN_ERROR_SIGNATURE in result or "insert_data" in result,
                      f"why_did_playbook_fail's result did not surface the run's "
                      f"failure ({RUN_ERROR_SIGNATURE!r}/insert_data) — it did not "
                      f"reach this run's execution. got: {result[:300]!r}")

        # 3. the bar: the diagnosis names the FAILING STEP and the ROOT CAUSE.
        #    Root cause = the record body (`resource:`) sits outside `arguments:`;
        #    the fixture itself pins this as S5's oracle. Lenient on phrasing
        #    (stochastic), strict on substance.
        said = _said(transcript).lower()
        checks.ok(EMIT_STEP.lower() in said,
                  f"the diagnosis never names the failing step {EMIT_STEP!r}")
        checks.ok("resource" in said,
                  "the diagnosis never names the misplaced `resource:` key — the "
                  "actual root cause the analyst needs to fix")
        checks.ok("argument" in said,
                  "the diagnosis never connects the failure to the `arguments:` "
                  "block the record body belongs under")
    finally:
        h.cleanup()

    return checks.report()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat n times; an LLM turn is stochastic, so a single "
                         "sample grades nothing")
    ap.add_argument("--llm", default="frank", choices=["frank", "anthropic"],
                    help="provider for the turn (default: frank)")
    ap.add_argument("--model", default=None,
                    help=f"override the model (frank default: {DEFAULT_FRANK_MODEL})")
    args = ap.parse_args(argv)

    model = args.model
    if args.llm == "frank" and not model:
        model = DEFAULT_FRANK_MODEL

    codes = [_once(i, args.llm, model) for i in range(1, args.runs + 1)]
    passed = codes.count(0)
    if args.runs > 1:
        print(f"\n=== S5: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
