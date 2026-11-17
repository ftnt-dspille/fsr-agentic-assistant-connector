#!/usr/bin/env python3
"""Measure how often each terminal-action guard actually fires.

AGENT_LOOP_SIMPLIFICATION Phase 4.1 asks each guard to justify itself from
measured fire rates: never fires across the corpus -> delete it; fires often ->
it is pointing at prose still worth fixing. Until now 4.1 had no input, and
could not get any: `tooling/evals/providers.py` calls `dispatch()` directly, so
the eval never runs `run_agent_turn` -- and `guards_fired` is snapshotted
inside `run_turn`. **No number of eval runs can ever produce a guard fire
rate.** That is a property of the harness, not of the guards.

This closes it without a box. `scripts/local_turn.py` already drives a real
`chat_turn` in-process with the LLM and FSR-client seams pulled out, and the
connector already copies the fires onto the envelope (`_with_guard_fires` ->
`tags.guards_fired`). So: run scenarios that cover each guard's path, count
what fires.

**Use a real model.** All three guards live INSIDE the provider loop
(`openai_provider.py:460-465`, same in `anthropic_provider.py`), and
`--llm fake` replaces the provider wholesale -- so a fake turn cannot fire a
guard no matter what it scripts. Checked: the fake narrates verified YAML
without calling `emit_playbook_offer`, which is textbook `CreateDeliveryGuard`
territory, and it still reports 0/6 (the connector's `_salvage_build_offer`
covers that path instead). `--llm fake` is the plumbing smoke test, not the
instrument. `--llm frank` runs the openai provider path, where the guards are
live; Frank is free (see the screening doctrine in the plan).

The chain from a fire to this report is pinned at all three joints:
`test_guard_fire_telemetry` (guard -> record), the framework's
`test_guard_fire_reaches_turn_result` (record -> TurnResult, the positive
control), and `tests/test_guard_fire_envelope` (TurnResult -> envelope tags).
Without those, a 0 here would be unfalsifiable.

**A rate needs repeats.** Single runs lied three separate times in one
afternoon on this exact surface; a guard that fires on 1 of 3 runs is a flaky
signal, not a 33% rate. `--repeat` defaults to 3 and the report shows fires per
scenario, so a guard that fires on every repeat of one scenario reads
differently from one that fires once anywhere.

    ./.venv/bin/python scripts/guard_fire_rates.py            # frank, repeat 3
    ./.venv/bin/python scripts/guard_fire_rates.py --llm fake # plumbing check
    ./.venv/bin/python scripts/guard_fire_rates.py --json out.json

Needs FSRPB_DEV=1 when running against an editable `fsr_playbooks` (the
connector's version pin is a production check; see operations.py:436).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

# The three guards under test. Named explicitly so a guard that is DELETED
# from fsr_playbooks shows up here as a missing key rather than as a silent
# zero -- "never fired" and "no longer exists" are the same number and
# opposite conclusions.
GUARDS = ("EnhanceDeliveryGuard", "CreateDeliveryGuard", "BuildProgressGuard")

# A small playbook to hang the enhance/diagnose scenarios off. It has to be
# real enough that the turn has something to edit; the guards care about
# whether a terminal tool is reached, not about what the YAML says.
_CONTEXT_YAML = """\
collection: Guard Rate Probe
playbooks:
  - name: Block IP
    steps:
      - name: Start
        type: start
        next: Block
      - name: Block
        type: connector
        connector: fortigate-firewall
        operation: block_ip_new
        params:
          ip_addresses: "{{ vars.input.params.ip }}"
        next: Done
      - name: Done
        type: end
