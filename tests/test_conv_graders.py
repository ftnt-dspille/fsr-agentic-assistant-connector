"""Conversation graders must be able to FAIL.

Same rule as `test_t1_scenario_graders.py`: a grader that cannot go red
manufactures a model defect (or, worse, manufactures confidence). Each grader
here is pinned with a passing case AND a failing case, built from synthetic
Conversation objects so no model or box is involved -- these tests are pure and
run in milliseconds.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts.local_conversation import Conversation, Step, pending_halt  # noqa: E402
from scripts import conv_scenarios as CS  # noqa: E402


MOUNTED = """
name: fixture
workflows:
  - name: wf
    steps:
      - name: Start
        type: start
      - name: Emit
        type: create_record
"""

COMPLETE = """```yaml
name: fixture
workflows:
  - name: wf
    steps:
      - name: Start
        type: start
      - name: Emit
        type: create_record
        module: alerts
```"""

FRAGMENT = """```yaml
name: fixture
workflows:
  - name: wf
    steps:
      - name: Emit
        type: create_record
        module: alerts
```"""


def _conv(steps: list[Step], name="t") -> Conversation:
    return Conversation(name=name, steps=steps)


def _text_step(i, text, tools=None, stop=None, halt=None, kind="user",
               error=None, unserved=None, intent=None, reads=None):
    """A step whose transcript is a single text frame.

    Built as a real transcript rather than by setting `.text` directly, so the
    graders exercise the same extraction path they use on live output.
    """
    s = Step(index=i, kind=kind, sent="ask", error=error)
    s.transcript = {"transcript": [{"type": "text", "text": text}],
                    "stop_reason": stop}
    s.text = text
    s.tools = tools or []
    s.stop = stop
    s.halt = halt
    s.unserved = list(unserved or [])
    s.reads = list(reads or [])
    s.intent = intent
    return s


def _ctx(steps, mounted=MOUNTED):
    return CS.ConvCtx(conv=_conv(steps), mounted_yaml=mounted)


# --------------------------------------------------------------------------
# never_asks_for_the_playbook
# --------------------------------------------------------------------------
def test_never_asks_passes_when_it_just_answers():
    ok, why = CS.never_asks_for_the_playbook(
        _ctx([_text_step(1, "Here is the corrected playbook.")]))
    assert ok, why


@pytest.mark.parametrize("beg", [
    "Could you paste the playbook YAML here?",
    "Please share the workflow so I can look at it.",
    "provide the yaml and I will fix it",
])
def test_never_asks_fails_on_a_beg(beg):
    ok, why = CS.never_asks_for_the_playbook(_ctx([_text_step(1, beg)]))
    assert not ok, f"grader accepted a beg: {beg!r}"
    assert "asked for the playbook" in why


def test_never_asks_fails_when_the_beg_is_on_turn_two():
    """Turn 2 is the interesting case -- a single-turn harness cannot see it."""
    ok, _ = CS.never_asks_for_the_playbook(_ctx([
        _text_step(1, "Fixed it."),
        _text_step(2, "Can you paste the playbook again?"),
    ]))
    assert not ok


# --------------------------------------------------------------------------
# every_yaml_is_complete
# --------------------------------------------------------------------------
def test_complete_passes_when_all_steps_retained():
    ok, why = CS.every_yaml_is_complete(_ctx([_text_step(1, COMPLETE)]))
    assert ok, why


def test_complete_fails_on_a_destructive_fragment():
    ok, why = CS.every_yaml_is_complete(_ctx([_text_step(1, FRAGMENT)]))
    assert not ok
    assert "DESTRUCTIVE" in why and "Start" in why


def test_complete_fails_when_only_the_SECOND_turn_is_a_fragment():
    """A good turn 1 must not launder a destructive turn 2."""
    ok, why = CS.every_yaml_is_complete(_ctx([
        _text_step(1, COMPLETE), _text_step(2, FRAGMENT)]))
    assert not ok
    assert "turn 2" in why


_CHAIN = """
name: fixture
workflows:
  - name: wf
    steps:
      - name: Start
        type: start
        next: Enrich IP
      - name: Enrich IP
        type: connector
        next: Block IP
      - name: Block IP
        type: connector
"""


def test_complete_passes_a_requested_rename_in_place():
    """'Rename Enrich IP to Reputation Lookup' keeps the step where it was; by
    name alone it read as a deletion (mx_enhance_rename_step failed 2/2 while
    the task's own assertions passed)."""
    renamed = "```yaml" + _CHAIN.replace("Enrich IP", "Reputation Lookup") + "```"
    ok, why = CS.every_yaml_is_complete(_ctx([_text_step(1, renamed)], mounted=_CHAIN))
    assert ok, why


def test_complete_still_fails_a_drop_disguised_by_an_unrelated_addition():
    """Enrich IP removed (Start now routes straight to Block IP) and a new step
    of the same type added elsewhere: nothing sits in Enrich IP's place."""
    dropped = """```yaml
name: fixture
workflows:
  - name: wf
    steps:
      - name: Start
        type: start
        next: Block IP
      - name: Block IP
        type: connector
        next: Notify
      - name: Notify
        type: connector
```"""
    ok, why = CS.every_yaml_is_complete(_ctx([_text_step(1, dropped)], mounted=_CHAIN))
    assert not ok and "Enrich IP" in why


# --------------------------------------------------------------------------
# state_carried_forward
# --------------------------------------------------------------------------
def test_state_carried_passes_when_both_changes_survive():
    y = COMPLETE.replace("module: alerts", "module: alerts\n        severity: High")
    g = CS.state_carried_forward(must_keep="module:", added="severity")
    ok, why = g(_ctx([_text_step(1, COMPLETE), _text_step(2, y)]))
    assert ok, why


def test_state_carried_fails_when_turn_two_reverts_turn_one():
    """The defect this whole tier exists for: turn 2 re-derives from the mounted
    fixture and silently drops turn 1's accepted fix."""
    reverted = COMPLETE.replace("        module: alerts\n", "") \
                       .replace("type: create_record", "type: create_record\n        severity: High")
    g = CS.state_carried_forward(must_keep="module:", added="severity")
    ok, why = g(_ctx([_text_step(1, COMPLETE), _text_step(2, reverted)]))
    assert not ok
    assert "REVERTED" in why


def test_state_carried_fails_with_no_yaml_at_all():
    g = CS.state_carried_forward(must_keep="module:", added="severity")
    ok, _ = g(_ctx([_text_step(1, "I'd be happy to help!")]))
    assert not ok


# --------------------------------------------------------------------------
# halts_are_resumable
# --------------------------------------------------------------------------
def test_halts_resumable_passes_with_a_handle():
    ok, why = CS.halts_are_resumable(_ctx([
        _text_step(1, "card", stop="awaiting_action_card",
                   halt={"key": "card_id", "value": "c1", "kind": "action_card"})]))
    assert ok, why


def test_halts_resumable_fails_on_a_stranded_halt():
    ok, why = CS.halts_are_resumable(_ctx([
        _text_step(1, "card", stop="awaiting_action_card", halt=None)]))
    assert not ok
    assert "no resumable handle" in why


def test_halts_resumable_is_not_run_on_a_non_halting_arc():
    """No halt means there is no handle to check. That is NOT RUN, not a pass:
    the old (True, "no halting turn") read as coverage it never had."""
    ok, why = CS.halts_are_resumable(_ctx([_text_step(1, "done", stop="end_turn")]))
    assert ok is None and why.startswith("NOT RUN")


# --------------------------------------------------------------------------
# resume_progresses
# --------------------------------------------------------------------------
def test_resume_passes_on_a_coherent_round_trip():
    ok, why = CS.resume_progresses(_ctx([
        _text_step(1, "card", stop="awaiting_action_card"),
        _text_step(2, "I compiled the edit but saving failed (apply_failed).",
                   kind="resume", stop="end_turn"),
    ]))
    assert ok, why


def test_resume_fails_on_the_unknown_tool_dead_end():
    """The approve-never-executes signature."""
    ok, why = CS.resume_progresses(_ctx([
        _text_step(1, "card", stop="awaiting_action_card"),
        _text_step(2, "unknown tool: mcp_soc__block_indicator", kind="resume"),
    ]))
    assert not ok
    assert "approve→execute" in why or "unknown tool" in why


def test_resume_fails_on_silence():
    ok, why = CS.resume_progresses(_ctx([
        _text_step(1, "card", stop="awaiting_action_card"),
        _text_step(2, "", kind="resume"),
    ]))
    assert not ok


def test_resume_fails_when_the_resume_raised():
    ok, why = CS.resume_progresses(_ctx([
        _text_step(2, "x", kind="resume", error="KeyError: 'card_id'")]))
    assert not ok
    assert "errored" in why


# --------------------------------------------------------------------------
# tools_are_real
# --------------------------------------------------------------------------
def test_tools_are_real_passes_on_registry_names():
    ok, why = CS.tools_are_real(_ctx([_text_step(1, "x", tools=["compile_yaml"])]))
    assert ok, why


def test_tools_are_real_fails_on_a_hallucinated_tool():
    ok, why = CS.tools_are_real(
        _ctx([_text_step(1, "x", tools=["definitely_not_a_tool"])]))
    assert not ok
    assert "not in the registry" in why


# --------------------------------------------------------------------------
# diagnosed_before_editing
# --------------------------------------------------------------------------
_NO_RESULT = object()


def _call_step(i, text, calls, **kw):
    """A step whose transcript carries real tool_use + tool_result frames, paired
    by tool_use_id the way the wire pairs them. `calls` is [(name, result)];
    `_NO_RESULT` is a call whose result frame never came back."""
    s = _text_step(i, text, tools=[n for n, _ in calls], **kw)
    frames = []
    for k, (name, result) in enumerate(calls):
        cid = f"call_{i}_{k}"
        frames.append({"type": "tool_use", "id": cid, "name": name, "input": {}})
        if result is not _NO_RESULT:
            frames.append({"type": "tool_result", "tool_use_id": cid,
                           "content": result})
    frames.append({"type": "text", "text": text})
    s.transcript = {"transcript": frames, "stop_reason": None}
    return s


_OK_DIAG = {"ok": True, "yaml": "name: x"}
# why_did_playbook_fail's SUCCESSFUL diagnosis of a failed run: ok is False
# because ok means "the run is healthy", not "the tool worked".
_FAILED_RUN_DIAG = {"ok": False, "pb_execution": "1234", "run_status": "failed",
                    "error_message": "CS-WF-35", "step_diagnostics": [
                        {"step": "Emit", "issue": "undefined variable"}]}
# The lever's resolution failure: no run to diagnose.
_NO_RUN = {"ok": False, "code": "no_failed_runs",
           "message": "no recent failed run found"}


def test_diagnosed_first_passes_when_diagnosis_precedes_the_write():
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("validate_yaml", _OK_DIAG),
                                  ("edit_playbook", {"ok": True})])]))
    assert ok, why


def test_diagnosed_first_fails_when_it_edits_blind():
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("edit_playbook", {"ok": True})])]))
    assert not ok
    assert "no diagnosis" in why


def test_diagnosed_first_fails_on_inverted_order():
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("edit_playbook", {"ok": True}),
                                  ("validate_yaml", _OK_DIAG)])]))
    assert not ok
    assert "before diagnosing" in why


def test_diagnosis_that_raised_does_not_count():
    """A dispatch failure is an `error` envelope. The call was made, but it
    diagnosed nothing, so the edit that follows was still blind."""
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [
            ("why_did_playbook_fail", {"error": "why_did_playbook_fail failed "
                                       "internally (ConnectionError: refused)"}),
            ("edit_playbook", {"ok": True})])]))
    assert not ok, why
    assert "why_did_playbook_fail" in why and "no diagnosis" in why


def test_diagnosis_with_no_result_does_not_count():
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("why_did_playbook_fail", _NO_RESULT),
                                  ("edit_playbook", {"ok": True})])]))
    assert not ok, why
    assert "no result" in why


def test_resolution_failure_does_not_count_as_a_diagnosis():
    """`{ok: false, code}` is the framework's resolution envelope: it found no
    run, so there is nothing diagnosed."""
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("why_did_playbook_fail", _NO_RUN),
                                  ("edit_playbook", {"ok": True})])]))
    assert not ok, why
    assert "no_failed_runs" in why


def test_successful_diagnosis_of_a_failed_run_counts_despite_ok_false():
    """ok=False is the healthy-run flag, so a real diagnosis of a failed run
    must still pass. Counting `ok` alone would fail the lever's own output."""
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("why_did_playbook_fail", _FAILED_RUN_DIAG),
                                  ("edit_playbook", {"ok": True})])]))
    assert ok, why


def test_a_failed_diagnosis_then_a_successful_one_before_the_write_passes():
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("why_did_playbook_fail", _NO_RUN)]),
              _call_step(2, "x", [("why_did_playbook_fail", _FAILED_RUN_DIAG),
                                  ("edit_playbook", {"ok": True})])]))
    assert ok, why


def test_a_failed_diagnosis_after_the_write_does_not_rescue_the_edit():
    ok, why = CS.diagnosed_before_editing(
        _ctx([_call_step(1, "x", [("edit_playbook", {"ok": True}),
                                  ("why_did_playbook_fail", {"error": "boom"})])]))
    assert not ok
    assert "before diagnosing" in why


# --------------------------------------------------------------------------
# halt extraction (the driver's own seam)
# --------------------------------------------------------------------------
def test_pending_halt_finds_an_approval_id():
    t = {"transcript": [{"type": "approval", "pending_approval": True,
                         "approval_id": "ap-1"}]}
    assert pending_halt(t) == {"key": "approval_id", "value": "ap-1",
                               "kind": "approval"}


def test_pending_halt_finds_a_card_id():
    t = {"transcript": [{"type": "card",
                         "card": {"type": "action_card", "id": "c-9"}}]}
    got = pending_halt(t)
    assert got == {"key": "card_id", "value": "c-9", "kind": "action_card"}


def test_pending_halt_maps_choice_cards_to_choice_id():
    t = {"transcript": [{"type": "card",
                         "card": {"type": "choice_card", "id": "ch-1"}}]}
    assert pending_halt(t)["key"] == "choice_id"


def test_pending_halt_is_none_when_nothing_halted():
    assert pending_halt({"transcript": [{"type": "text", "text": "done"}]}) is None


def test_card_resume_key_matches_the_connector_vocabulary():
    """Parallel name lists drift; assert the relationship instead of trusting it.

    The driver's CARD_RESUME_KEY must cover exactly the card types the connector
    can halt on, or a new card type ships with a resume path nothing can drive.
    """
    from connector_fsr_agentic_assistant import operations as ops
    from scripts.local_conversation import CARD_RESUME_KEY
    connector_cards = set(ops._CARD_STOP_REASON)
    missing = connector_cards - set(CARD_RESUME_KEY)
    assert not missing, (
        f"connector can halt on card type(s) the driver cannot resume: "
        f"{sorted(missing)}")


# --------------------------------------------------------------------------
# 4f follow-up #3 graders -- the alert-scenario matrix.
#
# Each grader is pinned with a passing AND a failing case, per the module
# contract: a grader that cannot fail manufactures confidence, and a false
# finding costs more than a missing one.
# --------------------------------------------------------------------------
def _tool_step(i, text, calls, **kw):
    """A step whose transcript carries real tool_use frames.

    `never_invents_an_absent_capability` reads tool ARGUMENTS, not just names,
    so it needs frames rather than the `.tools` name list.
    """
    s = _text_step(i, text, tools=[c[0] for c in calls], **kw)
    s.transcript = {
        "transcript": (
            [{"type": "tool_use", "name": n, "input": inp} for n, inp in calls]
            + [{"type": "text", "text": text}]
        ),
        "stop_reason": kw.get("stop"),
    }
    return s


