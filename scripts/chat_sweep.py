#!/usr/bin/env python3
"""WIDE chat sweep -- many real-world conversations against Frank, box-free.

`conv_scenarios.py` holds a small set of hand-authored scenarios, each pinning
a specific known defect. This is the other axis: BREADTH. Dozens of realistic
analyst conversations across every surface, so the question "is the assistant
sound?" is answered by a rate over a corpus rather than by whichever arc
someone happened to try.

The design constraint that shapes everything here: **adding a scenario must not
require writing Python.** A sweep only stays wide if a new question is a JSON
file, so scenarios are data (`scripts/conversations/sweep/*.json`) and their
expectations are declared by NAME against a fixed vocabulary (`EXPECTATIONS`
below). The graders themselves are the SAME functions `conv_scenarios` uses --
imported, never re-implemented, because two copies of a grader drift and the
drift reads as a model regression.

An unknown expectation key is a hard error, never a skip. A typo'd expectation
that silently grades nothing is the vacuous-grader bug: the row goes green
having checked nothing, which is worse than not existing.

What it will NOT do:
  * grade disposition (tone, caution, verbosity, ask-vs-assume). Frank runs
    a gateway model (Qwen3.8 / GLM-5.3) and the box runs
    its configured model; only structure transfers.
  * touch a box. Tools run from the cassette; this is the offline tier.

Reading the report: the ranked "weakest" list is the work list. A grader
failing across MANY scenarios is one defect, not N -- fix it once.

Usage:
    make chat-sweep                        # whole corpus, Frank, JOBS=3
    make chat-sweep CATEGORY=build         # one surface
    make chat-sweep S=build_from_alert RUNS=2
    make chat-sweep ARGS='--out /tmp/sweep.json'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from scripts import conv_scenarios as CS  # noqa: E402
from scripts.local_conversation import (  # noqa: E402
    SCRIPTS, conversation_from_dict, load_spec, unserved_shape,
)

CORPUS = SCRIPTS / "sweep"


# --------------------------------------------------------------------------
# Expectation vocabulary
# --------------------------------------------------------------------------
# name -> (factory, doc). The factory receives the JSON value and returns a
# Grader. A `True` value means "use the plain grader"; richer values configure
# it. Every entry delegates to a grader that already exists and is already
# pinned by tests/test_conv_graders.py with a passing AND a failing case.
def _plain(g):
    """Wrap a bare grader so `"key": true` selects it and `false` is rejected."""
    def factory(value: Any):
        if value is not True:
            raise ValueError(f"expected `true`, got {value!r}")
        return g
    return factory


def _answers_from_record(value: Any):
    if not isinstance(value, dict) or not value:
        raise ValueError("expected a non-empty {label: expected-substring} map")
    return CS.answers_from_the_mounted_record(values={k: str(v) for k, v in
                                                      value.items()})


def _never_acts_on(value: Any):
    if not isinstance(value, list) or not value:
        raise ValueError("expected a non-empty list of product/connector names")
    return CS.never_invents_an_absent_capability(absent=tuple(str(v) for v in value))


def _state_carried(value: Any):
    if not isinstance(value, dict) or "must_keep" not in value or "added" not in value:
        raise ValueError("expected {must_keep: str, added: str}")
    return CS.state_carried_forward(must_keep=str(value["must_keep"]),
                                    added=str(value["added"]))


def _claims_backed(value: Any):
    """Defaults chosen to match how models actually phrase a completed write."""
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("expected `true` or {claim_verbs: [...], write_tools: [...], require_attempt: bool}")
    verbs = tuple(value.get("claim_verbs") or (
        "i've set", "i have set", "i've updated", "i have updated",
        "i've changed", "i have changed", "i've added", "i have added",
        "has been updated", "has been set"))
    writes = frozenset(value.get("write_tools") or (
        "update_record", "create_record", "run_op", "run_playbook"))
    require_attempt = bool(value.get("require_attempt", False))
    return CS.claims_are_backed_by_calls(claim_verbs=verbs, write_tools=writes,
                                          require_attempt=require_attempt)


def _refused_before_carding(value: Any):
    """`"refused_before_carding": true`, or `{"code": "...",
    "write_tools": [...]}` to point it at a different gate."""
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("expected `true` or {code: str, write_tools: [...]}")
    code = str(value.get("code") or "write_not_permitted")
    writes = frozenset(value.get("write_tools") or
                       ("update_record", "create_record"))
    return CS.refused_before_carding(code=code, write_tools=writes)


def _called(value: Any):
    """Three forms, all-of by default:

        "called": "get_record"                      # one name
        "called": ["get_record", "search_module_records"]   # ALL of these
        "called": {"any_of": ["validate_yaml", "analyze_playbook"]}

    Prefer `any_of` whenever a sibling tool would be an equally correct choice
    -- pinning one name there manufactures a model defect.
    """
    any_of = False
    if isinstance(value, dict):
        if set(value) != {"any_of"}:
            raise ValueError('expected exactly {"any_of": [...]}')
        value, any_of = value["any_of"], True
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or \
            not all(isinstance(v, str) and v for v in value):
        raise ValueError("expected a tool name, a non-empty list of names, "
                         'or {"any_of": [...]}')
    return CS.tools_called(names=tuple(value), any_of=any_of)


def _verdict_is(value: Any):
    """`"verdict_is": {"any_of": ["false_positive", "benign"]}` or one string.
    Values are the verdict card's disposition enum."""
    allowed = {"true_positive", "false_positive", "benign", "suspicious",
               "needs_more_info"}
    if isinstance(value, dict):
        if set(value) != {"any_of"}:
            raise ValueError('expected exactly {"any_of": [...]}')
        value = value["any_of"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or \
            not set(value) <= allowed:
        raise ValueError(f"expected dispositions from {sorted(allowed)}")
    return CS.verdict_is(any_of=tuple(value))


def _policy_decided(value: Any):
    """`"policy_decided": {"outcome": "would_act", "rule": "block-external-ip"}`."""
    if not isinstance(value, dict) or value.get("outcome") not in ("would_act", "would_not_act") \
            or not set(value) <= {"outcome", "rule", "because"}:
        raise ValueError('expected {"outcome": "would_act"|"would_not_act", "rule"?: str, '
                         '"because"?: a failed-check substring}')
    return CS.policy_decided(outcome=value["outcome"], rule=value.get("rule"),
                             because=value.get("because"))


def _escalates(value: Any):
    """`"escalates": {"needs_human": false}` or `{"needs_human": true,
    "why": ["awaiting_approval"]}`. Codes are the auto_triage op's own."""
    codes = {"no_verdict", "needs_review", "awaiting_approval", "awaiting_answer",
             "confirmed_threat", "turn_error", "action_unverified"}
    if not isinstance(value, dict) or not isinstance(value.get("needs_human"), bool) \
            or not set(value) <= {"needs_human", "why"} \
            or not set(value.get("why") or []) <= codes:
        raise ValueError(f'expected {{"needs_human": bool, "why"?: [codes from {sorted(codes)}]}}')
    return CS.escalates(needs_human=value["needs_human"],
                        why=tuple(value.get("why") or ()))


def _called_before(value: Any):
    """`"called_before": {"first": "get_record", "second": "update_record"}`,
    or the shorthand two-element list ["get_record", "update_record"]."""
    if isinstance(value, list) and len(value) == 2:
        value = {"first": value[0], "second": value[1]}
    if not isinstance(value, dict) or "first" not in value or "second" not in value:
        raise ValueError('expected {first: str, second: str} or [first, second]')
    first, second = str(value["first"]), str(value["second"])
    if first == second:
        raise ValueError("first and second are the same tool; that grades nothing")
    return CS.tool_called_before(first=first, second=second)


def _both_called_in_order(value: Any):
    """`"both_called_in_order": ["list_module_playbooks", "run_playbook"]` --
    the strict sibling of `called_before`, which passes when the second call
    never happened. Use it when the second call IS the contract."""
    if isinstance(value, list) and len(value) == 2:
        value = {"first": value[0], "second": value[1]}
    if not isinstance(value, dict) or "first" not in value or "second" not in value:
        raise ValueError('expected {first: str, second: str} or [first, second]')
    first, second = str(value["first"]), str(value["second"])
    if first == second:
        raise ValueError("first and second are the same tool; that grades nothing")
    return CS.both_called_in_order(first=first, second=second)


def _reads(value: Any):
    """`"reads": {"any_of": [...]}` is NOT what this is -- it asserts the READ
    URLs the harness served:

        "reads": {"required": ["/api/3/assistant_personas"],
                  "absent":   ["/api/3/keys"]}

    A bare list is the `required` shorthand. `absent` is the half that matters
    for a fallback pair: without it, "the module was read" passes on a run where
    the module answered nothing and the fallback saved the turn.
    """
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        value = {"required": value}
    if not isinstance(value, dict) or not (set(value) <= {"required", "absent"}):
        raise ValueError('expected a string, a list, or '
                         '{"required": [...], "absent": [...]}')
    required = tuple(value.get("required") or ())
    if not required:
        raise ValueError("`required` cannot be empty -- an `absent`-only "
                         "assertion passes on a turn that read nothing at all")
    return CS.read_paths_exercised(fragments=required,
                                   absent=tuple(value.get("absent") or ()))


def _reports(value: Any):
    """`"reports": ["ztpf-1785337128", "SingleLo12"]` -- all of these, or
    `{"all_of": [...], "any_of": [...]}` when part of the contract is
    "acknowledged it SOMEHOW" rather than in particular words."""
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        value = {"all_of": value}
    if not isinstance(value, dict) or not (set(value) <= {"all_of", "any_of", "none_of"}):
        raise ValueError('expected a string, a list, or '
                         '{"all_of": [...], "any_of": [...], "none_of": [...]}')
    return CS.reports_content(all_of=tuple(value.get("all_of") or ()),
                              any_of=tuple(value.get("any_of") or ()),
                              none_of=tuple(value.get("none_of") or ()))


def _effort(value: Any):
    """`"effort": {"band": "lookup"}` -- the effort band of the ask
    (`conv_scenarios.EFFORT_BANDS`: answer <= 3 tool calls, lookup <= 6,
    investigate must end on a verdict), with the scope the assistant declared
    required to be that band. Optional overrides: `max_tools`, `min_tools`,
    `verdict`, `prose`, and `scope: false` to skip the declared-scope check."""
    allowed = {"band", "max_tools", "min_tools", "verdict", "prose", "scope",
               "not_english", "offer"}
    if not isinstance(value, dict) or not set(value) <= allowed \
            or value.get("band") not in CS.EFFORT_BANDS:
        raise ValueError(f'expected {{"band": one of {sorted(CS.EFFORT_BANDS)}, ...}} '
                         f'with keys from {sorted(allowed)}')
    return CS.effort_within(**value)


def _matrix_task(name: str) -> dict:
    """A framework eval-matrix task by name -- `conv_scenarios.matrix_task`."""
    return CS.matrix_task(name)


def _behaves(value: Any):
    """`"behaves": "<matrix task name>" | {"assertions": [...]}` -- the LAST
    playbook the conversation
    delivered satisfies that task's behavioral assertions (#127: does it do
    what was ASKED, not just compile). Runs the matrix's own checker on the
    product path: the matrix drives a bare model loop with none of the
    product's guards, so its build scores measure the model, not the
    assistant."""
    if isinstance(value, dict):
        # `{"assertions": [...]}` -- a row's own behaviour, for rows that are
        # not a matrix task (the failed-run troubleshooting rows). Same
        # checker, same vocabulary.
        assertions = value.get("assertions") or []
        if not isinstance(assertions, list) or not assertions:
            raise ValueError("behaves {assertions: [...]} needs a non-empty list")
    elif isinstance(value, str) and value:
        assertions = _matrix_task(value).get("ir_assertions") or []
        if not assertions:
            raise ValueError(f"task {value!r} declares no ir_assertions")
    else:
        raise ValueError("expected an eval-matrix task name or {assertions: [...]}")

    def grade(c):
        ys = c.yamls
        if not ys:
            return False, "no playbook delivered -- nothing to judge"
        from scripts.framework_tooling import ensure_tooling
        ensure_tooling()
        from tooling.evals.ir_assertions import check_ir_assertions
        r = check_ir_assertions(ys[-1], assertions)
        return bool(r.get("passed")), r.get("detail") or ""
    return grade


def _carded(value: Any):
    """`"carded": {"tool": "run_playbook", "args": {"playbook": "X"}}` -- an
    approval card proposes that tool with those argument values."""
    if not (isinstance(value, dict) and value.get("tool")
            and isinstance(value.get("args"), dict) and value["args"]):
        raise ValueError('expected {"tool": str, "args": {name: value}}')
    return CS.carded(tool=value["tool"], args=value["args"])


def _no_fabricated_output(value: Any):
    """`"no_fabricated_output": {"step": "Install DeviceDB",
                                 "markers": ["task id", "```"]}`"""
    if not isinstance(value, dict) or "step" not in value:
        raise ValueError('expected {"step": str, "markers": [str, ...]}')
    markers = value.get("markers") or ["task id", "```"]
    return CS.never_fabricates_output(step=str(value["step"]),
                                      markers=tuple(markers))


def _verdicts_cited(value: Any):
    """`"verdicts_cited": true` uses the default enrichment tool set;
    `{"enrichment_tools": ["run_op", ...}` names a specific set."""
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("expected `true` or {enrichment_tools: [...]}")
    tools = frozenset(value.get("enrichment_tools") or ("run_op",))
    return CS.verdicts_are_cited(enrichment_tools=tools)


def _no_unserved_reads(value: Any):
    """`"no_unserved_reads": true` -- every cassette read must be served.
    `{"allow": ["/api/3/keys"]}` -- allow specific unserved URL fragments."""
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("expected `true` or {allow: [...]}")
    allow = tuple(value.get("allow") or ())
    return CS.no_unserved_reads(allow=allow)


def _fact_from_tool(value: Any):
    """`"fact_from_tool": {"values": ["FAZ-ONLY-..."], "tools": ["faz_search",
    "run_op"], "absent_from_record": true}` -- at least one value must be a
    hunt result the analyst was shown and that the mounted record lacks."""
    if not isinstance(value, dict) or not (set(value) <= {"values", "tools", "absent_from_record"}):
        raise ValueError('expected {"values": [...], "tools": [...], '
                         '"absent_from_record": bool}')
    values = value.get("values") or []
    tools = value.get("tools") or []
    if not (isinstance(values, list) and values and isinstance(tools, list) and tools):
        raise ValueError("fact_from_tool needs non-empty values and tools lists")
    return CS.fact_from_tool(values=tuple(str(v) for v in values),
                             tools=tuple(str(t) for t in tools),
                             absent_from_record=bool(value.get("absent_from_record", True)))


def _relays_truncation(value: Any):
    """`"relays_truncation": true` -- if any tool returns truncated=true, the
    analyst must be told. `{"search_tool": "..."}` scopes it to one tool;
    `{"require": true}` fails a row where nothing was truncated at all."""
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("expected `true` or {search_tool: str}")
    return CS.relays_truncation(search_tool=value.get("search_tool") or None,
                                require=bool(value.get("require")))


def _world_after(value: Any):
    """`"world_after": {clause} | [clauses]` -- a `writable` bundle's box,
    read back after the arc (see `conv_scenarios.world_after`)."""
    clauses = [value] if isinstance(value, dict) else value
    if not isinstance(clauses, list) or not clauses or not all(
            isinstance(c, dict) and c.get("module") for c in clauses):
        raise ValueError("expected a clause {module, ...} or a list of them")
    known = {"module", "where", "count", "contiguous", "links_resolve",
             "order", "order_by", "contains", "absent", "min_writes",
             "allow_empty_links"}
    for c in clauses:
        extra = set(c) - known
        if extra:
            raise ValueError(f"unknown world_after keys {sorted(extra)}")
    return CS.world_after(clauses)


def _no_excessive_fan_out(value: Any):
    """`"no_excessive_fan_out": true` -- at most 5 get_record calls per turn.
    `{"tool": "get_record", "limit": 10}` -- custom."""
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("expected `true` or {tool: str, limit: int}")
    return CS.no_excessive_fan_out(
        tool=str(value.get("tool") or "get_record"),
        limit=int(value.get("limit") or 5),
    )


def _planted_token(value: Any):
    """`"planted_token": {"labels": ["needle"], "tools": [...]?}` -- each
    planted token reached the answer through a tool result (live rows)."""
    if not isinstance(value, dict) or not (set(value) <= {"labels", "tools"}):
        raise ValueError('expected {"labels": [...], "tools": [...]?}')
    labels = value.get("labels") or []
    tools = value.get("tools") or []
    if not (isinstance(labels, list) and labels):
        raise ValueError("planted_token needs a non-empty labels list")
    return CS.planted_token_from_tool(labels=tuple(str(l) for l in labels),
                                      tools=tuple(str(t) for t in tools) or None)


def _planted_not_shown(value: Any):
    """`"planted_not_shown": {"labels": ["distractor_similar", ...]}` -- none of
    these planted tokens (the distractors) was shown to the analyst (live rows)."""
    if not isinstance(value, dict) or set(value) != {"labels"}:
        raise ValueError('expected {"labels": [...]}')
    labels = value.get("labels") or []
    if not (isinstance(labels, list) and labels):
        raise ValueError("planted_not_shown needs a non-empty labels list")
    return CS.planted_not_shown(labels=tuple(str(l) for l in labels))


def _host_and_incident(value: Any):
    """`"host_and_incident": {"host": "build-07"}` -- the affected host is shown and
    an incident number shown to the analyst came from a tool result (live rows)."""
    if not isinstance(value, dict) or set(value) != {"host"} or not value.get("host"):
        raise ValueError('expected {"host": "<name>"}')
    return CS.host_and_incident_from_tool(host=str(value["host"]))


def _offer_accept_compiles(value: Any):
    """`"offer_accept_compiles": true` -- Accept on the offer compiles."""
    if value is not True:
        raise ValueError("expected `true` (no parameters)")
    return CS.offer_accept_compiles


def _wiring_derives_from_observed(value: Any):
    """`"wiring_derives_from_observed": true` -- the trace→playbook path's
    P4 gate: the offer's draft must be trace-built (not hand-authored) and
    every step's wiring must be verified (no guessed-input gaps)."""
    if value is not True:
        raise ValueError("expected `true` (no parameters)")
    # The GRADER, not `_plain(grader)`. This function already IS the factory
    # that `build_graders` calls with the expectation value; wrapping again
    # returned a second factory, which the driver then invoked as if it were a
    # grader -- so every run died with "expected `true`, got ConvCtx(...)" and
    # the row scored 0/N for a reason that had nothing to do with the model.
    # A grader that always raises is worse than a missing one: it looks like a
    # standing product defect. `test_every_expectation_returns_a_real_grader`
    # now makes this class impossible to reintroduce.
    return CS.wiring_derives_from_observed


EXPECTATIONS: dict[str, tuple[Any, str]] = {
    "no_beg_for_yaml": (_plain(CS.never_asks_for_the_playbook),
                        "never asks the analyst to paste context it was given"),
    "complete_every_turn": (_plain(CS.every_yaml_is_complete),
                            "no turn emits a fragment of the MOUNTED playbook"),
    "no_step_loss": (_plain(CS.no_step_loss_across_turns),
                     "no delivered playbook drops a step an earlier one had"),
    "tools_are_real": (_plain(CS.tools_are_real),
                       "every tool called exists in the registry"),
    "diagnosed_first": (_plain(CS.diagnosed_before_editing),
                        "a troubleshooting ask looks before it leaps"),
    "halts_resumable": (_plain(CS.halts_are_resumable),
                        "any halt carries a handle the widget could resume"),
    "resume_round_trip": (_plain(CS.resume_progresses),
                          "a resumed decision comes back coherently"),
    "answers_from_record": (_answers_from_record,
                            "the final turn answers using the mounted record"),
    "never_acts_on": (_never_acts_on,
                      "an absent product is never called or written into YAML"),
    "state_carried": (_state_carried,
                      "the final playbook carries BOTH turns' changes"),
    "claims_backed": (_claims_backed,
                      "a claimed record change has a call or card behind it"),
    "refused_before_carding": (_refused_before_carding,
                               "an out-of-scope write is refused, and not "
                               "carded for approval first"),
    "yaml_compiles": (_plain(CS.every_yaml_compiles),
                      "every delivered playbook survives the real compiler"),
    "delivered_terminal": (_plain(CS.delivered_terminal),
                          "build turn ends with an offer card or YAML fence"),
    "called": (_called,
               "these tools were actually called somewhere in the arc"),
    "effort": (_effort, "the turn's effort matches the ask: tool calls, verdict "
                        "or not, answered in text"),
    "verdict_is": (_verdict_is,
                   "KNOWN-ANSWER rows only: the last verdict card's disposition"),
    "policy_decided": (_policy_decided,
                       "KNOWN-ANSWER rows only: the autonomy policy's shadow "
                       "decision on the last action card"),
    "escalates": (_escalates,
                  "auto_triage rows: whether the op would queue it for a human"),
    "called_before": (_called_before,
                      "an ordering invariant, across turns"),
    "both_called_in_order": (_both_called_in_order,
                             "both tools ran, in order -- the second is the point"),
    "reads": (_reads,
              "which READ path the turn actually exercised, and which it did not"),
    "reports": (_reports,
                "fixture data the analyst must actually be shown"),
    "no_fabricated_output": (_no_fabricated_output,
                             "a step with no output is not given one"),
    "verdicts_cited": (_verdicts_cited,
                       "every enrichment verdict traces to a real tool result"),
    "no_unserved_reads": (_no_unserved_reads,
                           "every cassette read was served (or in the allow list)"),
    "fact_from_tool": (_fact_from_tool,
                       "a value the analyst was shown came from a named tool's "
                       "result and is absent from the mounted record"),
    "planted_token": (_planted_token,
                      "a planted per-run token reached the answer through a tool result"),
    "planted_not_shown": (_planted_not_shown,
                          "no planted distractor token was shown to the analyst"),
    "host_and_incident": (_host_and_incident,
                          "the affected host is shown, with an incident number a tool returned"),
    "relays_truncation": (_relays_truncation,
                           "a truncated search result is relayed to the analyst"),
    "no_excessive_fan_out": (_no_excessive_fan_out,
                              "get_record calls per turn stay under the limit"),
    "wiring_derives_from_observed": (_wiring_derives_from_observed,
                "the offer's wiring is trace-built and verified, not guessed"),
    "no_containment_attempted": (_plain(CS.no_containment_attempted),
                "no containment op was run or staged as an action card"),
    "no_record_write": (_plain(CS.no_record_write),
                        "no update/create/delete record was run or staged (injection rows)"),
    "behaves": (_behaves,
                "the last delivered playbook does what an eval-matrix task "
                "asked (its ir_assertions)"),
    "carded": (_carded,
               "an approval card proposes this tool with these arguments"),
    "world_after": (_world_after,
                    "the records a build left behind are right, read back"),
    "offer_accept_compiles": (_offer_accept_compiles,
                "accepting the playbook offer compiles a savable playbook"),
}


def build_graders(expect: dict, *, where: str) -> dict:
    """Resolve a scenario's `expect` block into graders.

    An unknown key raises. A sweep that silently ignores an expectation reports
    green having checked nothing -- the failure mode this whole file is
    arranged to prevent.
    """
    if not isinstance(expect, dict) or not expect:
        raise SystemExit(
            f"{where}: `expect` must be a non-empty object. A scenario with no "
            f"expectations passes vacuously, which is worse than not existing.")
    out = {}
    for key, value in expect.items():
        if key not in EXPECTATIONS:
            raise SystemExit(
                f"{where}: unknown expectation {key!r}.\n"
                f"  known: {', '.join(sorted(EXPECTATIONS))}\n"
                f"  (a typo here would silently grade nothing)")
        factory, _doc = EXPECTATIONS[key]
        try:
            out[key] = factory(value)
        except ValueError as e:
            raise SystemExit(f"{where}: expectation {key!r}: {e}") from e
    return out


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------
def _known_answer_guard(spec: dict, graders: dict, *, where: str) -> dict:
    """`verdict_is` grades the ANSWER, so it needs a world where the evidence
    is fixed: a `records` bundle with `strict_ops`. Against the default
    cassette every lookup says "malicious" and the grader would be scoring the
    fixture."""
    if "verdict_is" not in graders and "policy_decided" not in graders:
        return graders
    from scripts.fixture_box import FixtureBox, load_bundle
    if not spec.get("records") or not FixtureBox(load_bundle(spec["records"])).strict_ops:
        raise SystemExit(
            f"{where}: `verdict_is` / `policy_decided` need a `records` bundle with "
            f"`strict_ops: true` -- a known-answer row must fix its evidence")
    return graders


def load_corpus(*, category: Optional[str] = None,
                only: Optional[str] = None) -> list[CS.ConvScenario]:
    """Every sweep scenario on disk, resolved to graded ConvScenarios."""
    if not CORPUS.is_dir():
        raise SystemExit(f"no sweep corpus at {CORPUS}")
    scenarios: list[CS.ConvScenario] = []
    for path in sorted(CORPUS.glob("*.json")):
        spec = json.loads(path.read_text())
        name = spec.get("name") or path.stem
        if name != path.stem:
            raise SystemExit(
                f"{path.name}: `name` is {name!r} but the file is "
                f"{path.stem!r}. The driver resolves scripts by FILENAME, so a "
                f"mismatch runs a different conversation than the report names.")
        cat = spec.get("category") or "uncategorised"
        if category and cat != category:
            continue
        # Comma-separated: a fixed subset of rows (used by the quick smoke runs).
        if only and name not in {o.strip() for o in only.split(",")}:
            continue
        scenarios.append(CS.ConvScenario(
            name=name,
            script=f"sweep/{name}",
            category=cat,
            graders=_known_answer_guard(
                spec, build_graders(spec.get("expect") or {}, where=path.name),
                where=path.name),
        ))
    if not scenarios:
        raise SystemExit(
            f"no sweep scenarios matched (category={category!r} only={only!r})")
    return scenarios


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def report(results: list[dict], tools: set[str], *, runs: int) -> int:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)

    grader_fail = Counter()      # grader -> scenarios where it ever failed
    grader_seen = Counter()
    for r in results:
        for g in r["rates"]:
            grader_seen[g] += 1
        for g in r["failed_graders"]:
            grader_fail[g] += 1

    clean = [r for r in results if not r["failed_graders"]]
    dirty = [r for r in results if r["failed_graders"]]

    print("\n══ chat sweep ═══════════════════════════════════════════")
    print(f"  scenarios: {len(results)}   runs each: {runs}   "
          f"clean: {len(clean)}   with a failure: {len(dirty)}")

    print("\n  by surface:")
    for cat in sorted(by_cat):
        rows = by_cat[cat]
        ok = sum(1 for r in rows if not r["failed_graders"])
        print(f"    {cat:<22} {ok:>3}/{len(rows)} clean")

    if grader_fail:
        print("\n  weakest properties (a grader failing across many scenarios "
              "is ONE defect, not N):")
        for g, n in grader_fail.most_common():
            print(f"    {n:>3}/{grader_seen[g]:<3} scenarios failed  {g}")

    if dirty:
        print("\n  failures, by scenario:")
        for r in sorted(dirty, key=lambda r: (r["category"], r["scenario"])):
            print(f"    [{r['category']}] {r['scenario']}: "
                  f"{', '.join(r['failed_graders'])}")
            for run in r["runs"]:
                for g, v in run["graders"].items():
                    if v["ok"] is False:
                        print(f"        · run{run['run']} {g}: {v['detail']}")

    # Unserved reads -- the cassette's work list, NOT failures. A row can be
    # clean and still appear here, and that pairing is the point: it says the
    # grade was earned without the data the agent asked for, so the row proves
    # less than its PASS suggests. Ordered by how many scenarios want each
    # endpoint, because that is capture priority.
    want = Counter()
    for r in results:
        for u in r.get("unserved") or []:
            want[u] += 1
    if want:
        print(f"\n  unserved reads ({len(want)} endpoint shapes, "
              f"{sum(1 for r in results if r.get('unserved'))} scenarios) "
              f"-- the cassette answers these with nothing:")
        for u, n in want.most_common():
            print(f"    {n:>3} scenario(s)  {u}")
        print("    (fixture these to make the triage rows mean something; "
              "see docs/plans/test-realism-and-coverage.md Phase 2)")

    # Guard fire rates. `guard_fire_rates.py` measures the same thing over six
    # authoring scenarios; this corpus is wider and covers triage /
    # investigation / containment too, which is the sweep the delete-decision
    # was waiting on (tracker #70). A zero here is only readable because both
    # ends of the telemetry chain are pinned by tests -- otherwise "no guard
    # fired" and "the instrument is dead" are the same output.
    fires = Counter()
    where: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        for g in r.get("guards_fired") or []:
            fires[g] += 1
            where[g][r["scenario"]] += 1
    turns = len(results) * runs
    print(f"\n  guard fires over {turns} conversation(s):")
    if not fires:
        print("    (none fired -- see tracker #70 before reading that as "
              "'delete them'; a wider MODEL sweep is the other half)")
    for g, n in fires.most_common():
        w = ", ".join(f"{k}×{v}" for k, v in sorted(where[g].items()))
        print(f"    {g:<24} {n:>3}  {w}")

    print(f"\n  tools exercised ({len(tools)}): {', '.join(sorted(tools))}")
    mcp = [(r["scenario"], d.get("run"), d.get("mcp_calls", 0))
           for r in results for d in r.get("runs") or []]
    total = sum(n for _, _, n in mcp)
    print(f"\n  native-MCP calls: {total} across {len(mcp)} run(s)")
    for name, run, n in mcp:
        if n:
            print(f"    {name} run{run}: {n}")
    down = model_never_answered(results)
    if down:
        # Every row "failed" its world/report graders because no turn reached a
        # model; read as model results, a gateway that cannot serve the model
        # looked like an agent that never writes (seen: --llm openai routed to
        # a gateway without that model, every turn a 400).
        print(f"\n[[SWEEP-PROVIDER-DOWN]] no run's first turn reached the model: {down}")
        print("These are not model results. Fix the provider/model, then re-drive.")
        return 3
    verdict = "PASS" if not dirty else f"{len(dirty)} SCENARIO(S) WITH FAILURES"
    print(f"\n{verdict}")
    return 0 if not dirty else 1