"""

# One scenario per path a guard covers. `BuildProgressGuard._RUN_INTENT_TOOLS`
# is the only cover for a RUN request on the un-narrowed build slice since
# Lever 2 was deleted, so `run_on_build_slice` is the scenario that matters
# most -- it is the plan's stated reason that guard should be the LAST one
# considered for removal.
SCENARIOS: tuple[dict[str, Any], ...] = (
    {"name": "build_new", "intent": "build",
     "message": "build me a playbook that blocks an IP on fortigate"},
    {"name": "build_research_heavy", "intent": "build",
     "message": "what connectors do I have for firewalls, and which "
                "operation blocks an IP? then build the playbook"},
    {"name": "enhance_open_playbook", "intent": "build",
     "playbook_yaml": _CONTEXT_YAML,
     "message": "add a manual approval step before the block"},
    {"name": "enhance_small_edit", "intent": "build",
     "playbook_yaml": _CONTEXT_YAML,
     "message": "rename the Block step to Quarantine IP"},
    {"name": "run_on_build_slice", "intent": "build",
     "playbook_yaml": _CONTEXT_YAML,
     "message": "run this playbook"},
    {"name": "diagnose_failure", "intent": "build",
     "playbook_yaml": _CONTEXT_YAML,
     "message": "why did the last run of this playbook fail?"},
)


def _seal_the_box() -> None:
    """Point FSR_* at a closed port so no turn can reach an appliance.

    `--tools cassette` seams the FSR client, but not every path goes through
    that seam: the failure-diagnosis lever builds its own client from
    `probes._env`, and a default run was observed opening real connections to
    a lab box (66s on the diagnose scenario, versus 0.0s sealed). That is the
    exact contamination this measurement cannot afford -- a degrading box has
    already been misread as agent regression once on this surface, and a
    guard-fire rate that silently depends on a box is not reproducible.

    Sealing rather than unsetting, because `_load_repo_env_once()` setdefaults
    the framework repo's `.env` into the process: unset variables come back,
    set ones do not. Paths that need a box degrade to "no data" and log it,
    which is the honest outcome -- the guards do not depend on live reads.
    """
    # Delegated, not re-implemented. The same seal now guards EVERY cassette
    # turn (`local_turn.seal_the_box`) -- a `make chat-sweep` run was found
    # reaching a lab appliance while calling itself box-free. Two copies of a
    # seal is one copy that can quietly stop matching the other.
    from scripts.local_turn import seal_the_box
    seal_the_box()
    print("box sealed: FSR_* -> 127.0.0.1:9 (pass --allow-live to disable)\n",
          file=sys.stderr)


def _fires(envelope: Any) -> list[str]:
    """Guard fires off the turn envelope. Absent = healthy, by contract."""
    if not isinstance(envelope, dict):
        return []
    return list((envelope.get("tags") or {}).get("guards_fired") or [])


def run(*, llm: str, repeat: int, model: Optional[str],
        base_url: Optional[str], scenarios: tuple[dict, ...]) -> dict:
    from scripts.local_turn import local_turn

    rows: list[dict[str, Any]] = []
    for rep in range(1, repeat + 1):
        for sc in scenarios:
            kwargs = {k: v for k, v in sc.items() if k not in ("name",)}
            t0 = time.time()
            err = None
            envelope: Any = None
            try:
                res = local_turn(messages=[{"role": "user",
                                            "content": kwargs.pop("message")}],
                                 llm=llm, model=model,
                                 frank_base_url=base_url, **kwargs)
                envelope = res.transcript
            except Exception as exc:  # noqa: BLE001
                # A dead scenario must not take the sweep down -- and it must
                # not be counted as "no guard fired" either, which is why the
                # error is carried on the row and excluded from the denominator.
                err = f"{type(exc).__name__}: {exc}"
            row = {
                "scenario": sc["name"], "repeat": rep,
                "elapsed_s": round(time.time() - t0, 1),
                "stop_reason": (envelope or {}).get("stop_reason")
                if isinstance(envelope, dict) else None,
                "guards_fired": _fires(envelope),
                "error": err,
            }
            rows.append(row)
            mark = ",".join(row["guards_fired"]) or ("ERR" if err else "-")
            print(f"  [{rep}/{repeat}] {sc['name']:<24} {mark}  "
                  f"({row['elapsed_s']}s)", file=sys.stderr, flush=True)
    return _report(rows, llm=llm, repeat=repeat, model=model)


def _report(rows: list[dict], **meta: Any) -> dict:
    scored = [r for r in rows if not r["error"]]
    tally = {g: {"fires": 0, "scenarios": {}} for g in GUARDS}
    unknown: dict[str, int] = {}
    for r in scored:
        for g in r["guards_fired"]:
            if g not in tally:
                # A guard we do not know about fired. Surface it rather than
                # dropping it -- the list above is a snapshot of the source.
                unknown[g] = unknown.get(g, 0) + 1
                continue
            tally[g]["fires"] += 1
            sc = r["scenario"]
            tally[g]["scenarios"][sc] = tally[g]["scenarios"].get(sc, 0) + 1
    return {
        "meta": dict(meta, turns=len(rows), scored=len(scored),
                     errors=len(rows) - len(scored)),
        "tally": tally, "unknown_guards": unknown, "rows": rows,
    }


def _print_report(rep: dict) -> None:
    m = rep["meta"]
    print(f"\nguard fire rates -- {m['scored']}/{m['turns']} turns scored "
          f"({m['errors']} errored), llm={m['llm']} repeat={m['repeat']}")
    print(f"{'guard':<24} {'fires':>10}  where")
    print("-" * 72)
    for g, t in rep["tally"].items():
        where = ", ".join(f"{k}×{v}" for k, v in sorted(t["scenarios"].items()))
        print(f"{g:<24} {t['fires']:>4}/{m['scored']:<5}  {where or '(never)'}")
    if rep["unknown_guards"]:
        print(f"\nunrecognised guards fired: {rep['unknown_guards']} "
              "-- update GUARDS in this file")
    if m["errors"]:
        print(f"\n{m['errors']} turn(s) errored and are EXCLUDED from the "
              "denominator -- a failed turn is not evidence a guard is idle:")
        for r in rep["rows"]:
            if r["error"]:
                print(f"  {r['scenario']} rep{r['repeat']}: {r['error'][:120]}")
    # The reading, stated so the number is not left to interpretation.
    print("\nReading (plan §4.1): 0 fires across a real-model sweep is the "
          "case for deleting a guard; frequent fires mean it is compensating "
          "for prose worth fixing. Neither conclusion is safe from `--llm "
          "fake`, which never lets a guard fire.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", default="frank",
                    choices=("frank", "openai", "anthropic", "fake"),
                    help="fake = plumbing smoke test only; it cannot measure a "
                         "guard, because a scripted turn always reaches the "
                         "terminal tool")
    ap.add_argument("--repeat", type=int, default=3,
                    help="single runs lie on this surface (default 3)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--scenario", action="append", default=None,
                    help="run only these scenarios by name (repeatable)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the full report (incl. per-turn rows) here")
    ap.add_argument("--allow-live", action="store_true",
                    help="let tool calls reach whatever FSR_* points at "
                         "(default: they cannot -- see _seal_the_box)")
    args = ap.parse_args(argv)

    if not args.allow_live:
        _seal_the_box()

    if args.llm == "fake":
        print("NOTE: --llm fake measures the plumbing, not the guards.\n",
              file=sys.stderr)
    if not os.getenv("FSRPB_DEV"):
        print("NOTE: FSRPB_DEV is unset; an editable fsr_playbooks will trip "
              "the connector's version pin.\n", file=sys.stderr)

    scenarios = SCENARIOS
    if args.scenario:
        want = set(args.scenario)
        scenarios = tuple(s for s in SCENARIOS if s["name"] in want)
        if not scenarios:
            raise SystemExit(f"no scenario matched {sorted(want)}; have "
                             f"{[s['name'] for s in SCENARIOS]}")

    rep = run(llm=args.llm, repeat=args.repeat, model=args.model,
              base_url=args.base_url, scenarios=scenarios)
    _print_report(rep)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, indent=2, default=str))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