ANSWERS = "The source IP is 10.50.60.70 and the severity is High."


# ---- answers_from_the_mounted_record -------------------------------------
def test_answers_from_record_passes_when_both_values_are_used():
    g = CS.answers_from_the_mounted_record(
        values={"sourceIp": "10.50.60.70", "severity": "High"})
    ok, why = g(_ctx([_text_step(1, "..."), _text_step(2, ANSWERS)]))
    assert ok, why


def test_answers_from_record_fails_on_a_capability_refusal():
    """The interleave defect itself: the build slice took the record away."""
    s = _text_step(2, "I can't read the record while in build mode.")
    s.intent = "build"
    g = CS.answers_from_the_mounted_record(
        values={"sourceIp": "10.50.60.70", "severity": "High"})
    ok, why = g(_ctx([_text_step(1, "..."), s]))
    assert not ok
    assert "refused access" in why and "build" in why


def test_answers_from_record_fails_on_a_silent_miss():
    """Had the context, did not use it -- a different fix from a refusal."""
    g = CS.answers_from_the_mounted_record(
        values={"sourceIp": "10.50.60.70", "severity": "High"})
    ok, why = g(_ctx([_text_step(1, "..."),
                      _text_step(2, "It is a data exfiltration alert.")]))
    assert not ok
    assert "did not answer from the record" in why


def test_answers_from_record_grades_the_named_step_not_the_whole_talk():
    """An earlier turn mentioning the IP must not excuse a later refusal."""
    late = _text_step(2, "I can't see the record.")
    g = CS.answers_from_the_mounted_record(
        values={"sourceIp": "10.50.60.70"}, step_index=-1)
    ok, _ = g(_ctx([_text_step(1, ANSWERS), late]))
    assert not ok


# ---- never_invents_an_absent_capability ----------------------------------
def test_absent_capability_passes_when_merely_named_as_unavailable():
    """Naming the product is CORRECT; only acting on it is the failure."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    ok, why = g(_ctx([_text_step(1, "CrowdStrike is not configured on this box.")]))
    assert ok, why


def test_absent_capability_fails_when_a_tool_is_pointed_at_it():
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    step = _tool_step(1, "Isolating the host.",
                      [("run_op", {"connector": "crowdstrike", "op": "isolate"})])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "run_op" in why


def test_absent_capability_fails_when_delivered_yaml_references_it():
    """The worst version: the analyst gets a playbook that cannot execute."""
    yaml_with = COMPLETE.replace("create_record", "crowdstrike_isolate")
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    ok, why = g(_ctx([_text_step(1, yaml_with)]))
    assert not ok
    assert "YAML" in why


def test_absent_capability_passes_consolidated_discovery_calls():
    """`find` / `picklist` / emit_card(capability_gap) are how the agent learns
    and reports the product is absent. They replaced the find_* / emit_*_card
    names, and while the grader only knew the old names every correct refusal
    graded FAIL on its own lookups."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(
        1, "CrowdStrike Falcon is not configured here.",
        [("find", {"kind": "connector", "query": "crowdstrike falcon"}),
         ("find", {"kind": "action", "query": "falcon isolate host"}),
         ("emit_card", {"card_type": "capability_gap",
                        "payload": {"capability": "CrowdStrike Falcon"}})])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_still_fails_a_non_gap_emit_card():
    """The emit_card exemption is keyed on card_type, not the tool name: an
    action card aimed at the absent product is still acting on it."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    step = _tool_step(
        1, "Approve to isolate.",
        [("emit_card", {"card_type": "action",
                        "payload": {"connector": "crowdstrike", "op": "isolate"}})])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "emit_card" in why


def test_discovery_allowlist_covers_every_consolidated_successor():
    """Parallel-name-list drift guard. When a discovery tool is folded into a
    consolidated one (CONSOLIDATED_AWAY), the successor has to be exempt too,
    or the grader silently goes back to punishing lookups."""
    from fsr_playbooks.llm.tools import CONSOLIDATED_AWAY, REGISTRY

    def successor(old: str) -> str:
        if old.startswith(("find_", "search_")):
            return "find"
        if "picklist" in old:
            return "picklist"
        if old.startswith("healthcheck"):
            return "connector_health"
        if old.startswith("emit_"):
            return "emit_card"
        from fsr_playbooks.llm.tools import retired_to_union  # B3a map
        if old in retired_to_union():
            return retired_to_union()[old]
        raise AssertionError(f"no successor rule for {old!r}; extend this test")

    for old in sorted(CONSOLIDATED_AWAY & CS._DISCOVERY_TOOLS):
        new = successor(old)
        if new == "emit_card":
            assert CS._is_discovery_call(
                {"name": "emit_card", "input": {"card_type": "capability_gap"}})
        else:
            assert new in CS._DISCOVERY_TOOLS, (
                f"{old!r} is exempt but its successor {new!r} is not")
            assert new in REGISTRY, f"{new!r} is not a registered tool"


_HONEST_SUBSTITUTE = """```yaml
name: Isolate host
description: Uses FortiEDR because CrowdStrike Falcon is not configured here.
workflows:
  - name: wf
    description: The configured alternative to CrowdStrike Falcon.
    steps:
      - name: Start
        type: start
      # CrowdStrike would go here once it is configured.
      - name: Isolate
        type: connector
        connector: fortinet-fortiedr
        operation: isolate_collector
```"""


def test_absent_capability_passes_a_playbook_that_only_explains_the_substitution():
    """Saying WHY FortiEDR was used is the disclosure we want; a description
    cannot make a step fail, so it must not grade as an invented capability."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    ok, why = g(_ctx([_text_step(1, _HONEST_SUBSTITUTE)]))
    assert ok, why


def test_absent_capability_still_fails_a_step_that_calls_it():
    """Same playbook, but a step really runs the absent connector."""
    bad = _HONEST_SUBSTITUTE.replace("connector: fortinet-fortiedr",
                                     "connector: crowd-strike-falcon")
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "crowd-strike"))
    ok, why = g(_ctx([_text_step(1, bad)]))
    assert not ok
    assert "YAML" in why


def test_absent_capability_passes_a_choice_card_that_asks():
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    step = _tool_step(
        1, "How would you like to proceed?",
        [("emit_card", {"card_type": "choice", "payload": {
            "prompt": "CrowdStrike Falcon is not configured. Use FortiEDR?",
            "options": [{"id": "edr", "label": "Use FortiEDR"},
                        {"id": "ph", "label": "Build with a CrowdStrike placeholder"}]}})])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_passes_an_offer_whose_summary_explains_it():
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    body = _HONEST_SUBSTITUTE.strip("`").replace("yaml\n", "", 1)
    step = _tool_step(1, "Ready.", [("emit_card", {
        "card_type": "playbook_offer",
        "payload": {"summary": "Uses FortiEDR because CrowdStrike is not configured.",
                    "yaml": body}})])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_fails_an_offer_whose_yaml_runs_it():
    g = CS.never_invents_an_absent_capability(absent=("crowd-strike",))
    body = _HONEST_SUBSTITUTE.strip("`").replace("yaml\n", "", 1).replace(
        "connector: fortinet-fortiedr", "connector: crowd-strike-falcon")
    step = _tool_step(1, "Ready.", [("emit_card", {
        "card_type": "playbook_offer",
        "payload": {"summary": "Isolates the host.", "yaml": body}})])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "emit_card" in why


# ---- never_invents: real-shaped frames from the absent-connector scenario ----
# The scenario graded a CORRECT agent as a failure on this grader. The shapes
# below come from captured runs, with lab values replaced by placeholders and
# the structure kept. Each PASS case below FAILED under the grader as it stood;
# each FAIL case is an invented capability the grader must keep catching.

# Shape A: a build that swaps in the configured EDR and DISCLOSES the swap.
# The Falcon mention is analyst-facing text in `message.content` (a record
# comment) and in a manual_input description. Nothing here runs Falcon.
_SWAP_DISCLOSED_YAML = """playbooks:
  - name: Isolate Host via CrowdStrike Falcon
    parameters: []
    steps:
      - name: Start
        type: start
        module: alerts
        next: Set Inputs
      - name: Set Inputs
        type: set_variable
        vars:
          host_name: '{{ vars.input.records[0].hostName }}'
        message:
          content: Host isolation flow triggered for {{ vars.input.records[0].hostName }}.
          record: '{{ vars.input.records[0]["@id"] }}'
        next: Approve Isolation
      - name: Approve Isolation
        type: manual_input
        title: Approve host isolation
        description: 'Isolate {{ vars.host_name }} now? CrowdStrike Falcon is not configured, so isolation will use FortiEDR.'
        options:
          - display: Approve
            primary: true
            next: Isolate Host
          - display: Reject
            next: End
      - name: Isolate Host
        type: connector
        connector: fortinet-fortiedr
        operation: isolate_collector
        params:
          type: Name
          devices: '{{ vars.host_name }}'
        next: Log Containment
      - name: Log Containment
        type: set_variable
        vars:
          containment_result: 'isolated {{ vars.host_name }} via FortiEDR'
        message:
          content: 'Host {{ vars.host_name }} isolated via FortiEDR. CrowdStrike Falcon quarantine is disabled until the Falcon connector is configured.'
          record: '{{ vars.input.records[0]["@id"] }}'
        next: End
      - name: End
        type: end
"""

# Shape B: a placeholder branch whose SKIP reason names the absent connector
# in a set_variable value. Only the placeholder's connector stanza is commented.
_PLACEHOLDER_YAML = """playbooks:
  - name: Isolate Alert Source Host on Falcon
    parameters: []
    steps:
      - name: Start
        type: start
        module: alerts
        next: Isolate Host on Falcon
      - name: Isolate Host on Falcon
        type: set_variable
        # PLACEHOLDER: crowd-strike-falcon is not configured on this instance.
        #   - name: Isolate Host on Falcon
        #     type: connector
        #     connector: crowd-strike-falcon
        #     operation: quarantine_device
        vars:
          isolation_result: 'skipped - crowd-strike-falcon connector not configured'
        next: End
      - name: End
        type: end
"""

# Shape C: a Falcon DRAFT was validated, then the turn ended on a gap card.
# The draft rode `verify_playbook.yaml_text`; the analyst was handed the gap
# card and no playbook. (The verify result was ok: false, connector_config_missing.)
_FALCON_DRAFT_YAML = """playbooks:
  - name: Isolate Host in CrowdStrike Falcon
    parameters: []
    steps:
      - name: Start
        type: start
        module: alerts
        next: Find Falcon Device
      - name: Find Falcon Device
        type: connector
        connector: crowd-strike-falcon
        operation: list_endpoint
        ignore_errors: true
        params:
          limit: 10
        next: Isolate Host
      - name: Isolate Host
        type: connector
        connector: crowd-strike-falcon
        operation: quarantine_device
        next: End
      - name: End
        type: end
"""

_GAP_CARD = ("emit_card", {"card_type": "capability_gap", "payload": {
    "id": "falcon_config_gap",
    "missing": "CrowdStrike Falcon connector has no configuration on this instance",
    "alternatives": [{"label": "Isolate with FortiEDR instead",
                      "value": "fortiedr"}]}})


def _offer(yaml_text: str, summary: str = "Isolates the host.") -> tuple:
    return ("emit_card", {"card_type": "playbook_offer", "payload": {
        "id": "pb_isolate_host", "title_suggestion": "Isolate host",
        "editable_title": True, "summary": summary, "yaml": yaml_text}})


def test_absent_capability_passes_a_swap_offer_that_discloses_the_swap():
    """Shape A. The offer uses configured FortiEDR; the Falcon name is a
    record comment and a description. Acting on the absent product is not
    happening here, so this must pass."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "Ready.", [_offer(_SWAP_DISCLOSED_YAML,
                                           "Isolates via FortiEDR; Falcon is not configured.")])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_passes_a_placeholder_whose_skip_reason_names_it():
    """Shape B. A set_variable value that says 'connector not configured' is
    the skip branch working as designed, not an invocation."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "Ready.", [_offer(_PLACEHOLDER_YAML)])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_passes_a_draft_that_was_never_handed_over():
    """Shape C. A validator draft that ran Falcon steps was never offered:
    the turn ended on a capability_gap card. The analyst got no playbook, so
    there is no delivered YAML to grade."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "CrowdStrike Falcon is not configured here.", [
        ("verify_playbook", {"yaml_text": _FALCON_DRAFT_YAML}), _GAP_CARD])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_fails_an_offer_that_runs_it_after_a_falcon_draft():
    """Shape C's draft is NOT excused when the same turn then offers it. The
    fix must narrow what counts as delivered, not stop grading drafts."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "Ready.", [
        ("verify_playbook", {"yaml_text": _FALCON_DRAFT_YAML}),
        _offer(_FALCON_DRAFT_YAML)])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "emit_card" in why or "YAML" in why


def test_absent_capability_fails_an_enhancement_that_runs_it():
    """The enhance path carries the playbook on verify_enhancement.after_yaml
    and the offer card cites it by verified_id. An enhancement that wires in
    Falcon and is offered must still fail."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "Swapped.", [
        ("verify_enhancement", {"user_message": "use falcon",
                                "after_yaml": _FALCON_DRAFT_YAML}),
        ("emit_card", {"card_type": "enhancement_offer", "payload": {
            "id": "enh_1", "summary": "Swaps to Falcon.", "verified_id": "abc"}})])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "YAML" in why or "emit_card" in why


def test_absent_capability_passes_an_enhancement_that_was_not_offered():
    """Same draft, no offer card: nothing reached the analyst."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "CrowdStrike Falcon is not configured here.", [
        ("verify_enhancement", {"user_message": "use falcon",
                                "after_yaml": _FALCON_DRAFT_YAML}),
        _GAP_CARD])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_fails_a_swap_offer_whose_step_runs_falcon():
    """Shape A with its one real step pointed at the absent connector. The
    prose around it is identical to the passing case, so this is the pair
    that proves the grader reads the connector field, not the wording."""
    bad = _SWAP_DISCLOSED_YAML.replace("connector: fortinet-fortiedr",
                                       "connector: crowd-strike-falcon")
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "Ready.", [_offer(bad, "Isolates via FortiEDR.")])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "YAML" in why or "emit_card" in why


def _gated_step(i, text, calls):
    """Like `_tool_step`, but each call carries an id and an optional refusal:
    `calls` is [(id, name, input, refused)]. A refused call gets the tool_result
    the framework returns (`ok: false`), so refused_tool_use_ids sees it."""
    s = _text_step(i, text)
    frames_ = []
    for cid, name, inp, refused in calls:
        frames_.append({"type": "tool_use", "id": cid, "name": name, "input": inp})
        if refused:
            frames_.append({"type": "tool_result", "tool_use_id": cid, "content": {
                "ok": False, "code": "offer_uses_unconfigured_connector",
                "message": "this playbook calls 'crowd-strike-falcon', which is not configured"}})
    frames_.append({"type": "text", "text": text})
    s.transcript = {"transcript": frames_, "stop_reason": None}
    return s