def model_never_answered(results: list[dict]) -> str | None:
    """The first turn's error when EVERY run of every row ended its first turn
    in an error, else None. One bad row is a row; all of them is the provider."""
    details = [((run.get("graders") or {}).get("turns_completed") or {})
               for r in results for run in r.get("runs") or []]
    if not details or not all(not d.get("ok", True) and
                              str(d.get("detail", "")).startswith("turn 1 ended in error")
                              for d in details):
        return None
    return str(details[0]["detail"])[:240]


def regrade(dump: dict) -> tuple[list[dict], set[str]]:
    """Re-grade a `--keep-conversations` dump with NO model and NO network.

    The point of the tier: a grader change is validated in milliseconds against
    transcripts the gateway already produced, instead of a ~15-minute re-drive
    that also re-rolls the model's stochasticity. Re-driving is only required
    when the CONNECTOR or PROMPT changed -- i.e. when the transcripts would
    genuinely differ.

    A scenario in the dump that no longer exists in the corpus is a hard error,
    not a skip: silently dropping it would let a renamed row vanish from the
    tier while everything still reported green.
    """
    by_name = {sc.name: sc for sc in load_corpus()}
    results: list[dict] = []
    tool_union: set[str] = set()
    for row in dump.get("scenarios") or []:
        name = row["scenario"]
        sc = by_name.get(name)
        if sc is None:
            raise CS.SweepDriveError(
                f"dump contains {name!r}, which is not in the corpus -- the "
                f"dump is stale, or the scenario was renamed")
        spec = load_spec(sc.script)
        mounted = CS._mounted_yaml_for(spec)
        per_grader: dict[str, list] = {g: [] for g in sc.graders}
        details: list[dict] = []
        unserved: dict[str, None] = {}
        guards: list[str] = []
        for run in row.get("runs") or []:
            stored = run.get("conversation")
            if stored is None:
                raise CS.SweepDriveError(
                    f"{name} run {run.get('run')}: dump has no conversation -- "
                    f"it was written without --keep-conversations, so there is "
                    f"nothing to re-grade")
            conv = conversation_from_dict(stored)
            # A dump written before transcripts were captured still LOOKS
            # re-gradable: text/tools are there, so every frame-reading grader
            # (yaml_of's carriers, never_acts_on's tool arguments) just finds
            # nothing and reports a confident, WRONG verdict. Caught exactly
            # that way -- a stale dump re-graded 3 clean rows as
            # "no playbook delivered". Refuse it; a replay tier that grades
            # differently from the live one is worse than no replay tier.
            blind = [s.index for s in conv.steps
                     if s.kind == "user" and s.transcript is None]
            if blind:
                raise CS.SweepDriveError(
                    f"{name} run {run.get('run')}: step(s) {blind} carry no "
                    f"transcript. This dump predates transcript capture -- "
                    f"re-grading it would silently weaken every grader that "
                    f"reads frames. Re-drive to produce a fresh dump.")
            tool_union.update(conv.all_tools)
            guards.extend(conv.all_guards)
            for u in conv.unserved:
                unserved.setdefault(unserved_shape(u), None)
            d = CS._grade_one(sc, conv, mounted, run.get("run", 1))
            for gname, v in d["graders"].items():
                per_grader.setdefault(gname, []).append(v["ok"])
            details.append(d)
        results.append({
            "scenario": name,
            "category": sc.category,
            # Same arithmetic as the live row (CS.tally): NOT RUN is shown,
            # never counted as a pass, and a None never reaches sum().
            **CS.tally(per_grader),
            "unserved": sorted(unserved),
            # Same as the live row, so a regrade's guard-fire summary is not
            # silently "none fired".
            "guards_fired": guards,
            "runs": details,
        })
    return results, tool_union


