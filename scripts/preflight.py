#!/usr/bin/env python3
"""T0 preflight — can the discovery tools resolve what a scenario ASKS FOR?

Offline-testing plan Task B (`docs/plans/widget-capability-test-and-persona-
rollout.md` §6e). **Assert it before spending a run.**

Every build/troubleshoot scenario names things in analyst language — a connector
("FortiSOAR Utilities"), an operation ("Convert String Time to Minutes"), a step
type. The model can only be as good as what the discovery tools hand back for
those exact words. When a tool returns a confidently WRONG match, the model
reports it faithfully and the scenario fails *looking like a model defect*.

That is not hypothetical: S3 scored 0/5 because `find_connector` searched only
the first word of the query, returned `fortisoar-ml-engine` for "FortiSOAR
Utilities", and never searched "Utilities" — where the answer (`cyops_utilities`,
label literally "Utilities") was. Cost to find: a release → PyPI → bump → ship →
20-minute box eval. Cost to find here: **milliseconds, no box, no model**.

So this file asserts, per scenario and using the ask's OWN phrasing:

  * the connector query resolves to the expected connector,
  * the operation query resolves to the expected operation,
  * every step type the scenario needs is a real step type,
  * every tool the scenario depends on is in its intent's slice
    (the `eval_s5_diagnose._build_slice_has_tool()` idiom, generalised).

It is pure lookup against the packaged catalog — no LLM, no box, no ship. Run it
before any S-slice, and in CI via `tests/test_preflight_resolves.py`.

    FSRPB_DEV=1 .venv/bin/python scripts/preflight.py            # all scenarios
    FSRPB_DEV=1 .venv/bin/python scripts/preflight.py s3 --verbose
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# The registry — what each scenario's ask names, in the ask's own words.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    """One scenario's discovery preconditions.

    `connector` / `operation` queries are deliberately the HUMAN phrasing from
    the scenario's ASK, not the canonical name. Preflighting the canonical name
    would prove nothing — the model never types it; it types what the analyst
    said. That distinction is the entire value of this oracle.
    """

    why: str
    intent: str = "build"
    # (query, expected canonical connector name)
    connectors: tuple[tuple[str, str], ...] = ()
    # (connector, query, expected canonical op name)
    operations: tuple[tuple[str, str, str], ...] = ()
    step_types: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    # Rank tolerance: the expected match must appear within the first N hits.
    # A hit buried at rank 40 is not a hit the model will act on.
    rank_within: int = 10


SCENARIOS: dict[str, Spec] = {
    "s1": Spec(
        why="create a runnable playbook from scratch (alerts, create_record)",
        step_types=("start", "create_record"),
        tools=("verify_playbook", "validate_yaml"),
    ),
    "s2": Spec(
        why="modify the open playbook, changing only what was asked",
        step_types=("start", "create_record"),
        tools=("analyze_playbook", "verify_enhancement", "validate_yaml"),
    ),
    "s3": Spec(
        why="author a connector-action playbook (the slice that caught "
            "find_connector's first-word bug)",
        connectors=(("FortiSOAR Utilities", "cyops_utilities"),),
        operations=(("cyops_utilities", "Convert String Time to Minutes",
                     "convert_periodic_time_to_minutes"),),
        step_types=("start", "connector", "create_record"),
        tools=("find", "get_op_schema", "verify_playbook"),
    ),
    "s4": Spec(
        why="trigger / conditional / jinja authoring",
        step_types=("start", "decision", "set_variable"),
        tools=("get_step_type", "find", "validate_yaml"),
    ),
    "s5": Spec(
        why="diagnose a failed run — the 0.4.78 diagnose-before-fix lever",
        step_types=("create_record",),
        tools=("why_did_playbook_fail", "analyze_playbook"),
    ),
    "s6": Spec(
        why="apply a grounded fix to a broken playbook and re-run it",
        step_types=("create_record",),
        tools=("why_did_playbook_fail", "verify_enhancement", "validate_yaml"),
    ),
}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
@dataclass
class Result:
    scenario: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _names(matches: Any, *keys: str) -> list[str]:
    out: list[str] = []
    for m in (matches or []):
        if not isinstance(m, dict):
            continue
        for k in keys:
            if m.get(k):
                out.append(str(m[k]))
                break
    return out


def check(name: str, spec: Spec, *, verbose: bool = False) -> Result:
    from fsr_playbooks.llm.intents import tools_for_intent
    from fsr_playbooks.mcp_server.tools_discovery import (
        find_connector, find_operation, get_step_type)

    res = Result(name)

    for query, expect in spec.connectors:
        hits = _names(find_connector(query, limit=spec.rank_within).get("matches"),
                      "name")
        if expect not in hits:
            res.failures.append(
                f"find_connector({query!r}) did not return {expect!r} — got "
                f"{hits or '[]'}. The model will report whatever this says.")
        else:
            res.notes.append(
                f"connector {query!r} → {expect} (rank {hits.index(expect) + 1}"
                f"/{len(hits)})")

    for connector, query, expect in spec.operations:
        hits = _names(
            find_operation(connector, query, limit=spec.rank_within).get("matches"),
            "op_name", "name")
        if expect not in hits:
            res.failures.append(
                f"find_operation({connector!r}, {query!r}) did not return "
                f"{expect!r} — got {hits or '[]'}")
        else:
            res.notes.append(f"operation {query!r} → {expect}")

    for st in spec.step_types:
        got = get_step_type(st)
        # A known step type answers with its schema; an unknown one answers with
        # an error envelope carrying `suggestions`. `ok` is absent on success.
        if got.get("ok") is False or not got.get("name"):
            res.failures.append(
                f"get_step_type({st!r}) is not a known step type "
                f"(near: {got.get('near')})")
        else:
            res.notes.append(f"step_type {st} → {got.get('name')}")

    if spec.tools:
        slice_names = {t["name"] for t in tools_for_intent(spec.intent)}
        missing = [t for t in spec.tools if t not in slice_names]
        if missing:
            res.failures.append(
                f"intent {spec.intent!r} slice is missing {missing} — the "
                "scenario asks for something the model cannot reach. (Is "
                "FSRPB_DEV set / the editable framework on the path?)")
        else:
            res.notes.append(
                f"intent {spec.intent} slice has all {len(spec.tools)} tools")

    if verbose:
        for n in res.notes:
            print(f"    · {n}")
    return res


def run(scenarios: Optional[list[str]] = None, *, verbose: bool = False) -> int:
    wanted = scenarios or sorted(SCENARIOS)
    unknown = [s for s in wanted if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s) {unknown}; "
                         f"have {sorted(SCENARIOS)}")

    failed = 0
    for name in wanted:
        spec = SCENARIOS[name]
        res = check(name, spec, verbose=verbose)
        mark = "PASS" if res.ok else "FAIL"
        print(f"[{mark}] {name} — {spec.why}")
        for f in res.failures:
            print(f"    ✗ {f}")
        failed += 0 if res.ok else 1

    print(f"\n{len(wanted) - failed}/{len(wanted)} scenarios preflight clean.")
    if failed:
        print("A FAIL here is a TOOL/CATALOG defect, not a model defect — "
              "fix it before spending a live run.")
    return 1 if failed else 0


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="T0 scenario preflight (offline)")
    ap.add_argument("scenario", nargs="*", help="e.g. s3 (default: all)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show what each lookup resolved to, with rank")
    args = ap.parse_args(argv)
    return run(args.scenario or None, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(_main())