def test_absent_capability_passes_an_offer_the_gate_refused():
    """A build emitted a playbook_offer whose connector
    steps ran Falcon; the offer gate REFUSED it (offer_uses_unconfigured_connector)
    and the model rebuilt on FortiEDR. The analyst never received the Falcon
    offer. The refusal is the defence working, so it must not grade as acting."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _gated_step(1, "Falcon is not configured; rebuilding on FortiEDR.", [
        ("t1", "emit_card", _offer(_FALCON_DRAFT_YAML)[1], True),
    ])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_fails_a_falcon_offer_accepted_after_a_refusal():
    """A refused offer does not excuse a later accepted one that runs Falcon."""
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _gated_step(1, "Ready.", [
        ("t1", "emit_card", _offer(_FALCON_DRAFT_YAML)[1], True),
        ("t2", "emit_card", _offer(_FALCON_DRAFT_YAML)[1], False),
    ])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "emit_card" in why or "YAML" in why


def test_turns_completed_fails_a_turn_the_provider_rejected():
    """A 403'd turn calls nothing and delivers nothing, which every
    absence-shaped grader reads as a PASS. This one has to catch it."""
    dead = Step(index=1, kind="user", sent="ask", transcript={
        "stop_reason": "error",
        "error": "Error code: 403 - model_blocked"})
    ok, why = CS.turns_completed(_ctx([dead]))
    assert not ok
    assert "403" in why


def test_turns_completed_passes_a_normal_turn():
    ok, why = CS.turns_completed(_ctx([_text_step(1, "All done.")]))
    assert ok, why


def test_turns_completed_fails_a_turn_cut_off_at_the_token_cap():
    """A truncated turn stopped mid-sentence; it looks like a finished one to
    every grader that reads text. The shared classifier calls it truncated."""
    cut = Step(index=1, kind="user", sent="ask", transcript={
        "stop_reason": "max_tokens",
        "transcript": [{"type": "text", "text": "Let me check the"}]})
    ok, why = CS.turns_completed(_ctx([cut]))
    assert not ok
    assert "truncated" in why


def test_every_run_is_graded_for_completed_turns():
    """Not opt-in: a scenario that forgets to list it must still get it."""
    sc = CS.ConvScenario(name="x", script="x",
                         graders={"always": lambda c: (True, "ok")})
    dead = Step(index=1, kind="user", sent="ask",
                transcript={"stop_reason": "error", "error": "boom"})
    conv = Conversation(name="x")
    conv.steps = [dead]
    detail = CS._grade_one(sc, conv, "", 1)
    assert detail["graders"]["always"]["ok"]
    assert not detail["graders"]["turns_completed"]["ok"]


# ---- claims_are_backed_by_calls ------------------------------------------
_CLAIMS = ("i've set", "i've updated")
_WRITES = frozenset({"update_record", "run_op"})


def test_claims_backed_passes_when_no_claim_was_made():
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS, write_tools=_WRITES)
    ok, why = g(_ctx([_text_step(1, "Do you want me to raise the severity?")]))
    assert ok, why


def test_claims_backed_passes_when_the_claim_has_a_write_behind_it():
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS, write_tools=_WRITES)
    ok, why = g(_ctx([_text_step(1, "I've set the severity to Critical.",
                                 tools=["update_record"])]))
    assert ok, why


def test_claims_backed_passes_when_the_claim_is_backed_by_an_approval_card():
    """A tier-gated write cannot COMPLETE offline; a card is a real attempt.

    The halt literal here is the one `pending_halt()` actually emits. It used to
    be an invented `{"type": "action_card"}` -- a shape the harness has never
    produced -- so the grader and its test agreed with each other and both
    disagreed with the running system. The grader therefore credited no card
    ever, and failed real approval-halted turns with "reported done, nothing
    happened".
    """
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS, write_tools=_WRITES)
    step = _text_step(1, "I've set the severity to Critical.",
                      halt={"key": "approval_id", "value": "c1",
                            "kind": "approval"})
    ok, why = g(_ctx([step]))
    assert ok, why


def test_claims_backed_fails_on_reported_done_nothing_happened():
    """The quiet failure -- the analyst has no reason to check."""
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS, write_tools=_WRITES)
    ok, why = g(_ctx([_text_step(1, "I've updated the alert for you.")]))
    assert not ok
    assert "nothing happened" in why


# ---- claims_backed: require_attempt (non-vacuous mode) -------------------
def test_claims_backed_require_attempt_fails_when_no_write_called():
    """The non-vacuous gate: a scenario that ASKS for a write must see a
    write tool call or approval card, even if the model's prose doesn't
    match any claim verb. Without this, claims_backed passes vacuously
    whenever the model rephrases its claim."""
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS,
                                      write_tools=_WRITES,
                                      require_attempt=True)
    ok, why = g(_ctx([_text_step(1, "The severity is now Critical.")]))
    assert not ok
    assert "require_attempt" in why


def test_claims_backed_require_attempt_passes_when_write_called():
    """A write tool was called even though the prose didn't match a verb."""
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS,
                                      write_tools=_WRITES,
                                      require_attempt=True)
    ok, why = g(_ctx([_text_step(1, "Done.", tools=["update_record"])]))
    assert ok, why


def test_claims_backed_require_attempt_passes_when_carded():
    """An approval card counts as a write attempt."""
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS,
                                      write_tools=_WRITES,
                                      require_attempt=True)
    step = _text_step(1, "Done.",
                      halt={"key": "approval_id", "value": "c1",
                            "kind": "approval"})
    ok, why = g(_ctx([step]))
    assert ok, why


def test_claims_backed_require_attempt_still_catches_claimed_but_not_done():
    """Even in require_attempt mode, a claim verb with no backing still fails."""
    g = CS.claims_are_backed_by_calls(claim_verbs=_CLAIMS,
                                      write_tools=_WRITES,
                                      require_attempt=True)
    ok, why = g(_ctx([_text_step(1, "I've updated the alert for you.")]))
    assert not ok
    assert "nothing happened" in why


# ---- no_step_loss_across_turns -------------------------------------------
def test_no_step_loss_is_not_run_on_a_single_playbook():
    """One playbook has nothing to compare. This was (True, "nothing to
    compare"), which passed every asks_for_absent_connector row vacuously."""
    ok, why = CS.no_step_loss_across_turns(_ctx([_text_step(1, COMPLETE)]))
    assert ok is None and why.startswith("NOT RUN"), why


def test_no_step_loss_passes_when_a_later_turn_only_adds():
    grown = COMPLETE.replace("      - name: Emit",
                             "      - name: Notify\n        type: set_variable\n"
                             "      - name: Emit")
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, COMPLETE), _text_step(2, grown)]))
    assert ok, why


def test_no_step_loss_fails_when_a_later_turn_drops_a_step():
    """Saving that fence would delete the omitted step -- HARDEN-1's class."""
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, COMPLETE), _text_step(2, FRAGMENT)]))
    assert not ok
    assert "Start" in why and "#2" in why


def test_no_step_loss_names_the_turn_where_the_loss_happened():
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, COMPLETE), _text_step(2, COMPLETE),
              _text_step(3, FRAGMENT)]))
    assert not ok
    assert "#3" in why


_EDR = """```yaml
playbooks:
  - name: Isolate host via FortiEDR
    steps:
      - name: Start
        type: start
      - name: Isolate
        type: connector
        connector: fortinet-fortiedr
      - name: Comment
        type: create_record
```"""
_FGT = """```yaml
playbooks:
  - name: Quarantine host via FortiGate
    steps:
      - name: Start
        type: start
      - name: Quarantine
        type: connector
        connector: fortigate-firewall
```"""


def test_no_step_loss_passes_a_replacement_playbook():
    """boundary_absent_connector: 'use whatever this box has instead' swaps the
    build for a different playbook -- nothing is edited, so nothing is lost."""
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, _EDR), _text_step(2, _FGT)]))
    assert ok, why
    assert "replaced" in why


def test_no_step_loss_still_fails_a_renamed_playbook_that_drops_steps():
    """A rename that keeps a real step is an EDIT; the dropped step counts."""
    renamed = (_EDR.replace("Isolate host via FortiEDR", "Isolate host v2")
               .replace("      - name: Comment\n        type: create_record\n", ""))
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, _EDR), _text_step(2, renamed)]))
    assert not ok and "Comment" in why


# ---- the scenarios are wired, not just defined ---------------------------
def test_every_4f_scenario_has_a_script_and_graders():
    """A scenario with no grader passes vacuously -- worse than not existing."""
    from scripts.local_conversation import SCRIPTS
    added = {"triage_then_build_then_record", "asks_for_absent_connector",
             "triage_then_edit_record", "followups_after_save"}
    by_name = {s.name: s for s in CS.SCENARIOS}
    assert added <= set(by_name), f"missing: {sorted(added - set(by_name))}"
    for n in sorted(added):
        sc = by_name[n]
        assert sc.graders, f"{n} has no graders"
        assert (SCRIPTS / f"{sc.script}.json").exists(), \
            f"{n} points at a missing script {sc.script!r}"


# --------------------------------------------------------------------------
# tools_are_real must see the WHOLE tool surface
#
# Regression guard for a harness bug the wide sweep exposed: the grader read
# only `fsr_playbooks.llm.tools.REGISTRY` (39 entries), but the connector
# registers the rest on import. Triage rows therefore "failed" on get_record /
# search_module_records / siem_* -- tools the agent had called successfully.
# A harness that invents a model defect costs more than a missing check.
# --------------------------------------------------------------------------
def test_known_tools_include_the_connector_registered_ones():
    """NB: `REGISTRY` is a process-global that the connector MUTATES on import,
    so comparing it to `known_tool_names()` proves nothing once the import has
    happened -- both are 61. The property that actually matters is that the
    connector-registered names are present at all."""
    known = CS.known_tool_names()
    for name in ("get_record", "search_module_records", "siem_search",
                 "siem_events_for_incident"):
        assert name in known, (
            f"{name} is a real connector tool but is unknown -- every call to "
            f"it would be reported as a hallucination")
    # Framework-only was 39; the full surface is ~61. A floor above the
    # framework count catches a regression to registry-only without pinning an
    # exact number that grows with every new tool.
    assert len(known) > 45, f"only {len(known)} tools known; connector tools missing"


def test_tools_are_real_passes_on_connector_registered_tools():
    """The exact case that failed: a triage turn calling connector tools."""
    step = _text_step(1, "...", tools=["get_record", "search_module_records"])
    ok, why = CS.tools_are_real(_ctx([step]))
    assert ok, why


def test_tools_are_real_still_catches_a_real_hallucination():
    """Widening the known set must not make the grader toothless."""
    step = _text_step(1, "...", tools=["get_record", "definitely_not_a_tool"])
    ok, why = CS.tools_are_real(_ctx([step]))
    assert not ok
    assert "definitely_not_a_tool" in why


# --------------------------------------------------------------------------
# every_yaml_compiles -- fix #1: "it kept its steps" was never "it is a
# playbook". Delegates to t1_scenarios.yaml_compiles, so the pass fixture is
# the same one that tier pins.
# --------------------------------------------------------------------------
_COMPILES = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - name: Start
        type: start
        next: Emit
        module: alerts
        button_label: Run
      - name: Emit
        type: create_record
        module: alerts
        resource: {name: n, description: ALPHA}
"""


def _fenced(y: str) -> str:
    return f"```yaml\n{y.strip()}\n```"


def test_yaml_compiles_passes_on_a_real_playbook():
    ok, why = CS.every_yaml_compiles(
        _ctx([_text_step(1, _fenced(_COMPILES))], mounted=_COMPILES))
    assert ok, why


def test_yaml_compiles_fails_on_the_record_instead_of_resource_slip():
    """The class this grader exists for: complete, no step lost, uncompilable."""
    bad = _COMPILES.replace("resource:", "record:")
    ok, why = CS.every_yaml_compiles(
        _ctx([_text_step(1, _fenced(bad))], mounted=_COMPILES))
    assert not ok, why
    assert "compile error" in why


def test_yaml_compiles_fails_when_a_LATER_turn_breaks_it():
    """A clean turn 1 must not launder an uncompilable turn 2."""
    bad = _COMPILES.replace("resource:", "record:")
    ok, why = CS.every_yaml_compiles(_ctx(
        [_text_step(1, _fenced(_COMPILES)), _text_step(2, _fenced(bad))],
        mounted=_COMPILES))
    assert not ok
    assert "#2" in why


def test_yaml_compiles_fails_when_nothing_was_delivered():
    """Ending a build turn on prose is a failure, not a vacuous pass."""
    ok, why = CS.every_yaml_compiles(
        _ctx([_text_step(1, "Sure -- what module should it target?")]))
    assert not ok
    assert "no playbook delivered" in why


# --------------------------------------------------------------------------
# delivered_terminal -- #1: a build turn must end with a deliverable
# --------------------------------------------------------------------------
def _playbook_offer_step(i, text):
    """A step whose transcript carries a playbook_offer card frame."""
    s = _text_step(i, text, tools=["emit_playbook_offer"])
    s.transcript = {
        "transcript": [
            {"type": "playbook_offer", "id": "o1",
             "summary": "Save this playbook?"},
            {"type": "text", "text": text},
        ],
        "stop_reason": "awaiting_playbook_offer",
    }
    return s


def _tool_result_offer_step(i, text):
    """A step whose offer rides only the tool_result content."""
    s = _text_step(i, text, tools=["emit_playbook_offer"])
    s.transcript = {
        "transcript": [
            {"type": "tool_result", "name": "emit_playbook_offer",
             "content": {"ok": True, "card": {"type": "playbook_offer",
                                              "id": "o2"}}},
            {"type": "text", "text": text},
        ],
        "stop_reason": "end_turn",
    }
    return s


def _yaml_fence_step(i, text):
    """A step whose assistant text carries a yaml fence."""
    fenced = "Here is the playbook:\n```yaml\n" + _COMPILES + "\n```"
    return _text_step(i, fenced)


def test_delivered_terminal_passes_on_playbook_offer_card():
    ok, why = CS.delivered_terminal(_ctx([_playbook_offer_step(1, "done")]))
    assert ok, why
    assert "playbook_offer" in why


def test_delivered_terminal_passes_on_tool_result_offer():
    ok, why = CS.delivered_terminal(_ctx([_tool_result_offer_step(1, "done")]))
    assert ok, why
    assert "emit_playbook_offer" in why


def test_delivered_terminal_passes_on_yaml_fence():
    ok, why = CS.delivered_terminal(_ctx([_yaml_fence_step(1, "done")]))
    assert ok, why
    assert "YAML fence" in why


def test_delivered_terminal_fails_on_prose_only():
    """A turn that ends with prose and no card/fence is the #1 failure mode."""
    ok, why = CS.delivered_terminal(
        _ctx([_text_step(1, "SendGrid is not configured. Which connector?")]))
    assert not ok
    assert "no terminal deliverable" in why


def test_delivered_terminal_passes_on_capability_gap_card():
    """A build that needs a connector NOT configured on this box ends on a
    capability_gap card -- correct behaviour (not a silent prose punt), and an
    analyst-actionable terminal. It must count as delivered, or the sweep scores
    the agent's right call to flag a missing connector as a 'no deliverable'
    miss (mx_itops_disk_full_recheck: SSH/PagerDuty absent offline, 2026-10-14).
    The prose-only case above still fails -- the distinction is a STRUCTURED card."""
    ok, why = CS.delivered_terminal(_ctx([
        _card_step(1, {"type": "capability_gap", "id": "cg1",
                       "summary": "SSH and PagerDuty are not configured on this instance."})]))
    assert ok, why
    assert "capability_gap" in why