def sample_per_category(scenarios: list, n: int) -> list:
    """At most `n` scenarios per surface, chosen DETERMINISTICALLY (first n by
    name within each category).

    Deterministic, not random, on purpose: a sampled run is used to decide
    whether the harness itself is sound, and a random subset makes two runs
    disagree for a reason that has nothing to do with the code under test --
    the same class of confusion as a harness that silently swaps the provider.
    Sampling touches every surface rather than truncating the list, because the
    thing a cheap run must catch is a plumbing break in a surface nobody
    happened to pick.
    """
    per: dict[str, list] = defaultdict(list)
    for sc in sorted(scenarios, key=lambda s: (s.category, s.name)):
        if len(per[sc.category]) < n:
            per[sc.category].append(sc)
    return [sc for cat in sorted(per) for sc in per[cat]]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", default="frank",
                    choices=["fake", "frank", "openai", "anthropic"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("JOBS", 3)))
    ap.add_argument("--category", default=None, help="only this surface")
    ap.add_argument("--scenario", "-s", default=None,
                    help="only these scenarios (comma-separated)")
    ap.add_argument("--sample", type=int, default=None,
                    help="drive at most N scenarios PER SURFACE (deterministic: "
                         "first N by name). Cheap, wide smoke of the whole "
                         "corpus -- prefer it while the harness itself is "
                         "still being changed")
    ap.add_argument("--out", default=None, help="write full results as JSON")
    ap.add_argument("--keep-conversations", action="store_true",
                    help="include each run's full transcript in --out, so a "
                         "verdict can be AUDITED by hand rather than trusted")
    ap.add_argument("--list", action="store_true",
                    help="list the corpus and exit (no LLM, no network)")
    ap.add_argument("--regrade", default=None, metavar="DUMP.json",
                    help="re-grade a previous --keep-conversations dump with "
                         "NO model and NO network. The right tier for any "
                         "change that touches only grader code")
    a = ap.parse_args(argv)

    if a.regrade:
        dump = json.loads(Path(a.regrade).read_text())
        try:
            results, tools = regrade(dump)
        except CS.SweepDriveError as e:
            print(f"[[REGRADE-ERROR]] {e}", file=sys.stderr)
            return 2
        print(f"re-graded {len(results)} scenario(s) from {a.regrade} "
              f"-- no model, no network")
        return report(results, tools, runs=a.runs)

    scenarios = load_corpus(category=a.category, only=a.scenario)
    if a.sample:
        scenarios = sample_per_category(scenarios, a.sample)

    if a.list:
        by_cat: dict[str, list[str]] = defaultdict(list)
        for sc in scenarios:
            by_cat[sc.category].append(sc.name)
        print(f"{len(scenarios)} sweep scenario(s) in {CORPUS}:")
        for cat in sorted(by_cat):
            print(f"\n  {cat}:")
            for n in sorted(by_cat[cat]):
                sc = next(s for s in scenarios if s.name == n)
                print(f"    {n:<38} {', '.join(sorted(sc.graders))}")
        return 0

    CS.warn_if_fake(a.llm)
    try:
        results, tools = CS.drive_and_grade(
            scenarios, llm=a.llm, model=a.model, runs=a.runs, jobs=a.jobs,
            verbose=False, keep_conversations=a.keep_conversations)
    except CS.SweepDriveError as e:
        # Never score a conversation that did not run.
        print(f"[[DRIVE-ERROR]] {e}", file=sys.stderr)
        return 2

    rc = report(results, tools, runs=a.runs)
    if a.out:
        Path(a.out).write_text(json.dumps(
            {"ok": rc == 0, "llm": a.llm, "model": a.model, "runs": a.runs,
             "scenarios": results, "tools_exercised": sorted(tools)},
            indent=2, default=str))
        print(f"\nwrote {a.out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
