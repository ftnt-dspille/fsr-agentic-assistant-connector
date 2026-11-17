"""S3 — can the assistant author a runnable CONNECTOR-ACTION playbook?

S1 grades a playbook that only creates a record; S3 adds the thing the scenario
matrix actually names: a **connector op in the middle**, whose output has to flow
onward into the observable result. The plan's bar for S3 is precise —

    passes when: runs; the connector op received the right inputs and its output
    was mapped onward.

so this eval proves *both halves*, and guards the one way a model can fake it.

    ASK (no OPEN PLAYBOOK): "make a playbook that uses the FortiSOAR Utilities
      'Convert String Time to Minutes' action to convert '3 hours', then creates
      an alert whose description is that number of minutes"
      -> a build turn, authoring from scratch
      -> extract the final YAML                                   the deliverable
      -> STRUCTURAL oracle on the authored YAML:                  did it wire it?
           * a `connector` step invokes cyops_utilities /
             convert_periodic_time_to_minutes with the asked-for input
           * the create_record description is a Jinja expr that references that
             step's output — NOT a hardcoded "180"
      -> compile_yaml -> push_playbook -> activate                the widget's path
      -> run, read the created alert's description == "180"       BEHAVIOURAL oracle

Why the structural oracle is not optional here. The op is deterministic
(`convert_periodic_time_to_minutes("3 hours") -> {"minutes": 180}`, config-less,
live-verified on 206), which is what makes "180" a clean behavioural marker — but
it *also* means a model could skip the connector entirely and just author
`description: "180"`, computing the answer in its head. That playbook would run
and emit "180" and pass a behavioural-only check while doing none of what S3
tests. So the structural oracle asserts a *real data flow*: a connector step
exists, and the record field is a Jinja reference to its output, not a literal.
The runtime "180" then proves that flow actually carried the right value.

The op was chosen for the sharpest possible oracle: pure compute (no external
service, so no config, no network, no flake), integer output (no copy-typing for
the model to mangle), and a value the input uniquely determines ("3 hours" -> 180;
any other input or a broken mapping yields a different number or an empty field).

**The assistant does not write; the WIDGET does.** As in S1, `push_playbook` is a
connector op the widget calls on Deploy — in no intent's tool slice, so the
assistant cannot call it. This script does what the Deploy button does with the
assistant's authored YAML, and grades the assistant on that YAML.

**LLM turns are stochastic.** One green run is not a fix and one red run is not a
defect — use --runs n and grade the pattern.

Run:
  cd <connector-repo>
  set -a; . <harness>/.env.206; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/eval_s3_connector.py [--runs 3]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid as _uuid

import urllib3
import yaml as _yaml

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_canon  # noqa: E402

from eval_harness import Checks, EvalHarness  # noqa: E402
from drive import detached_turn  # noqa: E402
from fsr_playbooks.harness.frames import assistant_text  # noqa: E402

CONNECTOR = "cyops_utilities"
OPERATION = "convert_periodic_time_to_minutes"
INPUT_TIME = "3 hours"
MARKER = "180"  # convert_periodic_time_to_minutes("3 hours") -> {"minutes": 180}
REC_NAME = "ZZ Eval S3 Alert"

# The ask. Analyst-level: it names the OUTCOME (an alert whose description is the
# converted minutes) and the MECHANISM at the altitude a designer would — "use
# the Utilities 'Convert String Time to Minutes' action". Naming the connector
# action is not spelling out the answer; it is the whole point of S3 ("connector-
# action playbook"). It deliberately does NOT give the field path, the step
# wiring, or the number 180 — those are what the assistant has to get right.
ASK_TMPL = (
    "Create a new playbook that I can run manually from the alerts module. "
    "When it runs it should use the FortiSOAR Utilities connector's "
    "'Convert String Time to Minutes' action to convert {time!r} into a number "
    "of minutes, and then create a new alert named {name!r} whose description is "
    "exactly that number of minutes the action returned. It doesn't need any "
    "inputs. Give me the finished playbook."
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
    """The YAML the Deploy button would ship — the offer card's `yaml` input
    first (that is the exact text `_resume_playbook_offer_accept` compiles), else
    the LAST ```yaml fence. Identical to S1's extractor; kept local so the two
    evals can diverge without coupling."""
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


def _steps(yaml_text: str) -> list[dict]:
    """The step list of the first playbook in the authored collection.

    Tolerant of the two shapes the assistant may emit: a full collection
    (`playbooks: [{steps: [...]}]`) or a bare single playbook (`steps: [...]`).
    Returns [] on anything unparseable — the caller reports that as a finding.
    """
    try:
        doc = _yaml.safe_load(yaml_text)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(doc, dict):
        return []
    pbs = doc.get("playbooks")
    if isinstance(pbs, list) and pbs and isinstance(pbs[0], dict):
        doc = pbs[0]
    steps = doc.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _slug(name: str) -> str:
    """Step name -> the token it is referenced by inside Jinja (spaces -> `_`).
    Mirrors the grammar's `vars.steps.<name-with-spaces-replaced-by-underscores>`
    rule (static_grammar_block.md §3.3)."""
    return re.sub(r"\s+", "_", (name or "").strip())


def _connector_step(steps: list[dict]) -> dict | None:
    """The connector step that invokes the asked-for op, matched leniently: a
    `connector`-type step whose arguments name cyops_utilities OR the operation.
    Either alone is enough — the model occasionally abbreviates the connector
    name but the operation is unambiguous."""
    for s in steps:
        if (s.get("type") or "").lower() != "connector":
            continue
        args = s.get("arguments") or {}
        conn = str(args.get("connector") or "").lower()
        op = str(args.get("operation") or "").lower()
        if CONNECTOR in conn or op == OPERATION:
            return s
    return None


def _record_step(steps: list[dict]) -> dict | None:
    for s in steps:
        if (s.get("type") or "").lower() in ("create_record", "insert_record"):
            return s
    return None


def _description_arg(rec_step: dict) -> str:
    """The create_record's description as authored (before compile/runtime)."""
    res = (rec_step.get("arguments") or {}).get("resource") or {}
    return str(res.get("description", "")) if isinstance(res, dict) else ""


def _check_structure(h: EvalHarness, yaml_text: str) -> None:
    """The 'did it actually wire the connector' oracle. Records findings; never
    raises — a structural miss and a behavioural miss are both worth seeing in
    one run."""
    checks = h.checks
    steps = _steps(yaml_text)
    if not checks.ok(steps, "authored YAML has no parseable steps"):
        return

    conn = _connector_step(steps)
    if checks.ok(conn,
                 f"no connector step invoking {CONNECTOR}/{OPERATION} — the "
                 f"assistant did not use the connector action the ask named"):
        args = conn.get("arguments") or {}
        params = args.get("params") or {}
        # right INPUT: the periodic-time param carries the asked-for value.
        pval = " ".join(str(v) for v in params.values()) if isinstance(params, dict) else str(params)
        checks.ok(re.search(r"3\s*hours?", pval, re.I),
                  f"connector step's params do not pass {INPUT_TIME!r} "
                  f"(got params={params!r}) — the op would receive the wrong input")
        checks.note(f"connector step={conn.get('name')!r} "
                    f"op={args.get('operation')!r} params={params!r}")

    rec = _record_step(steps)
    if not checks.ok(rec, "no create_record step — nothing emits the marker alert"):
        return

    desc = _description_arg(rec)
    # OUTPUT mapped onward: the description must be a Jinja reference, not the
    # literal answer. `description: "180"` is the exact fake this guards against —
    # it would run and emit 180 while using no connector output at all.
    is_jinja = "{{" in desc
    checks.ok(is_jinja and desc.strip() not in (MARKER, f'"{MARKER}"'),
              f"create_record description is not a Jinja reference to the "
              f"connector output (got {desc!r}) — a hardcoded value is not "
              f"'output mapped onward'")
    if is_jinja and conn:
        # Best-effort: note whether it references THIS connector step's output.
        slug = _slug(conn.get("name") or "")
        if slug and slug not in desc and "vars.steps" not in desc:
            checks.note(f"NOTE: description Jinja {desc!r} does not obviously "
                        f"reference step {slug!r} — may route via an intermediate var")
    checks.note(f"description as authored: {desc!r}")


def _build_turn(h: EvalHarness, ask: str, session_id: str, timeout: int) -> list:
    turn, transcript, stop = detached_turn(
        h.live, session_id=session_id, intent="build",
        messages=[{"role": "user", "content": ask}], timeout=timeout)
    h.checks.note(f"turn={turn} stop={stop} tools={_tools(transcript)}")
    h.checks.note(f"assistant: {_said(transcript)[:600]}")
    return transcript or []


def _push(h: EvalHarness, yaml_text: str, session_id: str) -> str | None:
    """Deploy button for a NEW playbook: compile_yaml -> push_playbook. Same as
    S1 — a compile error is an assistant finding, a push error is reported."""
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


def _diagnose_miss(h: EvalHarness, wf_uuid: str, yaml_text: str) -> None:
    """A run that made no marker alert (or the wrong one) is exactly what S3
    exists to catch — dump the authored YAML and the run's per-step outcomes so a
    connector-step failure (bad params, missing config) is self-explaining."""
    print("\n--- S3 behavioural miss: authored YAML ---")
    print(yaml_text)
    print("--- run's actual step outcomes ---")
    pk = h.runs[-1] if h.runs else None
    try:
        # detailed=True recurses child runs with timing — a failing connector
        # call often lives in a sub-playbook, invisible to the root-only view.
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
    checks = Checks(f"S3 author a connector-action playbook (run {run_idx})")
    h = EvalHarness(checks)
    session_id = f"eval-s3-{h.stamp}-{_uuid.uuid4().hex[:8]}"
    checks.note(f"box={h.live.base} connector={h.live.version} session={session_id}")

    try:
        ask = ASK_TMPL.format(time=INPUT_TIME, name=REC_NAME)
        transcript = _build_turn(h, ask, session_id, timeout)

        # 1. the deliverable.
        yaml_text = _offer_yaml(transcript)
        if not checks.ok(yaml_text,
                         "the turn produced no playbook YAML — no emit_playbook_offer "
                         "and no ```yaml fence, so the Deploy button has nothing to push"):
            return checks.report()

        # 2. structural oracle: the connector is actually wired, output flows on.
        #    (Runs even if the push/run later fails, so a broken data flow is
        #    visible independently of a broken run.)
        _check_structure(h, yaml_text)

        # 3. the widget's create path.
        wf_uuid = _push(h, yaml_text, session_id)
        if not wf_uuid:
            return checks.report()
        try:
            h.client.playbooks.update(wf_uuid, isActive=True)
        except Exception as e:  # noqa: BLE001
            checks.note(f"activate warning (continuing): {e}")

        # 4. behavioural oracle: it RUNS and the connector's 180 lands on the alert.
        got = h.emit_marker(wf_uuid, module="alerts", record_name=REC_NAME)
        if not checks.eq(got, MARKER,
                         "description on the alert the connector-action playbook emitted"):
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
        print(f"\n=== S3: {passed}/{args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