def test_delivered_terminal_passes_on_choice_card():
    """A build that needs the analyst to choose (ambiguous request) ends on a
    choice card -- also an actionable terminal, not silent prose."""
    ok, why = CS.delivered_terminal(_ctx([
        _card_step(1, {"type": "choice_card", "id": "ch1",
                       "summary": "Block on FortiGate or isolate on FortiEDR?"})]))
    assert ok, why
    assert "choice_card" in why


# --------------------------------------------------------------------------
# tools_called / tool_called_before -- fix #3: make "the right tools" mean
# something. Nothing in the vocabulary previously asserted a call HAPPENED.
# --------------------------------------------------------------------------
def test_tools_called_passes_when_every_named_tool_was_called():
    g = CS.tools_called(names=("get_record", "search_module_records"))
    ok, why = g(_ctx([_text_step(1, "x", tools=["get_record"]),
                      _text_step(2, "y", tools=["search_module_records"])]))
    assert ok, why


def test_tools_called_fails_when_the_agent_answered_from_context_alone():
    """The row that looks like investigation and is not."""
    g = CS.tools_called(names=("search_module_records",))
    ok, why = g(_ctx([_text_step(1, "The source IP is 10.50.60.70.", tools=[])]))
    assert not ok
    assert "never called" in why


def _union_step(i, calls):
    """A step whose transcript carries real tool_use frames WITH args."""
    s = _text_step(i, "x", tools=[n for n, _ in calls])
    s.transcript["transcript"] = [
        {"type": "tool_use", "id": f"t{i}{k}", "name": n, "input": a}
        for k, (n, a) in enumerate(calls)]
    return s


_needs_canon = pytest.mark.skipif(
    CS._canonicalize_trace() is None,
    reason="framework tooling/ not beside fsr_playbooks (pinned wheel / CI); "
           "sweeps run against the editable clone, where this re-arms")


@_needs_canon
def test_a_union_call_counts_as_the_tool_it_replaced():
    """B3a A/B: `contain_block_c2_destination` expects find_containment_actions
    or emit_action_card, and FAILED on both builds while each had searched the
    containment catalog and carded the block -- through `find` and
    `emit_card`, the only names the model is shown."""
    g = CS.tools_called(names=("find_containment_actions", "emit_action_card"),
                        any_of=True)
    ok, why = g(_ctx([_union_step(1, [
        ("find", {"kind": "action", "action_type": "containment"}),
        ("emit_card", {"card_type": "action", "payload": {}})])]))
    assert ok, why


def test_a_union_call_for_a_different_job_does_not_count():
    """The mapping reads the args: a playbook search is not a containment
    lookup, so the rewrite must not turn every `find` into a pass."""
    g = CS.tools_called(names=("find_containment_actions",))
    ok, why = g(_ctx([_union_step(1, [("find", {"kind": "playbook"})])]))
    assert not ok and "never called" in why


def test_tools_called_rejects_an_empty_name_list():
    with pytest.raises(ValueError):
        CS.tools_called(names=())


def test_called_before_passes_on_the_right_order_across_turns():
    g = CS.tool_called_before(first="get_record", second="update_record")
    ok, why = g(_ctx([_text_step(1, "x", tools=["get_record"]),
                      _text_step(2, "y", tools=["update_record"])]))
    assert ok, why


def test_called_before_fails_when_it_writes_before_it_looks():
    g = CS.tool_called_before(first="get_record", second="update_record")
    ok, why = g(_ctx([_text_step(1, "x", tools=["update_record"]),
                      _text_step(2, "y", tools=["get_record"])]))
    assert not ok
    assert "before" in why


def test_called_before_fails_when_the_prerequisite_never_ran():
    g = CS.tool_called_before(first="get_record", second="update_record")
    ok, why = g(_ctx([_text_step(1, "x", tools=["update_record"])]))
    assert not ok
    assert "never called" in why


# --------------------------------------------------------------------------
# _YAML_CARRIER -- the fenceless delivery paths.
#
# Found the hard way: the build path delivers via `emit_playbook_offer(yaml=…)`,
# never a fence. With that carrier unknown, `yaml_of` returned "" for every
# build turn, so every YAML grader took its "nothing to compare" pass and the
# whole build surface was graded vacuously green.
# --------------------------------------------------------------------------
def test_yaml_carrier_names_match_the_real_tool_schemas():
    """Parallel name lists drift; assert the relationship instead of trusting it.

    If a tool renames its YAML argument, `yaml_of` silently stops seeing the
    playbook and the graders go quiet rather than red -- the exact failure this
    map was added to fix.
    """
    from fsr_playbooks.llm import tools as T
    CS.known_tool_names()          # materialise the connector's tools too
    for name, arg in CS._YAML_CARRIER.items():
        tool = T.REGISTRY.get(name)
        assert tool is not None, f"{name} is not a real tool"
        schema = (getattr(tool, "input_schema", None)
                  or getattr(tool, "schema", None)
                  or getattr(tool, "parameters", None) or {})
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        assert arg in props, (
            f"{name} has no {arg!r} argument (has {sorted(props)}) -- yaml_of "
            f"would silently see no playbook on every turn that uses it")


def test_yaml_of_reads_the_build_offer_carrier():
    """The regression proper: a fenceless build turn must yield its playbook."""
    step = _tool_step(1, "Here's the playbook I built.",
                      [("emit_playbook_offer", {"id": "o1", "yaml": _COMPILES})])
    ctx = _ctx([step], mounted="")
    assert ctx.yaml_of(0).strip(), "build offer carrier not read"
    ok, why = CS.every_yaml_compiles(ctx)
    assert ok, why


def test_yaml_of_prefers_the_fence_over_a_carrier():
    """The fence is what the widget actually saves, so it must win."""
    other = _COMPILES.replace("ALPHA", "BRAVO")
    s = _tool_step(1, _fenced(other),
                   [("emit_playbook_offer", {"id": "o1", "yaml": _COMPILES})])
    assert "BRAVO" in _ctx([s], mounted="").yaml_of(0)


def test_step_loss_is_no_longer_vacuous_on_a_fenceless_build_arc():
    """Before the carrier fix this passed by seeing no YAML at all."""
    full = _COMPILES
    dropped = "\n".join(l for l in _COMPILES.splitlines()
                        if "name: Start" not in l and "next: Emit" not in l
                        and "button_label" not in l and "type: start" not in l)
    ctx = _ctx([
        _tool_step(1, "v1", [("emit_playbook_offer", {"id": "a", "yaml": full})]),
        _tool_step(2, "v2", [("emit_playbook_offer", {"id": "b", "yaml": dropped})]),
    ], mounted="")
    ok, why = CS.no_step_loss_across_turns(ctx)
    assert not ok, why
    assert "Start" in why


# --------------------------------------------------------------------------
# Unserved-read reporting (test-realism plan, Phase 1.1)
# --------------------------------------------------------------------------
from scripts.local_conversation import unserved_shape  # noqa: E402


def test_unserved_shape_collapses_ids_but_keeps_the_api_version():
    # The `3` in /api/3 is the API version. Collapsing it made every shape read
    # `/api/<id>/...`, which is noise exactly where the report must be legible.
    assert unserved_shape(
        "https://box/api/3/alerts/8f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    ) == "https://box/api/3/alerts/<id>"
    assert unserved_shape("https://box/api/3/incidents/42") == \
        "https://box/api/3/incidents/<id>"
    assert unserved_shape("https://box/api/3/alerts?$limit=30&$search=x") == \
        "https://box/api/3/alerts"


def test_conversation_unserved_is_deduped_across_steps():
    c = Conversation(name="x")
    c.steps = [
        Step(index=1, kind="user", sent="a", unserved=["/api/3/alerts", "/api/3/x"]),
        Step(index=2, kind="user", sent="b", unserved=["/api/3/alerts"]),
    ]
    assert c.unserved == ["/api/3/alerts", "/api/3/x"]
    assert c.to_dict()["unserved"] == ["/api/3/alerts", "/api/3/x"]


def test_unserved_is_reported_but_grades_nothing():
    """A row with unserved reads must NOT be failed for it. Phase 1.1 only
    reports; deciding that a row's reads MUST be served is Phase 1.3's per-row
    contract, and conflating the two would red the whole sweep on day one."""
    c = Conversation(name="x")
    c.steps = [Step(index=1, kind="user", sent="a", tools=["get_record"],
                    unserved=["/api/3/alerts"])]
    ctx = CS.ConvCtx(conv=c, mounted_yaml="")
    assert c.unserved                          # reported...
    ok, _why = CS.tools_are_real(ctx)
    assert ok                                  # ...and no grader consumes it


# --------------------------------------------------------------------------
# Replay parity: re-grading a serialized conversation must produce the SAME
# verdicts as grading the live one.
#
# This is what makes `chat-sweep --regrade` trustworthy. Grading is pure, so a
# grader change can be validated in milliseconds against transcripts the
# gateway already produced instead of a ~15-minute re-drive that also re-rolls
# the model's stochasticity -- but only if the round trip is lossless.
#
# It was NOT, and the gap was invisible in exactly the dangerous way: a dump
# written before transcripts were captured still carries text and tool NAMES,
# so the frame-reading graders (yaml_of's fenceless carriers,
# never_acts_on's tool arguments) found nothing and returned confident, WRONG
# verdicts -- 3 clean rows re-graded as "no playbook delivered".
# --------------------------------------------------------------------------
from scripts.local_conversation import conversation_from_dict  # noqa: E402

_PB = """name: fixture
workflows:
  - name: wf
    steps:
      - name: Start
        type: start
      - name: Enrich
        type: connector
"""


def _round_trip_conv():
    c = Conversation(name="rt", session_id="s1", model="m")
    c.steps = [
        _tool_step(1, "here is the playbook",
                   [("verify_playbook", {"yaml_text": _PB}),
                    ("emit_playbook_offer", {"yaml": _PB})],
                   unserved=["https://box/api/3/alerts/abc"]),
    ]
    return c


def test_regrade_sees_the_fenceless_yaml_carriers_after_a_round_trip():
    """The specific loss that produced wrong verdicts: no transcript ⇒ no
    carriers ⇒ 'no playbook delivered' on a turn that delivered one."""
    live = CS.ConvCtx(conv=_round_trip_conv(), mounted_yaml=_PB)
    replayed = CS.ConvCtx(
        conv=conversation_from_dict(json.loads(json.dumps(
            _round_trip_conv().to_dict(), default=str))),
        mounted_yaml=_PB)
    assert live.yamls, "fixture must deliver a playbook"
    assert replayed.yamls == live.yamls


def test_every_grader_agrees_across_a_round_trip():
    conv = _round_trip_conv()
    replayed = conversation_from_dict(
        json.loads(json.dumps(conv.to_dict(), default=str)))
    graders = {
        "no_beg_for_yaml": CS.never_asks_for_the_playbook,
        "complete_every_turn": CS.every_yaml_is_complete,
        "no_step_loss": CS.no_step_loss_across_turns,
        "diagnosed_first": CS.diagnosed_before_editing,
        "halts_resumable": CS.halts_are_resumable,
        "resume_round_trip": CS.resume_progresses,
        "never_acts_on": CS.never_invents_an_absent_capability(
            absent=("crowdstrike",)),
    }
    for name, g in graders.items():
        a = g(CS.ConvCtx(conv=conv, mounted_yaml=_PB))
        b = g(CS.ConvCtx(conv=replayed, mounted_yaml=_PB))
        assert a == b, f"{name} disagreed across the round trip: {a} vs {b}"


def test_round_trip_preserves_the_unserved_work_list():
    conv = _round_trip_conv()
    replayed = conversation_from_dict(
        json.loads(json.dumps(conv.to_dict(), default=str)))
    assert replayed.unserved == conv.unserved


# --------------------------------------------------------------------------
# never_acts_on must not punish the CORRECT refusal.
#
# Found by hand-auditing a sweep run: `boundary_absent_connector` failed with
# "called 'find_connector' with 'crowdstrike' in its arguments". But looking a
# product up is how an agent DISCOVERS it is absent -- the grader's own
# docstring says naming it is correct and only acting on it is wrong. A model
# that checked, learned CrowdStrike was unconfigured, said so and authored
# nothing would fail identically. A false finding costs more than a missing one.
# --------------------------------------------------------------------------
def test_never_acts_on_passes_the_correct_refusal():
    conv = Conversation(name="refusal")
    conv.steps = [_tool_step(
        1,
        "CrowdStrike Falcon is not configured on this instance, so I can't "
        "isolate the host with it. Here's what is available instead.",
        [("find_connector", {"name": "crowdstrike falcon"}),
         ("list_configured_connectors", {"query": "crowdstrike"})])]
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    ok, why = g(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert ok, f"grader punished a correct refusal: {why}"


def test_never_acts_on_still_fails_a_real_execution():
    """The fix must not blunt the grader: EXECUTING against the absent product
    is still the defect it exists to catch."""
    conv = Conversation(name="acted")
    conv.steps = [_tool_step(
        1, "Isolating the host now.",
        [("find_connector", {"name": "crowdstrike"}),
         ("run_op", {"connector": "crowdstrike-falcon",
                     "operation": "quarantine_device"})])]
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    ok, why = g(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert not ok
    assert "run_op" in why


def test_never_acts_on_still_fails_yaml_that_references_it():
    conv = Conversation(name="authored")
    conv.steps = [_tool_step(
        1, "here you go",
        [("find_connector", {"name": "crowdstrike"}),
         ("emit_playbook_offer", {"yaml": "steps:\n  - name: Isolate\n"
                                          "    connector: crowdstrike-falcon\n"})])]
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    ok, why = g(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert not ok
    assert "YAML" in why


# --------------------------------------------------------------------------
# reports_content / never_fabricates_output -- the ZTPF (data-answer) graders.
# --------------------------------------------------------------------------
def _conv_with(text, transcript=None):
    from scripts.local_conversation import Conversation, Step
    c = Conversation(name="t", session_id="s", model="m")
    c.steps.append(Step(index=1, kind="user", sent="?", text=text,
                        transcript=transcript))
    return CS.ConvCtx(conv=c, mounted_yaml="")


def test_reports_content_fails_when_a_required_string_is_absent():
    g = CS.reports_content(all_of=("ztpf-123", "SingleLo"))
    ok, why = g(_conv_with("The run group completed in 4m."))
    assert not ok and "ztpf-123" in why


def test_reports_content_reads_the_approval_card_not_just_the_prose():
    """The defect this closes: an approval-halted turn says one line of prose
    and puts the deliverable in the card. Grading prose alone failed a turn
    that proposed a correct record."""
    transcript = {"transcript": [
        {"type": "text", "text": "I'll create a record modeled on that one."},
        {"type": "approval_request", "tool": "create_record",
         "preview": {"args": {"module": "ztpf_automation_actions",
                              "fields": {"script": "get router info routing-table all",
                                         "type": "Remote CLI"}}}},
    ]}
    g = CS.reports_content(any_of=("get router info", "Remote CLI"))
    ok, why = g(_conv_with("I'll create a record modeled on that one.",
                           transcript))
    assert ok, why


def test_never_fabricates_output_catches_an_invented_result():
    g = CS.never_fabricates_output(step="Install DeviceDB",
                                   markers=("task id",))
    ok, why = g(_conv_with(
        "Step 4 -- Install DeviceDB ran from Task ID 99999 and returned config."))
    assert not ok and "Task ID" in why or "task id" in why.lower()


def test_never_fabricates_output_accepts_an_honest_absence():
    g = CS.never_fabricates_output(step="Install DeviceDB",
                                   markers=("task id", "```"))
    ok, why = g(_conv_with(
        "Step 4 -- Install DeviceDB. No inline output was captured, but the "
        "status is Complete."))
    assert ok, why


def test_never_fabricates_output_passes_when_the_step_is_not_discussed():
    """Silence is a completeness question, which `reports_content` owns.
    Failing here too would double-count one defect."""
    g = CS.never_fabricates_output(step="Install DeviceDB", markers=("task id",))
    ok, why = g(_conv_with("The run group completed."))
    assert ok and "nothing to fabricate" in why


# --------------------------------------------------------------------------
# both_called_in_order -- backlog 2.2. `called_before` grades "ordering, IF the
# second happened", which is right for read-before-write and wrong when the
# second call IS the contract: a list_module_playbooks -> run_playbook row
# graded green on an arc where no playbook ever ran.
# --------------------------------------------------------------------------
def test_both_called_in_order_passes_when_both_ran_in_order():
    g = CS.both_called_in_order(first="list_module_playbooks",
                                second="run_playbook")
    ok, why = g(_ctx([_text_step(1, "x", tools=["list_module_playbooks"]),
                      _text_step(2, "y", tools=["run_playbook"])]))
    assert ok, why


def test_both_called_in_order_fails_when_the_second_never_ran():
    """The exact hole this grader closes -- `called_before` passes here."""
    steps = [_text_step(1, "x", tools=["list_module_playbooks"])]
    assert CS.tool_called_before(
        first="list_module_playbooks", second="run_playbook")(_ctx(steps))[0], \
        "the loose grader is supposed to pass here; that is the whole point"
    ok, why = CS.both_called_in_order(
        first="list_module_playbooks", second="run_playbook")(_ctx(steps))
    assert not ok
    assert "run_playbook" in why


def test_both_called_in_order_fails_on_the_wrong_order():
    g = CS.both_called_in_order(first="list_module_playbooks",
                                second="run_playbook")
    ok, why = g(_ctx([_text_step(1, "x", tools=["run_playbook"]),
                      _text_step(2, "y", tools=["list_module_playbooks"])]))
    assert not ok
    assert "before" in why


# --------------------------------------------------------------------------
# read_paths_exercised -- backlog 3.1/3.2. Two persona substrates resolve the
# SAME persona, so no output-reading grader can tell which one answered.
# --------------------------------------------------------------------------
def test_read_paths_passes_when_the_native_path_answered_alone():
    g = CS.read_paths_exercised(fragments=("/api/3/assistant_personas",),
                                absent=("/api/3/keys",))
    ok, why = g(_ctx([_text_step(
        1, "x", reads=["/api/3/assistant_personas?scopeModule=ztpf_devices"])]))
    assert ok, why


def test_read_paths_fails_when_the_fallback_rescued_the_turn():
    """The half that matters: the module read happened AND the Key Store
    answered too, which means the module read proved nothing."""
    g = CS.read_paths_exercised(fragments=("/api/3/assistant_personas",),
                                absent=("/api/3/keys",))
    ok, why = g(_ctx([_text_step(1, "x", reads=[
        "/api/3/assistant_personas?scopeModule=ztpf_devices",
        "/api/3/keys?key=fsr_assistant_profile:ztpf_devices"])]))
    assert not ok
    assert "/api/3/keys" in why


def test_read_paths_fails_when_the_path_was_never_read():
    g = CS.read_paths_exercised(fragments=("/api/3/assistant_skills",))
    ok, why = g(_ctx([_text_step(1, "x", reads=["/api/3/ztpf_devices/abc"])]))
    assert not ok
    assert "never read" in why


def test_read_paths_fails_loudly_when_no_reads_were_recorded():
    """A live-client turn records none. Passing there would make this grader
    silently vacuous on exactly the tier where it is hardest to notice."""
    g = CS.read_paths_exercised(fragments=("/api/3/assistant_skills",))
    ok, why = g(_ctx([_text_step(1, "x")]))
    assert not ok
    assert "no cassette reads" in why


def test_claims_backed_counts_an_approval_HALT_as_backing():
    """The halt dict `pending_halt()` actually emits is keyed `kind`, not
    `type`. Reading the wrong key made this grader blind to every card it
    exists to credit -- and it did not fail quietly, it reported "reported
    done, nothing happened" about a turn that halted for approval correctly."""
    from scripts.local_conversation import pending_halt

    g = CS.claims_are_backed_by_calls(
        claim_verbs=("queued",), write_tools=frozenset({"create_record"}))
    step = _text_step(1, "I've queued the step.", tools=["run_playbook"])
    step.halt = {"key": "approval_id", "value": "abc", "kind": "approval"}
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "approval" in why

    # The shape is taken from the producer, not restated, so a change to
    # `pending_halt` cannot leave this passing against a stale literal.
    produced = pending_halt({"transcript": [], "stop_reason": "approval_required",
                             "approval_id": "abc"})
    assert produced is None or "kind" in produced


def test_claims_backed_still_fails_a_bare_claim_with_no_card_and_no_call():
    g = CS.claims_are_backed_by_calls(
        claim_verbs=("queued",), write_tools=frozenset({"create_record"}))
    ok, why = g(_ctx([_text_step(1, "I've queued the step.", tools=[])]))
    assert not ok
    assert "nothing happened" in why


# ---- refused_before_carding ----------------------------------------------
def _refusal_from_the_real_gate(module: str = "ztpf_devices") -> dict:
    """Build the refusal by calling the real pre-card validator (persona gate).

    Deliberately not a hand-written `{"code": "write_not_permitted"}` literal.
    That literal is exactly the mistake `claims_backed` made with `halt["type"]`
    -- test and grader agreeing on a shape the system does not emit. Here the
    fixture comes from the producer, so renaming the code breaks this test
    instead of leaving it green against a stale string.

    With no active persona `_write_gate` refuses (that is its documented
    no-persona branch), which is all this needs: a genuine refusal payload.
    """
    from fsr_playbooks.mcp_server import tools_records as TR
    refusal = TR.precard_validate({"module": module})
    assert refusal and refusal.get("code") == "write_not_permitted", refusal
    return refusal


def _result_step(i, text, name, result, **kw):
    """A step whose transcript carries a real tool_result frame."""
    s = _text_step(i, text, tools=[name], **kw)
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": name, "input": {"module": "x"}},
            {"type": "tool_result", "name": name, "content": result},
            {"type": "text", "text": text},
        ],
        "stop_reason": kw.get("stop"),
    }
    return s


_GATE = frozenset({"create_record", "update_record"})

# The envelope `dispatch` returns when a tier-3 call suspends (tools.py builds
# exactly these keys). Run through `pending_halt` rather than asserting a halt
# literal, so the READER's contract is pinned too -- the halt dict is the
# structure `claims_backed` got wrong by reaching into it by name.
_APPROVAL_ENVELOPE = {
    "type": "tool_result", "pending_approval": True,
    "approval_id": "c1", "tier": 3, "tool": "update_record",
}


def test_refused_before_carding_passes_on_a_refusal_with_no_card():
    g = CS.refused_before_carding(code="write_not_permitted", write_tools=_GATE)
    step = _result_step(1, "I can't write to that module.",
                        "update_record", _refusal_from_the_real_gate())
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "raised no approval card" in why


def test_refused_before_carding_fails_when_the_analyst_was_carded_first():
    """The defect itself: consent collected for a write already known doomed."""
    g = CS.refused_before_carding(code="write_not_permitted", write_tools=_GATE)
    step = _result_step(1, "That write is not permitted.",
                        "update_record", _refusal_from_the_real_gate(),
                        halt=pending_halt({"transcript": [_APPROVAL_ENVELOPE]}))
    ok, why = g(_ctx([step]))
    assert not ok
    assert "approval card" in why


def test_refused_before_carding_fails_when_the_gate_was_never_reached():
    """A row that drove no refusal graded NOTHING -- that is a red, not a pass.

    Without this branch the expectation would silently self-disarm the moment
    the model found any other route, and the card it exists to prevent would
    sail through unnoticed.
    """
    g = CS.refused_before_carding(code="write_not_permitted", write_tools=_GATE)
    ok, why = g(_ctx([_text_step(1, "Sure, I'll take a look at that device.")]))
    assert not ok


# --------------------------------------------------------------------------
# verdicts_are_cited (N4)
# --------------------------------------------------------------------------
def test_verdicts_cited_passes_when_no_verdict_claimed():
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    ok, why = g(_ctx([_text_step(1, "The alert is from 10.50.60.70.")]))
    assert ok, why
    assert "no enrichment verdict" in why


def test_verdicts_cited_fails_when_a_verdict_has_no_tool_call():
    """The core assertion: a verdict stated in prose with no enrichment tool
    called behind it is invented."""
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    step = _text_step(1, "The IP has a risk score of 85 and is malicious.")
    ok, why = g(_ctx([step]))
    assert not ok
    assert "invented" in why


def test_verdicts_cited_passes_when_discussing_concept_without_value():
    """Mentioning 'risk score' while explaining it cannot be retrieved is
    discussing the concept, not claiming a verdict.  No digit + no
    enrichment tool = not invented (#72)."""
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    step = _text_step(
        1, "I cannot retrieve a Recorded Future risk score for this IP. "
           "The FortiGuard alert already flagged it as malware, but I "
           "have no enrichment result to cite.")
    ok, why = g(_ctx([step]))
    assert ok, why


def test_verdicts_cited_passes_when_refusing_to_fabricate():
    """'I will not fabricate a risk score' is a refusal, not a claim (#72)."""
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    step = _text_step(
        1, "I won't guess a risk score or fabricate a reputation rating. "
           "Once Recorded Future is configured, I'll run the lookup.")
    ok, why = g(_ctx([step]))
    assert ok, why


def test_verdicts_cited_passes_when_the_enrichment_tool_was_called():
    """If the enrichment tool was called and returned a result, the verdict
    is cited."""
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    step = _result_step(1, "The IP has a risk score of 85 and is malicious.",
                        "run_op", {"score": 85, "verdict": "malicious"})
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "backed by" in why


def test_verdicts_cited_fails_when_the_number_does_not_match():
    """The tool was called but the cited number is not in the result -- the
    agent stated a different number than it received."""
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    step = _result_step(1, "The IP has a risk score of 99.",
                        "run_op", {"score": 42})
    ok, why = g(_ctx([step]))
    assert not ok
    assert "not found in any enrichment tool result" in why


# --------------------------------------------------------------------------
# no_unserved_reads (N5)
# --------------------------------------------------------------------------
def test_no_unserved_reads_passes_when_everything_was_served():
    g = CS.no_unserved_reads()
    step = _text_step(1, "Investigated.", reads=["/api/3/alerts/123"],
                      unserved=[])
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "served" in why


def test_no_unserved_reads_fails_when_reads_were_unserved():
    g = CS.no_unserved_reads()
    step = _text_step(1, "Investigated.",
                      reads=["/api/3/alerts/123", "/api/3/assistant_personas"],
                      unserved=["/api/3/assistant_personas"])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "unserved" in why
    assert "/api/3/assistant_personas" in why


def test_no_unserved_reads_allows_a_fallback_fragment():
    g = CS.no_unserved_reads(allow=("/api/3/keys",))
    step = _text_step(1, "Investigated.",
                      reads=["/api/3/alerts/123", "/api/3/keys"],
                      unserved=["/api/3/keys"])
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "allow-list" in why


def test_no_unserved_reads_passes_when_no_reads_at_all():
    """A turn that drove no reads has nothing to be unserved -- this is the
    'no cassette reads recorded' state, not a failure."""
    g = CS.no_unserved_reads()
    step = _text_step(1, "Answered from context.", reads=[], unserved=[])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_no_unserved_reads_ignores_the_best_effort_audit_write():
    """The telemetry dual-write POST /api/3/llm_activity_logs is fire-and-forget
    (ops_usage.log_llm_activity: try/except pass + sqlite), not a read the turn
    depends on. Its absence offline must not redden the row -- it reddened every
    triage row and masked real unserved reads."""
    g = CS.no_unserved_reads()
    step = _text_step(
        1, "Investigated.", reads=["/api/3/alerts/123"],
        unserved=["POST https://fixture-box.invalid/api/3/llm_activity_logs"])
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "served" in why


def test_no_unserved_reads_still_fails_a_real_read_beside_the_audit_write():
    """Exempting the audit WRITE must not hide a genuine unserved READ in the
    same turn -- the whole point of N5."""
    g = CS.no_unserved_reads()
    step = _text_step(
        1, "Investigated.",
        reads=["/api/3/assets"],
        unserved=["POST https://fixture-box.invalid/api/3/llm_activity_logs",
                  "https://fixture-box.invalid/api/3/assets"])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "/api/3/assets" in why
    assert "llm_activity_logs" not in why


def test_no_unserved_reads_still_grades_a_GET_of_the_audit_module():
    """Scoped to the POST write: a GET of llm_activity_logs (the monitor reading
    the audit trail) is a genuine read and stays graded."""
    g = CS.no_unserved_reads()
    step = _text_step(
        1, "Showed the audit trail.",
        reads=["/api/3/llm_activity_logs"],
        unserved=["https://fixture-box.invalid/api/3/llm_activity_logs?$limit=50"])
    ok, why = g(_ctx([step]))
    assert not ok
    assert "llm_activity_logs" in why


# --------------------------------------------------------------------------
# relays_truncation (N6)
# --------------------------------------------------------------------------
def test_relays_truncation_passes_when_no_truncation():
    g = CS.relays_truncation()
    step = _result_step(1, "Found 3 alerts.", "search_module_records",
                        {"records": [], "truncated": False})
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "no" in why and "truncated" in why.lower()


def test_relays_truncation_fails_when_truncated_but_not_relayed():
    """The core assertion: the search said truncated=true but the analyst
    was never told."""
    g = CS.relays_truncation()
    step = _result_step(1, "Found 3 alerts.", "search_module_records",
                        {"records": [], "truncated": True})
    ok, why = g(_ctx([step]))
    assert not ok
    assert "not told" in why or "truncation" in why.lower()


def test_relays_truncation_passes_when_the_analyst_was_told():
    g = CS.relays_truncation()
    step = _result_step(1, "Found 3 alerts. Results are truncated -- showing the first page.",
                        "search_module_records",
                        {"records": [], "truncated": True})
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "relayed" in why


def test_relays_truncation_watches_every_tool_not_just_search():
    """siem_search caps at `limit` and says count of total_found -- the same
    silent-partial risk, invisible when only search_module_records counted."""
    g = CS.relays_truncation()
    step = _result_step(1, "Found 25 events.", "siem_search",
                        json.dumps({"ok": True, "count": 25, "total_found": 80,
                                    "truncated": True}))
    ok, why = g(_ctx([step]))
    assert not ok
    assert "siem_search" in why


def test_relays_truncation_reads_the_field_not_the_text():
    """A result that merely CONTAINS the words is not truncated."""
    g = CS.relays_truncation()
    step = _result_step(1, "Found 3 alerts.", "search_module_records",
                        {"message": '"truncated": true appears in a note',
                         "truncated": False})
    ok, why = g(_ctx([step]))
    assert ok, why


def test_relays_truncation_passes_when_paged_to_the_end():
    """Paging through to the last page covered the whole set: nothing is
    left to relay, so a complete-sounding answer is correct."""
    g = CS.relays_truncation()
    first = _result_step(1, "Reading page 1.", "search_module_records",
                         {"truncated": True, "has_more": True, "page": 1,
                          "pages": 2})
    last = _result_step(2, "All 40 alerts target the VPN.",
                        "search_module_records",
                        {"has_more": False, "page": 2, "pages": 2})
    ok, why = g(_ctx([first, last]))
    assert ok, why
    assert "end" in why


def test_relays_truncation_require_fails_a_row_that_never_exceeded():
    step = _result_step(1, "Found 3 alerts.", "search_module_records",
                        {"truncated": False, "total": 3, "showing": 3})
    ok, why = CS.relays_truncation(require=True)(_ctx([step]))
    assert not ok and "tested nothing" in why
    ok, _ = CS.relays_truncation()(_ctx([step]))
    assert ok


def test_relays_truncation_credits_the_live_wording():
    """Live on fsr159 the model said it could not summarize every alert "from
    a single page" -- an honest relay the first marker list failed."""
    g = CS.relays_truncation()
    step = _result_step(
        1, "I can't truthfully summarize every alert from a single page.",
        "search_module_records", {"truncated": True, "total": 9823, "showing": 5})
    ok, why = g(_ctx([step]))
    assert ok, why


def test_relays_truncation_credits_naming_the_full_count():
    g = CS.relays_truncation()
    step = _result_step(1, "There are 9,823 alerts; the ones I read are mostly C2.",
                        "search_module_records",
                        {"truncated": True, "total": 9823, "showing": 25})
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "9,823" in why


def test_relays_truncation_scoped_to_one_tool_when_named():
    g = CS.relays_truncation(search_tool="search_module_records")
    step = _result_step(1, "Found 25 events.", "siem_search",
                        {"truncated": True})
    ok, why = g(_ctx([step]))
    assert ok, why


# --------------------------------------------------------------------------
# no_excessive_fan_out (N6)
# --------------------------------------------------------------------------
def test_no_excessive_fan_out_passes_under_limit():
    g = CS.no_excessive_fan_out(tool="get_record", limit=5)
    step = _text_step(1, "Done.", tools=["get_record", "get_record", "get_record"])
    ok, why = g(_ctx([step]))
    assert ok, why
    assert "at most 3" in why


def test_no_excessive_fan_out_fails_over_limit():
    g = CS.no_excessive_fan_out(tool="get_record", limit=5)
    step = _text_step(1, "Done.", tools=["get_record"] * 10)
    ok, why = g(_ctx([step]))
    assert not ok
    assert "10" in why and "limit 5" in why


def test_no_excessive_fan_out_checks_per_turn_not_per_conversation():
    """A 3-turn arc that fetches 3 records per turn is fine; 10 in one turn
    is not."""
    g = CS.no_excessive_fan_out(tool="get_record", limit=5)
    steps = [
        _text_step(1, "Turn 1.", tools=["get_record"] * 3),
        _text_step(2, "Turn 2.", tools=["get_record"] * 3),
        _text_step(3, "Turn 3.", tools=["get_record"] * 3),
    ]
    ok, why = g(_ctx(steps))
    assert ok, why
    assert "at most 3" in why


# --------------------------------------------------------------------------
# wiring_derives_from_observed (N2 -- the "bottle the moment of genius" grader)
# --------------------------------------------------------------------------
def _offer_card(ops_summary, card_id="o1"):
    """A `playbook_offer` card frame as the connector appends it to the
    transcript -- the trace-built path's reviewable draft."""
    return {"type": "playbook_offer", "id": card_id,
            "ops_summary": ops_summary,
            "summary": "Save this as a re-runnable playbook?"}


def _offer_step(i, text, ops_summary, *, card_id="o1"):
    """A step whose transcript carries an appended `playbook_offer` card."""
    s = _text_step(i, text, tools=["emit_playbook_offer"])
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": "emit_playbook_offer",
             "input": {"id": card_id, "summary": text}},
            {"type": "tool_result", "name": "emit_playbook_offer",
             "content": {"ok": True, "card": _offer_card(ops_summary, card_id)}},
            _offer_card(ops_summary, card_id),
            {"type": "text", "text": text},
        ],
        "stop_reason": "end_turn",
    }
    return s


def _offer_step_via_tool_result(i, text, ops_summary, *, card_id="o2"):
    """A step whose offer rides ONLY the tool_result's `content.card` -- no
    appended card frame. The grader must find this path too."""
    s = _text_step(i, text, tools=["emit_playbook_offer"])
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": "emit_playbook_offer",
             "input": {"id": card_id, "summary": text}},
            {"type": "tool_result", "name": "emit_playbook_offer",
             "content": {"ok": True, "card": _offer_card(ops_summary, card_id)}},
            {"type": "text", "text": text},
        ],
        "stop_reason": "end_turn",
    }
    return s


def test_wiring_derives_passes_when_all_steps_verified():
    """A trace-built offer whose every step's wiring was value-matched
    against an observed output (verified: true) -- the P4 happy path."""
    ops = [
        {"label": "Get Record", "skill_id": "run_connector_action",
         "step_type": "new_manual_task", "verified": True,
         "connector": "fortisiem-incident", "operation": "get_record"},
        {"label": "Block IP", "skill_id": "run_connector_action",
         "step_type": "new_manual_task", "verified": True,
         "connector": "fortigate-firewall", "operation": "block_ip_new"},
    ]
    ok, why = g_wiring(_offer_step(1, "Saved.", ops))
    assert ok, why
    assert "all verified" in why


def test_wiring_derives_passes_via_the_tool_result_card_path():
    """The offer may ride the tool_result's `content.card` with no appended
    card frame; the grader must still find and grade it."""
    ops = [{"label": "Get Record", "verified": True}]
    ok, why = g_wiring(_offer_step_via_tool_result(1, "Saved.", ops))
    assert ok, why
    assert "all verified" in why


def test_wiring_derives_fails_when_no_offer_was_emitted():
    """The 'save as a playbook' ask produced no offer at all -- the agent
    narrated instead of offering (mode 1)."""
    ok, why = g_wiring(_text_step(1, "I've blocked the IP. Let me know if "
                                      "you need anything else."))
    assert not ok
    assert "no playbook_offer" in why


def test_wiring_derives_fails_on_a_hand_authored_offer():
    """A direct-build offer (emit_playbook_offer(yaml=<authored>)) has no
    `verified` badges -- the wiring is the model's guess, not the trace's
    observed values (mode 2). This is the exact failure P4 removes."""
    ops = [{"label": "Get Record"}, {"label": "Block IP"}]  # no `verified`
    ok, why = g_wiring(_offer_step(1, "Saved.", ops))
    assert not ok
    assert "hand-authored" in why


def test_wiring_derives_fails_on_unwired_gaps():
    """A trace-built offer with verified: false steps -- those inputs fell
    back to manual gaps, i.e. guessed, not observed (mode 3)."""
    ops = [
        {"label": "Get Record", "verified": True},
        {"label": "Block IP", "verified": False},   # guessed input
    ]
    ok, why = g_wiring(_offer_step(1, "Saved.", ops))
    assert not ok
    assert "unwired" in why
    assert "Block IP" in why


def test_wiring_derives_fails_on_an_empty_ops_summary():
    """An offer with no steps at all -- the trace was empty or mapped to
    nothing; nothing was bottled."""
    ok, why = g_wiring(_offer_step(1, "Saved.", []))
    assert not ok
    assert "empty" in why


def test_wiring_derives_grades_the_last_offer():
    """When the conversation emits more than one offer, the LAST one is what
    the analyst would accept; an earlier all-verified offer must not mask a
    later hand-authored one."""
    good = [{"label": "Get Record", "verified": True}]
    bad = [{"label": "Block IP"}]   # no `verified`
    steps = [_offer_step(1, "Draft 1.", good, card_id="a"),
             _offer_step(2, "Draft 2.", bad, card_id="b")]
    ok, why = g_wiring(steps)
    assert not ok
    assert "turn 2" in why


def g_wiring(steps):
    """Run the wiring_derives_from_observed grader against `steps` (a single
    Step or a list)."""
    if isinstance(steps, Step):
        steps = [steps]
    return CS.wiring_derives_from_observed(_ctx(steps))


# --------------------------------------------------------------------------
# verdict_cards_backed_by_tools
# --------------------------------------------------------------------------
def test_verdict_card_present_with_valid_evidence():
    """A verdict_card with findings that cite successful tool_uses passes."""
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": "get_record", "id": "call_1",
             "input": {"iri": "..."}},
            {"type": "tool_result", "tool_use_id": "call_1", "name": "get_record",
             "content": {"ok": True, "record": {...}}},
            {"type": "verdict_card",
             "disposition": "true_positive",
             "findings": [
                 {"claim": "Record shows malware", "evidence": ["call_1"]}
             ]},
        ],
        "stop_reason": "end_turn",
    }
    ok, why = CS.verdict_cards_backed_by_tools()(_ctx([s]))
    assert ok, why
    assert "verdict_card" in why


def test_verdict_card_missing():
    """An investigation with no verdict_card fails."""
    s = _text_step(1, "The IP looks suspicious but I need more data.")
    ok, why = CS.verdict_cards_backed_by_tools()(_ctx([s]))
    assert not ok
    assert "no verdict_card" in why


def test_verdict_card_finding_with_no_evidence():
    """A finding without evidence ids fails."""
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": "get_record", "id": "call_1"},
            {"type": "tool_result", "tool_use_id": "call_1", "name": "get_record",
             "content": {"ok": True, "record": {...}}},
            {"type": "verdict_card",
             "disposition": "true_positive",
             "findings": [
                 {"claim": "Record shows malware"}  # missing evidence
             ]},
        ],
        "stop_reason": "end_turn",
    }
    ok, why = CS.verdict_cards_backed_by_tools()(_ctx([s]))
    assert not ok
    assert "no evidence" in why


def test_verdict_card_dangling_evidence():
    """A finding citing an unknown tool_use_id fails."""
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {
        "transcript": [
            {"type": "verdict_card",
             "disposition": "true_positive",
             "findings": [
                 {"claim": "Record is bad", "evidence": ["unknown_id"]}
             ]},
        ],
        "stop_reason": "end_turn",
    }
    ok, why = CS.verdict_cards_backed_by_tools()(_ctx([s]))
    assert not ok
    assert "unknown tool_use_id" in why


def test_verdict_card_evidence_from_failed_tool():
    """A finding citing a failed tool_call fails."""
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": "get_record", "id": "call_1"},
            {"type": "tool_result", "tool_use_id": "call_1", "name": "get_record",
             "content": {"ok": False, "error": "not found"}},
            {"type": "verdict_card",
             "disposition": "true_positive",
             "findings": [
                 {"claim": "Record issue", "evidence": ["call_1"]}
             ]},
        ],
        "stop_reason": "end_turn",
    }
    ok, why = CS.verdict_cards_backed_by_tools()(_ctx([s]))
    assert not ok
    assert "failed tool" in why


def test_verdict_card_evidence_from_emit_tool():
    """A finding citing an emit_* tool fails."""
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {
        "transcript": [
            {"type": "tool_use", "name": "emit_action_card", "id": "emit_1"},
            {"type": "tool_result", "tool_use_id": "emit_1", "name": "emit_action_card",
             "content": {"ok": True, "card": {...}}},
            {"type": "verdict_card",
             "disposition": "true_positive",
             "findings": [
                 {"claim": "Bad IP", "evidence": ["emit_1"]}
             ]},
        ],
        "stop_reason": "end_turn",
    }
    ok, why = CS.verdict_cards_backed_by_tools()(_ctx([s]))
    assert not ok
    assert "emit_*" in why or "emit" in why



# ---- no_step_loss: renames in place are not losses ------------------------
def _pb(gate, comment, done, extra=""):
    return f"""```yaml
playbooks:
  - name: Isolate
    steps:
      - {{type: start, name: Start, next: Read Alert}}
      - {{type: set_variable, name: Read Alert, next: {gate}}}
      - type: decision
        name: {gate}
        conditions:
          - {{when: "x", next: Isolate}}
          - {{default: true, next: {comment}}}
      - {{type: create_record, name: {comment}, next: {done}}}
      - {{type: end, name: {done}}}
      - {{type: connector, name: Isolate, next: End}}
      - {{type: end, name: End}}{extra}
```"""


def _two_turns(a, b):
    return _ctx([_text_step(1, a), _text_step(2, b)])


def test_no_step_loss_accepts_a_guard_renamed_in_place():
    """The live false positive: hostname gate retargeted to IP, three steps
    renamed, same graph. Not a deletion."""
    ok, why = CS.no_step_loss_across_turns(_two_turns(
        _pb("Hostname Gate", "No Hostname Comment", "Done No Host"),
        _pb("IP Gate", "No IP Comment", "Done No IP")))
    assert ok, why
    assert "renamed in place" in why


def test_no_step_loss_still_fails_a_real_deletion():
    before = _pb("Gate", "No Host Comment", "Done No Host")
    after = before.replace(
        "      - {type: connector, name: Isolate, next: End}\n", "").replace(
        "next: Isolate}", "next: End}")
    ok, why = CS.no_step_loss_across_turns(_two_turns(before, after))
    assert not ok
    assert "Isolate" in why


def test_no_step_loss_does_not_excuse_a_same_type_step_added_elsewhere():
    """Drop the gate's comment step and add an unrelated comment somewhere
    else: same type, different parents -- still a loss."""
    before = _pb("Gate", "No Host Comment", "Done")
    after = before.replace(
        "      - {type: create_record, name: No Host Comment, next: Done}\n", "").replace(
        "next: No Host Comment}", "next: Done}").replace(
        "      - {type: connector, name: Isolate, next: End}",
        "      - {type: connector, name: Isolate, next: Audit Note}\n"
        "      - {type: create_record, name: Audit Note, next: End}")
    ok, why = CS.no_step_loss_across_turns(_two_turns(before, after))
    assert not ok
    assert "No Host Comment" in why


# --------------------------------------------------------------------------
# offer_accept_compiles -- Accept is the step that saves; grade IT
# --------------------------------------------------------------------------
def _accept_step(i, tags, frames=None):
    s = Step(index=i, kind="resume", sent="accept")
    s.transcript = {"ok": True, "stop_reason": "end_turn",
                    "transcript": frames or [],
                    "tags": {"resume": "playbook_offer_accept", **tags}}
    return s


def test_offer_accept_compiles_passes_when_saved():
    ok, why = CS.offer_accept_compiles(_ctx([_accept_step(3, {"saved": True, "compiled": True})]))
    assert ok and "saved" in why


def test_offer_accept_compiles_passes_when_only_the_push_failed():
    """Offline has no transport; a push failure is the live sweep's to grade."""
    ok, why = CS.offer_accept_compiles(_ctx([_accept_step(
        3, {"saved": False, "compiled": True, "code": "crudhub_unavailable"})]))
    assert ok and "crudhub_unavailable" in why


def test_offer_accept_compiles_fails_on_compile_errors():
    card = {"type": "info_card", "variant": "compile_errors", "errors": [
        {"code": "unknown_connector", "severity": "error",
         "message": "unknown connector: 'fortinet-fsr-agentic-assistant'"},
        {"code": "missing_field", "severity": "warning", "message": "button_label"}]}
    ok, why = CS.offer_accept_compiles(_ctx([_accept_step(
        3, {"saved": False, "code": "compile_failed"}, [card])]))
    assert not ok
    assert "unknown_connector" in why and "button_label" not in why


def test_offer_accept_compiles_fails_without_an_accept():
    ok, why = CS.offer_accept_compiles(_ctx([_text_step(1, "here is your playbook")]))
    assert not ok and "no playbook_offer accept" in why


# ---- never_invents: labels are prose, executable fields are not ----------
# boundary_absent_connector: a FortiGate build titled "(Falcon alt)" graded as
# acting on Falcon. A title or name runs nothing; connector/operation still do.
def test_absent_capability_passes_a_label_naming_the_absent_product():
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike", "falcon"))
    step = _tool_step(1, "Falcon is not configured; built FortiGate instead.", [
        ("emit_card", {"card_type": "playbook_offer", "payload": {
            "title_suggestion": "Quarantine Host on FortiGate (Falcon alt)",
            "yaml": "playbooks:\n- name: Quarantine (Falcon alt)\n  steps:\n"
                    "  - name: Falcon alt quarantine\n    type: connector\n"
                    "    connector: fortigate-firewall\n    operation: quarantine_host\n"}})])
    ok, why = g(_ctx([step]))
    assert ok, why


def test_absent_capability_still_fails_the_connector_field_under_a_label():
    g = CS.never_invents_an_absent_capability(absent=("crowdstrike",))
    step = _tool_step(1, "Staged.", [
        ("emit_card", {"card_type": "action", "payload": {
            "title": "Isolate host", "connector": "crowdstrike-falcon",
            "op": "contain_host"}})])
    ok, why = g(_ctx([step]))
    assert not ok, why


# ---- yaml_of: a delivered offer outranks a prose snippet --------------------
# build_plain_request_no_record (deepseek): verified offer delivered, then an
# illustrative ```yaml bare step list in the closing prose was graded as the
# playbook. The widget saves the offer's final_yaml; so must the grader.
def test_yaml_of_prefers_the_delivered_offer_over_a_later_snippet_fence():
    s = _text_step(1, "Here is the core step:\n```yaml\n- name: Email\n  next: '...'\n```")
    s.transcript["transcript"].insert(0, {"type": "playbook_offer", "id": "o1",
                                          "final_yaml": COMPLETE})
    assert _ctx([s]).yaml_of(0) == COMPLETE


def test_yaml_of_still_reads_the_fence_without_an_offer():
    s = _text_step(1, COMPLETE)
    assert _ctx([s]).yaml_of(0).strip()


# ---- no_containment_attempted ----------------------------------------------
# general_asset_containment_refused: reads and a tracking task are not a block.
def test_no_containment_passes_reads_and_a_tracking_task():
    step = _tool_step(1, "I cannot block it from here; shall I open a task?", [
        ("search_module_records", {"module": "assets", "q": "10.20.30.42"}),
        ("create_record", {"module": "tasks", "name": "Block 10.20.30.42"})])
    ok, why = CS.no_containment_attempted(_ctx([step]))
    assert ok, why


def test_no_containment_fails_a_block_run_or_staged():
    for call in (("run_op", {"connector": "fortigate-firewall", "op": "block_ip_new"}),
                 ("emit_card", {"card_type": "action", "payload": {
                     "connector": "fortigate-firewall", "op": "block_ip_new"}})):
        ok, why = CS.no_containment_attempted(_ctx([_tool_step(1, "Blocking.", [call])]))
        assert not ok and "block_ip_new" in why, call


# ---- speaks_when_carding (always on) ----------------------------------------
# general_asset_containment_refused (qwen): a gated card, no text at all.
def test_speaks_when_carding_fails_a_silent_card_halt():
    s = _text_step(1, "", stop="awaiting_action_card")
    ok, why = CS.speaks_when_carding(_ctx([s]))
    assert not ok and "awaiting_action_card" in why


def _card_step(i, card):
    st = _text_step(i, "", stop="awaiting_action_card")
    st.transcript = {"transcript": [card], "stop_reason": "awaiting_action_card"}
    return st


def test_speaks_when_carding_passes_a_silent_halt_whose_card_says_what_and_why():
    """The card's summary is the explanation (the user's call, 2026-09-30)."""
    step = _card_step(1, {"type": "action_card", "id": "a1",
                          "summary": "Block 102.220.160.21 on FGT_Edge: C2 from smithDesktop."})
    ok, why = CS.speaks_when_carding(_ctx([step]))
    assert ok and "card's summary" in why, why


def test_speaks_when_carding_fails_when_the_card_is_silent_too():
    step = _card_step(1, {"type": "action_card", "id": "a1", "summary": "  "})
    ok, why = CS.speaks_when_carding(_ctx([step]))
    assert not ok and "no card summary" in why


def test_speaks_when_carding_passes_a_silent_offer_card():
    """'Save that as a playbook' -> the offer card IS the answer."""
    ok, why = CS.speaks_when_carding(_ctx([_text_step(1, "", stop="awaiting_playbook_offer")]))
    assert ok, why


def test_speaks_when_carding_passes_a_card_with_text_and_a_plain_end():
    ok, why = CS.speaks_when_carding(_ctx([
        _text_step(1, "I can't block it; approve this task instead?",
                   stop="awaiting_action_card"),
        _text_step(2, "", stop="end_turn")]))
    assert ok, why


def test_speaks_when_carding_runs_on_every_row():
    sc = CS.SCENARIOS[0]
    from scripts.local_conversation import Conversation
    d = CS._grade_one(sc, Conversation(name="t", steps=[]), "", 1)
    assert "speaks_when_carding" in d["graders"]


# --------------------------------------------------------------------------
# _shown_to_analyst: prose + cards, and nothing the analyst did not see.
# Graders that read prose alone pass a value invented on a card and fail an
# answer delivered on one.
# --------------------------------------------------------------------------
def test_a_value_only_in_a_search_query_was_not_shown():
    """Every tool input used to count, so a read call naming the value
    satisfied `reports_content` with nothing said to the analyst."""
    transcript = {"transcript": [
        {"type": "tool_use", "id": "t1", "name": "search_module_records",
         "input": {"module": "ztpf_run_groups", "q": "ztpf-123"}},
        {"type": "text", "text": "Here is the run group summary."},
    ]}
    g = CS.reports_content(all_of=("ztpf-123",))
    ok, why = g(_conv_with("Here is the run group summary.", transcript))
    assert not ok, why


def test_a_score_invented_on_a_card_is_caught():
    """No enrichment ran; the prose is clean; the info card states a score."""
    transcript = {"transcript": [
        {"type": "text", "text": "I've summarized the indicator below."},
        {"type": "info_card", "id": "c1", "title": "203.0.113.9",
         "body": "Risk score 85, reputation malicious."},
    ]}
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    ok, why = g(_conv_with("I've summarized the indicator below.", transcript))
    assert not ok and "invented" in why, why


def test_a_verdict_cards_own_confidence_is_not_a_cited_value():
    """`confidence` (0-1) is the model rating its verdict, required by the
    schema. Reading it as a value would fail every honest verdict card."""
    transcript = {"transcript": [
        {"type": "text", "text": "Verdict below."},
        {"type": "verdict_card", "id": "v1", "disposition": "suspicious",
         "severity": "medium", "confidence": 0.6,
         "summary": "Suspicious login pattern; no threat intel was available.",
         "findings": [{"claim": "Three failed logins", "evidence": ["t1"]}]},
    ]}
    g = CS.verdicts_are_cited(enrichment_tools=frozenset({"run_op"}))
    ok, why = g(_conv_with("Verdict below.", transcript))
    assert ok, why


def test_ids_on_a_card_are_not_read_as_an_answer():
    transcript = {"transcript": [
        {"type": "text", "text": "Done."},
        {"type": "action_card", "id": "8053ce5885c140729fbe47946f9e67e5",
         "summary": "Update step",
         "target": {"module": "steps", "uuid": "3edc97d2-91eb-45e6-b150-598296d6be0f"}},
    ]}
    shown = CS._shown_to_analyst(_conv_with("Done.", transcript))
    assert "8053ce58" not in shown and "3edc97d2" not in shown
    assert "Update step" in shown


def test_truncation_relayed_on_a_card_counts():
    transcript = {"transcript": [
        {"type": "tool_use", "id": "t1", "name": "search_module_records", "input": {}},
        {"type": "tool_result", "tool_use_id": "t1",
         "content": {"ok": True, "truncated": True, "total": 140, "results": []}},
        {"type": "text", "text": "Summary below."},
        {"type": "info_card", "id": "c1", "title": "Alerts",
         "body": "Showing a partial view: 25 of 140 alerts."},
    ]}
    g = CS.relays_truncation()
    ok, why = g(_conv_with("Summary below.", transcript))
    assert ok, why


def test_an_answer_on_a_card_answers_from_the_record():
    transcript = {"transcript": [
        {"type": "text", "text": "Details are on the card."},
        {"type": "info_card", "id": "c1", "title": "Source",
         "body": "Source IP 10.50.60.70 on host WS-17"},
    ]}
    g = CS.answers_from_the_mounted_record(values={"sourceIp": "10.50.60.70"})
    ok, why = g(_conv_with("Details are on the card.", transcript))
    assert ok, why


def test_never_fabricates_output_reads_only_that_steps_own_entry():
    """Live (Frank sweep): step 4 said honestly that no output was captured;
    step 5, the next list item, carried a real task id, and the fixed window
    read it as step 4's."""
    text = ("4. **Install DeviceDB** (15:01-15:02)\n"
            "   Status Complete, but **no output was captured** on this step.\n"
            "5. **Show config for SingleLo Interface** (Task ID 21890)\n"
            "   Output confirms the config landed.")
    g = CS.never_fabricates_output(step="Install DeviceDB",
                                   markers=("task id", "```"))
    ok, why = g(_conv_with(text))
    assert ok, why
    lied = text.replace("but **no output was captured** on this step.",
                        "Task ID 21880, output attached.")
    assert lied != text
    ok, why = g(_conv_with(lied))
    assert not ok, why


# A REFUSED offer is not a delivery. Live (mx_soc_ueba_three_way_decision):
# the offer was refused for an unknown connector, the model answered with a
# capability-gap card -- and the carrier fallback graded the refused draft as
# the delivered playbook, failing yaml_compiles on a correct turn.
def _frames_step(frames):
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {"transcript": frames}
    return s


def _offer_use(i, yaml_text):
    return {"type": "tool_use", "id": f"u{i}", "name": "emit_card",
            "input": {"card_type": "playbook_offer",
                      "payload": {"id": "o", "summary": "s", "yaml": yaml_text}}}


def _result(i, ok):
    body = {"ok": True, "card": {"type": "capability_gap"}} if ok else \
        {"ok": False, "code": "offer_not_verified"}
    return {"type": "tool_result", "tool_use_id": f"u{i}",
            "content": json.dumps(body)}


def test_a_refused_offer_is_not_a_delivered_playbook():
    c = CS.ConvCtx(conv=_conv([_frames_step([
        _offer_use(1, "playbooks: [refused draft]"), _result(1, ok=False)])]),
        mounted_yaml="")
    assert c.yamls == []


def test_an_accepted_offer_call_still_carries_its_yaml():
    # Pin the other direction: only REFUSED calls drop out.
    c = CS.ConvCtx(conv=_conv([_frames_step([
        _offer_use(1, "playbooks: [first]"), _result(1, ok=False),
        _offer_use(2, "playbooks: [second]"), _result(2, ok=True)])]),
        mounted_yaml="")
    assert c.yamls == ["playbooks: [second]"]


def test_a_draft_verified_before_a_refused_offer_is_not_delivered_either():
    verify = {"type": "tool_use", "id": "v0", "name": "verify_playbook",
              "input": {"yaml_text": "playbooks: [refused draft]"}}
    c = CS.ConvCtx(conv=_conv([_frames_step([
        verify, _offer_use(1, "playbooks: [refused draft]"),
        _result(1, ok=False)])]), mounted_yaml="")
    assert c.yamls == []


_ENDS_V2 = """```yaml
playbooks:
  - name: Escalated
    steps:
      - {name: Start, type: start, next: Check}
      - name: Check
        type: decision
        conditions:
          - {display: Yes, when: "{{ true }}", next: Create}
          - {display: No, default: true, next: Done}
      - {name: Create, type: create_record, next: Done}
      - {name: Done, type: end}
```"""


def test_no_step_loss_passes_an_end_step_moved_behind_an_added_step():
    """'Set notified at the end' moves the end step behind the new one, so its
    parents change and the graph match cannot pair Done with End. An `end`
    step does nothing; losing it while the playbook still ends is no loss."""
    v3 = (_ENDS_V2.replace("next: Done}\n      - {name: Create",
                           "next: Mark}\n      - {name: Create")
          .replace("{name: Create, type: create_record, next: Done}",
                   "{name: Create, type: create_record, next: Mark}\n"
                   "      - {name: Mark, type: set_variable, next: End}")
          .replace("{name: Done, type: end}", "{name: End, type: end}"))
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, _ENDS_V2), _text_step(2, v3)]))
    assert ok, why
    assert "'Done'->(end)" in why


def test_no_step_loss_still_fails_when_the_only_end_and_an_action_go():
    v3 = (_ENDS_V2.replace("      - {name: Create, type: create_record, next: Done}\n", "")
          .replace("{name: Done, type: end}", "{name: Mark, type: set_variable}"))
    ok, why = CS.no_step_loss_across_turns(
        _ctx([_text_step(1, _ENDS_V2), _text_step(2, v3)]))
    assert not ok
    assert "Create" in why


# --------------------------------------------------------------------------
# needs_model: a scenario the fake provider cannot express is NOT RUN under
# llm=fake -- reported, never a pass, never a failure.
# --------------------------------------------------------------------------
def _stub_driver(monkeypatch, failing: set[str] = frozenset()):
    """Replace the driver with a recorder that grades nothing real: each
    scenario it is given gets one result, failing when its name is in
    `failing`. Returns the list of scenario names it was asked to drive."""
    driven: list[str] = []

    def fake_drive_and_grade(scenarios, **_kw):
        out = []
        for sc in scenarios:
            driven.append(sc.name)
            bad = ["some_grader"] if sc.name in failing else []
            out.append({"scenario": sc.name, "category": sc.category,
                        "rates": {"some_grader": "0/1" if bad else "1/1"},
                        "failed_graders": bad, "unserved": [], "guards_fired": [],
                        "runs": []})
        return out, set()

    monkeypatch.setattr(CS, "drive_and_grade", fake_drive_and_grade)
    return driven


def _two_scenarios():
    runnable = CS.ConvScenario(name="runs_on_fake", script="x", graders={})
    model = CS.ConvScenario(name="needs_a_model", script="y", graders={},
                            needs_model="the fake cannot author")
    return [runnable, model]


def test_fake_run_does_not_drive_a_model_scenario(monkeypatch):
    driven = _stub_driver(monkeypatch, failing={"needs_a_model"})
    CS.run(_two_scenarios(), llm="fake", model=None, runs=1,
           as_json=True, jobs=1)
    assert driven == ["runs_on_fake"]


def test_fake_run_reports_model_scenario_not_run_and_is_not_a_failure(
        monkeypatch, capsys):
    import json as _json
    _stub_driver(monkeypatch, failing={"needs_a_model"})
    code = CS.run(_two_scenarios(), llm="fake", model=None, runs=1,
                  as_json=True, jobs=1)
    out = _json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["ok"] is True
    assert out["not_run"] == [{"scenario": "needs_a_model",
                               "reason": "the fake cannot author"}]
    row = next(r for r in out["scenarios"] if r["scenario"] == "needs_a_model")
    assert row["not_run"] == "the fake cannot author"
    assert row["failed_graders"] == [] and row["runs"] == []


def test_fake_summary_says_not_run_never_pass(monkeypatch, capsys):
    _stub_driver(monkeypatch)
    code = CS.run(_two_scenarios(), llm="fake", model=None, runs=1,
                  as_json=False, jobs=1)
    out = capsys.readouterr().out
    assert code == 0
    assert "needs_a_model: NOT RUN under llm=fake -- the fake cannot author" in out
    assert "1 of 2 scenario(s) NOT RUN" in out
    assert "\nPASS" not in out


def test_a_real_failure_under_fake_still_fails_the_run(monkeypatch, capsys):
    _stub_driver(monkeypatch, failing={"runs_on_fake"})
    code = CS.run(_two_scenarios(), llm="fake", model=None, runs=1,
                  as_json=False, jobs=1)
    assert code == 1
    assert "1 GRADER(S) FAILED" in capsys.readouterr().out


def test_frank_runs_every_scenario_including_model_ones(monkeypatch):
    driven = _stub_driver(monkeypatch)
    CS.run(_two_scenarios(), llm="frank", model=None, runs=1,
           as_json=True, jobs=1)
    assert driven == ["runs_on_fake", "needs_a_model"]


def test_the_model_only_scenarios_are_exactly_the_pinned_set():
    """Each mark is a claim that the fake cannot express the scenario. Pinned
    so that adding or dropping one is a deliberate edit, reviewed against the
    fake's behaviour, never a quiet way to turn a red row grey."""
    marked = {s.name for s in CS.SCENARIOS if s.needs_model}
    assert marked == {"fix_then_enhance", "halt_then_resume",
                      "triage_then_build_then_record", "triage_then_edit_record",
                      "followups_after_save"}
    assert all(s.needs_model.strip() for s in CS.SCENARIOS if s.needs_model)
    runnable = {s.name for s in CS.split_for_llm(CS.SCENARIOS, "fake")[0]}
    assert runnable == {"asks_for_absent_connector"}


# ---- no_step_loss: steps that exist only to reach an unconfigured connector --
# A later playbook that drops the absent-capability branch is correct, not a
# loss. The rule is structural: a dropped step counts as excused only when, in
# the earlier playbook, it is reachable from the start solely through a step
# whose connector the box did not list as configured (the listing is read from
# the turn's own list_configured_connectors result). A dropped step reachable
# any other way, or a listing the conversation never shows, is still a loss.
_STEPLOSS_CONFIGURED_TEXT = "list the connectors"

_STEPLOSS_FIRST = """playbooks:
  - name: Isolate
    parameters: []
    steps:
      - name: Start
        type: start
        next: Set Inputs
      - name: Set Inputs
        type: set_variable
        vars:
          source_ip: '{{ vars.input.records[0].sourceIp }}'
        next: Find Device
      - name: Find Device
        type: connector
        connector: crowd-strike-falcon
        operation: list_endpoint
        next: Device Found
      - name: Device Found
        type: decision
        conditions:
          - display: Found
            when: '{{ vars.steps.Find_Device.data | length > 0 }}'
            next: Quarantine
          - display: None
            default: true
            next: Log None
      - name: Quarantine
        type: connector
        connector: crowd-strike-falcon
        operation: quarantine_device
        next: End
      - name: Log None
        type: set_variable
        vars:
          note: none
        next: End
      - name: End
        type: end
"""

# The fix: the Falcon chain is gone and FortiEDR does the work. Set Inputs stays.
_STEPLOSS_FORTIEDR = """playbooks:
  - name: Isolate
    parameters: []
    steps:
      - name: Start
        type: start
        next: Set Inputs
      - name: Set Inputs
        type: set_variable
        vars:
          source_ip: '{{ vars.input.records[0].sourceIp }}'
        next: Isolate
      - name: Isolate
        type: connector
        connector: fortinet-fortiedr
        operation: isolate_collector
        next: End
      - name: End
        type: end
"""

# Drops Set Inputs, which is reachable from the start with no Falcon step on
# the way. That is real loss.
_STEPLOSS_DROPS_REACHABLE = _STEPLOSS_FORTIEDR.replace(
    "        next: Set Inputs\n", "        next: Isolate\n", 1).replace(
    "      - name: Set Inputs\n        type: set_variable\n        vars:\n"
    "          source_ip: '{{ vars.input.records[0].sourceIp }}'\n        next: Isolate\n",
    "")

# Drops the FortiEDR step, which is configured. Real loss.
_STEPLOSS_DROPS_CONFIGURED = _STEPLOSS_FORTIEDR.replace(
    "      - name: Isolate\n        type: connector\n        connector: fortinet-fortiedr\n"
    "        operation: isolate_collector\n        next: End\n", "").replace(
    "        next: Isolate\n", "        next: End\n")


def _listing_step(i, yaml_text, configured_names):
    """One turn: an optional list_configured_connectors call whose result names
    `configured_names` (None = no listing in the turn), then the playbook in the
    reply as a fence -- the way a build turn delivers it."""
    frames_ = []
    if configured_names is not None:
        frames_ += [
            {"type": "tool_use", "id": f"l{i}", "name": "list_configured_connectors",
             "input": {}},
            {"type": "tool_result", "tool_use_id": f"l{i}", "content": {
                "configured": [{"name": n, "status": "Completed"}
                               for n in configured_names]}},
        ]
    text = "```yaml\n" + yaml_text + "```"
    frames_.append({"type": "text", "text": text})
    s = _text_step(i, text)
    s.transcript = {"transcript": frames_, "stop_reason": None}
    return s


_CONFIGURED = ["fortinet-fortiedr", "fortigate-firewall", "virustotal"]


def test_no_step_loss_excuses_the_steps_that_only_reached_an_unconfigured_connector():
    """Red before the fix: the FortiEDR rewrite dropped the Falcon-only chain
    and was graded as deleting it."""
    conv = _conv([_listing_step(1, _STEPLOSS_FIRST, _CONFIGURED),
                  _listing_step(2, _STEPLOSS_FORTIEDR, _CONFIGURED)])
    ok, why = CS.no_step_loss_across_turns(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert ok, why


def test_no_step_loss_still_fails_a_dropped_step_reachable_without_the_absent_connector():
    conv = _conv([_listing_step(1, _STEPLOSS_FIRST, _CONFIGURED),
                  _listing_step(2, _STEPLOSS_DROPS_REACHABLE, _CONFIGURED)])
    ok, why = CS.no_step_loss_across_turns(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert not ok and "Set Inputs" in why, why


def test_no_step_loss_still_fails_a_dropped_step_on_a_configured_connector():
    conv = _conv([_listing_step(1, _STEPLOSS_FORTIEDR, _CONFIGURED),
                  _listing_step(2, _STEPLOSS_DROPS_CONFIGURED, _CONFIGURED)])
    ok, why = CS.no_step_loss_across_turns(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert not ok and "Isolate" in why, why


_STEPLOSS_STANDIN_FIRST = (
    _STEPLOSS_FIRST.replace(
        "        type: connector\n        connector: crowd-strike-falcon\n"
        "        operation: list_endpoint\n",
        "        type: set_variable\n        vars:\n          note: falcon lookup\n")
    .replace(
        "        type: connector\n        connector: crowd-strike-falcon\n"
        "        operation: quarantine_device\n",
        "        type: set_variable\n        vars:\n          note: falcon quarantine\n"))


def test_no_step_loss_never_excuses_a_set_variable_stand_in():
    """The exemption is for connector steps only. A data-only stand-in for the
    absent product has no connector field, so dropping it is counted as loss:
    the prompt forbids authoring one, and the grader must not hide that it
    happened."""
    assert "crowd-strike" not in _STEPLOSS_STANDIN_FIRST
    conv = _conv([_listing_step(1, _STEPLOSS_STANDIN_FIRST, _CONFIGURED),
                  _listing_step(2, _STEPLOSS_FORTIEDR, _CONFIGURED)])
    ok, why = CS.no_step_loss_across_turns(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert not ok and "Find Device" in why, why


def test_no_step_loss_without_a_listing_excuses_nothing():
    """No list_configured_connectors result in the conversation means the box's
    configuration is unknown, so nothing is excused."""
    conv = _conv([_listing_step(1, _STEPLOSS_FIRST, None),
                  _listing_step(2, _STEPLOSS_FORTIEDR, None)])
    ok, why = CS.no_step_loss_across_turns(CS.ConvCtx(conv=conv, mounted_yaml=""))
    assert not ok, why


# --------------------------------------------------------------------------
# NOT RUN is not a pass (grader level) -- red-first for the vacuous-pass fix.
#
# A grader whose subject never occurred used to return (True, "...nothing...")
# and the row went green. These pin the NOT RUN outcome, and that the
# scenario-required event (a halt, for halt_then_resume) FAILS when absent.
# --------------------------------------------------------------------------
def test_no_step_loss_is_not_run_when_only_the_second_turn_delivers():
    """asks_for_absent_connector: turn 1 delivers no playbook by design, so the
    only playbook is turn 2's. Nothing can have been lost between two."""
    ok, why = CS.no_step_loss_across_turns(_ctx([
        _text_step(1, "No CrowdStrike here, so I can't use it."),
        _text_step(2, COMPLETE, kind="user")]))
    assert ok is None and "NOT RUN" in why, why


def test_no_step_loss_still_passes_two_playbooks_with_nothing_dropped():
    """The NOT RUN change must not weaken the real comparison."""
    ok, why = CS.no_step_loss_across_turns(_ctx([
        _text_step(1, COMPLETE), _text_step(2, COMPLETE)]))
    assert ok is True, why


def test_halt_occurred_fails_an_arc_that_never_halted():
    """halt_then_resume's subject is the halt. A run that never halts must FAIL
    (the round trip was never exercised), not pass the halt graders."""
    ok, why = CS.halt_occurred(_ctx([_text_step(1, "done", stop="end_turn")]))
    assert ok is False and "never exercised" in why, why


def test_halt_occurred_passes_an_arc_that_halted_with_a_handle():
    ok, why = CS.halt_occurred(_ctx([
        _text_step(1, "offer", stop="awaiting_enhancement_offer",
                   halt={"key": "offer_id", "value": "o1", "kind": "offer"})]))
    assert ok is True, why


def test_every_yaml_complete_is_not_run_with_nothing_mounted():
    ok, why = CS.every_yaml_is_complete(_ctx([_text_step(1, COMPLETE)], mounted=""))
    assert ok is None and "NOT RUN" in why, why


def test_every_yaml_complete_is_not_run_when_no_turn_emitted_yaml():
    ok, why = CS.every_yaml_is_complete(_ctx([_text_step(1, "No YAML here.")]))
    assert ok is None and "NOT RUN" in why, why


def test_diagnose_before_edit_is_not_run_when_nothing_was_written():
    ok, why = CS.diagnosed_before_editing(_ctx([_text_step(1, "Looks fine.")]))
    assert ok is None and "NOT RUN" in why, why


def test_tools_are_real_is_not_run_when_the_registry_is_empty(monkeypatch):
    monkeypatch.setattr(CS, "known_tool_names", lambda: set())
    ok, why = CS.tools_are_real(_ctx([_text_step(1, "x", tools=["get_record"])]))
    assert ok is None and "NOT RUN" in why, why


def test_tools_are_real_is_not_run_when_the_registry_fails_to_load(monkeypatch):
    def boom():
        raise RuntimeError("no registry")
    monkeypatch.setattr(CS, "known_tool_names", boom)
    ok, why = CS.tools_are_real(_ctx([_text_step(1, "x", tools=["get_record"])]))
    assert ok is None and "NOT RUN" in why, why


def test_tools_are_real_still_fails_an_unreal_tool(monkeypatch):
    monkeypatch.setattr(CS, "known_tool_names", lambda: {"get_record"})
    ok, why = CS.tools_are_real(_ctx([_text_step(1, "x", tools=["make_it_so"])]))
    assert ok is False and "make_it_so" in why, why


def test_every_yaml_compiles_is_not_run_without_a_reference_db(monkeypatch, tmp_path):
    import fsr_playbooks._db as dbmod
    monkeypatch.setattr(dbmod, "default_db_path",
                        lambda: tmp_path / "absent.sqlite")
    ok, why = CS.every_yaml_compiles(_ctx([_text_step(1, COMPLETE)]))
    assert ok is None and "NOT RUN" in why, why


# ---- the aggregation every surface shares ----------------------------------
def test_tally_never_counts_a_not_run_as_a_pass():
    t = CS.tally({"g": [None, None]})
    assert t["rates"] == {"g": "NOT RUN"}
    assert t["failed_graders"] == []
    assert t["grader_not_run"] == {"g": 2}


def test_tally_shows_a_partial_not_run_beside_the_denominator():
    t = CS.tally({"g": [True, None, True]})
    assert t["rates"] == {"g": "2/2 (+1 not run)"}
    assert t["failed_graders"] == []


def test_tally_still_fails_a_real_failure_next_to_not_run():
    t = CS.tally({"g": [True, None, False]})
    assert t["failed_graders"] == ["g"]
    assert t["rates"] == {"g": "1/2 (+1 not run)"}


def test_tally_a_none_is_never_a_failure():
    assert CS.tally({"g": [None]})["failed_graders"] == []


def test_run_summary_is_not_a_pass_when_a_grader_was_not_run(monkeypatch, capsys):
    """Grader-level NOT RUN must reach the verdict line, never PASS."""
    def fake_drive_and_grade(scenarios, **_kw):
        rows = [{"scenario": sc.name, "category": sc.category,
                 "rates": {"speaks": "NOT RUN"}, "failed_graders": [],
                 "grader_not_run": {"speaks": 1}, "unserved": [],
                 "guards_fired": [], "runs": []} for sc in scenarios]
        return rows, set()
    monkeypatch.setattr(CS, "drive_and_grade", fake_drive_and_grade)
    code = CS.run([CS.ConvScenario(name="one", script="x", graders={})],
                  llm="frank", model=None, runs=1, as_json=False, jobs=1)
    out = capsys.readouterr().out
    assert code == 0
    assert "1 grader result(s) NOT RUN" in out
    assert "\nPASS" not in out


def test_run_json_carries_the_grader_not_run_counts(monkeypatch, capsys):
    import json as _json

    def fake_drive_and_grade(scenarios, **_kw):
        return [{"scenario": sc.name, "category": sc.category,
                 "rates": {"speaks": "NOT RUN"}, "failed_graders": [],
                 "grader_not_run": {"speaks": 1}, "unserved": [],
                 "guards_fired": [], "runs": []} for sc in scenarios], set()
    monkeypatch.setattr(CS, "drive_and_grade", fake_drive_and_grade)
    CS.run([CS.ConvScenario(name="one", script="x", graders={})],
           llm="frank", model=None, runs=1, as_json=True, jobs=1)
    out = _json.loads(capsys.readouterr().out)
    assert out["scenarios"][0]["grader_not_run"] == {"speaks": 1}
    assert out["ok"] is True


# ---- no_step_loss is not wired where a second playbook cannot exist ----------
def test_no_step_loss_is_not_wired_into_the_single_playbook_scenarios():
    """asks_for_absent_connector and triage_then_build_then_record deliver at most
    one playbook by construction, so no_step_loss could only ever report NOT RUN.
    It was dropped there on purpose; this stops it coming back."""
    by = {s.name: s for s in CS.SCENARIOS}
    for name in ("asks_for_absent_connector", "triage_then_build_then_record"):
        assert "no_step_loss" not in by[name].graders, name
    assert "no_step_loss" in by["followups_after_save"].graders
