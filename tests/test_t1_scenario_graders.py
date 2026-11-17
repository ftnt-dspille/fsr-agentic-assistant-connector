"""Unit tests for the T1 scenario graders (offline-testing plan §6e).

The graders are pure functions over a turn, so they are testable without a model
at all. That matters more than usual here: **a broken grader manufactures a
model defect**, and a false finding costs more than a missing one. So each
grader is pinned with a passing AND a failing case -- a grader that cannot fail
is not a grader.

The `DriveError` tests pin the harder-won lesson: the first version of this
suite reported a 403 as `emitted_yaml 0/1 -- no playbook authored`, i.e. it
graded a turn that never ran and blamed the model. Drive errors must abort, not
score.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import t1_scenarios as t1  # noqa: E402

_PB = """
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


def _ctx(*, said="", yaml="", tools=(), mounted=_PB) -> t1.Ctx:
    return t1.Ctx(ask="a", mounted_yaml=mounted, transcript=[], said=said,
                  yaml=yaml, tools=list(tools))


# --- returns_complete_playbook -------------------------------------------

def test_complete_playbook_passes_when_all_steps_kept():
    ok, _ = t1.returns_complete_playbook(_ctx(yaml=_PB.replace("ALPHA", "BRAVO")))
    assert ok


def test_complete_playbook_catches_a_destructive_fragment():
    """The failure that would silently delete an analyst's steps on Save."""
    fragment = ("collection: t\nvisible: true\nplaybooks:\n  - name: P\n"
                "    steps:\n      - name: Emit\n        type: create_record\n"
                "        arguments: {module: alerts, resource: {name: n}}\n")
    ok, detail = t1.returns_complete_playbook(_ctx(yaml=fragment))
    assert not ok and "Start" in detail


def test_complete_playbook_does_not_mistake_the_playbook_name_for_a_step():
    """A `- name:` regex also matches the PLAYBOOK's name; reporting that as a
    dropped step gets the grader disbelieved."""
    ok, detail = t1.returns_complete_playbook(_ctx(yaml=_PB))
    assert ok, detail
    assert "P" not in t1._step_names(_PB)


# --- changed_only_what_was_asked -----------------------------------------

def test_diff_grader_requires_the_change_and_forbids_the_stale_value():
    g = t1.changed_only_what_was_asked(expect="BRAVO", forbid=("ALPHA",))
    assert g(_ctx(yaml=_PB.replace("ALPHA", "BRAVO")))[0]
    # change absent
    assert not g(_ctx(yaml=_PB))[0]
    # change present but stale value left behind → a half-applied edit
    assert not g(_ctx(yaml=_PB + "\n# BRAVO"))[0]


# --- never_asks_for_the_playbook -----------------------------------------

@pytest.mark.parametrize("said", [
    "Could you paste the playbook YAML here?",
    "Please provide the playbook definition so I can help.",
    "Can you share the yaml content?",
])
def test_begging_for_the_playbook_is_caught(said):
    assert not t1.never_asks_for_the_playbook(_ctx(said=said))[0]


@pytest.mark.parametrize("said", [
    "The playbook creates an alert. Here is the revised YAML.",
    "I can see the Emit step sets description: ALPHA.",
])
def test_normal_prose_is_not_flagged(said):
    assert t1.never_asks_for_the_playbook(_ctx(said=said))[0]


# --- ordering -------------------------------------------------------------

def test_called_before_requires_presence_and_order():
    g = t1.called_before("why_did_playbook_fail", "verify_enhancement")
    assert g(_ctx(tools=["why_did_playbook_fail", "verify_enhancement"]))[0]
    # S6 run 3: only verify_enhancement, diagnose lever never fired
    assert not g(_ctx(tools=["verify_enhancement"]))[0]
    # diagnosing AFTER fixing does not satisfy "diagnose before fix"
    assert not g(_ctx(tools=["verify_enhancement", "why_did_playbook_fail"]))[0]


# --- emitted_yaml / compiles ---------------------------------------------

def test_emitted_yaml_fails_on_a_clarifying_question():
    ok, detail = t1.emitted_yaml(_ctx(said="Which module should it target?"))
    assert not ok and "question" in detail


def test_yaml_compiles_discriminates():
    assert t1.yaml_compiles(_ctx(yaml=_PB))[0]
    bad = _PB.replace("resource:", "record:")  # the observed `record:` slip
    assert not t1.yaml_compiles(_ctx(yaml=bad))[0]


# --- drive-error guard ----------------------------------------------------

def test_drive_error_raised_on_failed_envelope():
    with pytest.raises(t1.DriveError):
        t1._raise_on_drive_error(
            {"ok": False, "error": "The OpenAI API key lacks permission",
             "transcript": []})


def test_drive_error_raised_on_error_frame():
    with pytest.raises(t1.DriveError):
        t1._raise_on_drive_error(
            {"ok": True, "transcript": [{"type": "error", "message": "403"}]})


@pytest.mark.parametrize("stop", ["max_turns", "max_tokens", "length",
                                  "max_tool_turns"])
def test_drive_error_raised_on_a_truncated_turn(stop):
    """A cap is the same class as a 403 (§6g.2 F3). Two build turns ended
    mid-word with no YAML; grading that as `emitted_yaml 0/1` would score the
    harness's token budget as the model refusing to author."""
    with pytest.raises(t1.DriveError, match="TRUNCATED"):
        t1._raise_on_drive_error(
            {"ok": True, "stop_reason": stop,
             "transcript": [{"type": "text", "text": "…before the fallback. Let"}]})


def test_healthy_envelope_does_not_raise():
    t1._raise_on_drive_error(
        {"ok": True, "stop_reason": "end_turn",
         "transcript": [{"type": "text", "text": "hi"}]})


# =========================================================================
# Real-playbook graders (loops, branches, grounded explanations, read-only)
# =========================================================================

_LOOPY = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - name: Start
        type: start_on_create
        module: incidents
        next: Find
      - name: Find
        type: find_record
        module: incidents
        arguments: {query: {logic: AND, filters: []}}
        next: Each
      - name: Each
        type: set_variable
        for_each:
          item: "{{ vars.steps.Find }}"
          condition: ""
        vars: {x: "{{ vars.item.id }}"}
"""

_BRANCHY = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - name: Start
        type: start_on_create
        module: alerts
        next: Pick
      - name: Pick
        type: decision
        conditions:
          - {display: High, when: "{{ vars.s > 7 }}", next: Up}
          - {display: Else, default: true, next: Down}
      - name: Up
        type: set_variable
        vars: {a: "1"}
      - name: Down
        type: set_variable
        vars: {a: "2"}
"""


# --- preserves_loops ------------------------------------------------------

def test_preserves_loops_passes_when_the_loop_survives():
    ok, _ = t1.preserves_loops(_ctx(yaml=_LOOPY, mounted=_LOOPY))
    assert ok


def test_preserves_loops_catches_a_dropped_for_each():
    """The step survives, stops iterating. Compiles, runs, does 1/N the work."""
    stripped = _LOOPY.replace(
        '        for_each:\n          item: "{{ vars.steps.Find }}"\n'
        '          condition: ""\n', "")
    ok, detail = t1.preserves_loops(_ctx(yaml=stripped, mounted=_LOOPY))
    assert not ok and "Each" in detail


def test_preserves_loops_catches_a_retargeted_loop():
    """Keeping `for_each` but iterating something else is the same bug wearing
    a disguise -- a dict-key diff would call this lossless."""
    moved = _LOOPY.replace("{{ vars.steps.Find }}", "{{ vars.something_else }}")
    ok, detail = t1.preserves_loops(_ctx(yaml=moved, mounted=_LOOPY))
    assert not ok and "iterates over something else" in detail


def test_preserves_loops_is_not_run_without_a_loop():
    ok, detail = t1.preserves_loops(_ctx(yaml=_PB, mounted=_PB))
    assert ok is None and detail.startswith("NOT RUN"), detail


# --- preserves_branches ---------------------------------------------------

def test_preserves_branches_passes_on_an_untouched_decision():
    ok, _ = t1.preserves_branches()(_ctx(yaml=_BRANCHY, mounted=_BRANCHY))
    assert ok


def test_preserves_branches_catches_a_dropped_arm():
    thinned = _BRANCHY.replace(
        '          - {display: Else, default: true, next: Down}\n', "")
    ok, detail = t1.preserves_branches()(_ctx(yaml=thinned, mounted=_BRANCHY))
    assert not ok and "dropped" in detail


def test_preserves_branches_catches_a_removed_decision_step():
    ok, detail = t1.preserves_branches()(
        _ctx(yaml=_BRANCHY.replace("type: decision", "type: set_variable"),
             mounted=_BRANCHY))
    assert not ok and "Pick" in detail


def test_preserves_branches_flags_an_unrequested_new_arm_but_allows_one_asked_for():
    """Same input, opposite verdicts -- this is what `allow_new` is FOR, and a
    flag whose two settings behave identically is a flag that does nothing."""
    grown = _BRANCHY.replace(
        '          - {display: Else, default: true, next: Down}\n',
        '          - {display: Mid, when: "{{ vars.s > 3 }}", next: Down}\n'
        '          - {display: Else, default: true, next: Down}\n')
    strict, _ = t1.preserves_branches()(_ctx(yaml=grown, mounted=_BRANCHY))
    lenient, _ = t1.preserves_branches(allow_new=True)(
        _ctx(yaml=grown, mounted=_BRANCHY))
    assert not strict and lenient


# --- grounded_in_real_steps ----------------------------------------------

def test_grounded_passes_when_every_named_step_exists():
    mounted = _PB.replace("name: Emit", "name: Emit Alert")
    ok, _ = t1.grounded_in_real_steps(
        _ctx(said="`Start` fires, then the `Emit Alert` step creates the alert.",
             mounted=mounted))
    assert ok


def test_grounded_catches_an_invented_step():
    ok, detail = t1.grounded_in_real_steps(
        _ctx(said="It first runs the `Enrich Indicator` step.", mounted=_LOOPY))
    assert not ok and "Enrich Indicator" in detail


def test_grounded_ignores_backticked_jinja_and_single_words():
    """A grader that cries wolf gets ignored, so plain prose, Jinja and bare
    identifiers must not read as invented step names."""
    ok, _ = t1.grounded_in_real_steps(
        _ctx(said="It reads `{{ vars.item.id }}` and the `incidents` module, "
                  "then updates each record it finds.", mounted=_LOOPY))
    assert ok


_REAL_ISH = """
collection: 04 - Actions
visible: true
playbooks:
  - name: Incident 04 - Check for SLA violations
    steps:
      - name: Start
        type: start
        module: threat_intel_feeds
        arguments: {button_label: Block File Hash}
        next: Select SLA
      - name: Select SLA
        type: decision
        conditions:
          - {display: Do Nothing, default: true, next: Do Nothing}
          - {display: Return SLA, when: "{{ vars.x }}", next: Do Nothing}
      - name: Do Nothing
        type: set_variable
        arguments: {status: Awaiting Action}
"""


@pytest.mark.parametrize("said", [
    "The status is set to `Awaiting Action`.",             # picklist value
    'It allows "and hyphens" in the name.',                # prose in quotes
    "`Incident 04 Check for SLA violations` runs nightly.",  # playbook's own name
    "The `Return SLA` branch is taken when the list is non-empty.",  # branch label
    "The button is labelled `Block File Hash`.",           # trigger button_label
    "It skips values `which are outside the allowed` range.",  # backticked prose
])
def test_grounded_does_not_cry_wolf_on_real_references(said):
    """§6g.2 F1 -- the five things the first version wrongly called invented
    steps. Every one is legitimately present in the mounted playbook, and a
    false 'the model confabulated' costs more than a miss."""
    ok, detail = t1.grounded_in_real_steps(_ctx(said=said, mounted=_REAL_ISH))
    assert ok, detail


def test_grounded_still_catches_a_name_nowhere_in_the_playbook():
    ok, detail = t1.grounded_in_real_steps(
        _ctx(said="Then the `Quarantine Endpoint` step fires.", mounted=_REAL_ISH))
    assert not ok and "Quarantine Endpoint" in detail


# --- emitted_no_yaml ------------------------------------------------------

def test_no_yaml_passes_on_a_prose_answer():
    ok, _ = t1.emitted_no_yaml(_ctx(said="It runs once per incident."))
    assert ok


def test_no_yaml_catches_an_unasked_for_rewrite():
    """On a read-only ask an unrequested fence is an unrequested WRITE -- the
    widget saves the last fence over the open record."""
    ok, detail = t1.emitted_no_yaml(_ctx(said="Here you go", yaml=_PB))
    assert not ok and "read-only" in detail


# --- replaces_step --------------------------------------------------------
#
# `automate_manual_step` asks "Replace the No Operation step…", so that step is
# SUPPOSED to disappear. Graded with the blanket `returns_complete_playbook`
# bar it scored 0/6 as "DESTRUCTIVE: dropped step(s) ['No Operation']" -- a
# correct answer marked as the worst possible failure. Grader defect, not a
# model one. These pin the replacement bar so it can never regress to either
# extreme: blind to real step loss, or hostile to the replacement it asked for.

_REPL_MOUNTED = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - name: Start
        type: start
        next: Manual Task
        arguments: {module: alerts, button_label: Run}
      - name: Manual Task
        type: manual_input
        next: No Operation
        arguments: {module: alerts}
      - name: No Operation
        type: no_op
        arguments: {}
"""

_REPL_GOOD = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - name: Start
        type: start
        next: Manual Task
        arguments: {module: alerts, button_label: Run}
      - name: Manual Task
        type: manual_input
        next: Block IPv4
        arguments: {module: alerts}
      - name: Block IPv4
        type: connector
        arguments: {connector: fortigate-firewall, operation: block_ip}
"""


def _repl_ctx(y):
    return t1.Ctx(ask="a", mounted_yaml=_REPL_MOUNTED, transcript=[], said="",
                  yaml=y, tools=[])


def test_replaces_step_passes_when_the_no_op_is_genuinely_replaced():
    ok, why = t1.replaces_step("No Operation", wired_from="Manual Task")(
        _repl_ctx(_REPL_GOOD))
    assert ok, why


def test_replaces_step_rejects_a_playbook_that_still_has_the_no_op():
    ok, why = t1.replaces_step("No Operation")(_repl_ctx(_REPL_MOUNTED))
    assert not ok and "still present" in why


def test_replaces_step_still_catches_OTHER_dropped_steps():
    """The destructive bar must survive intact for every step but the target."""
    thinned = _REPL_GOOD.replace(
        "      - name: Manual Task\n"
        "        type: manual_input\n"
        "        next: Block IPv4\n"
        "        arguments: {module: alerts}\n", "")
    ok, why = t1.replaces_step("No Operation")(_repl_ctx(thinned))
    assert not ok and "DESTRUCTIVE" in why and "Manual Task" in why


def test_replaces_step_rejects_a_bare_deletion_with_no_replacement():
    deleted = _REPL_MOUNTED.replace(
        "      - name: No Operation\n        type: no_op\n        arguments: {}\n",
        "").replace("next: No Operation", "next: Start")
    ok, why = t1.replaces_step("No Operation")(_repl_ctx(deleted))
    assert not ok and "nothing replaced it" in why


def test_replaces_step_catches_a_replacement_left_beside_the_old_wiring():
    """The scenario's actual trap: the branch still routes to the no-op."""
    dangling = _REPL_GOOD.replace("next: Block IPv4", "next: No Operation")
    ok, why = t1.replaces_step("No Operation")(_repl_ctx(dangling))
    assert not ok and "still references" in why


# ── investigation graders ─────────────────────────────────────────────────
#
# These grade a triage turn against the REAL mounted record. Both fooled the
# author once during development -- no_invented_ips must not flag a masked
# address, and grounded_in_record must not demand a whole field value quoted
# verbatim -- so pin both failure directions here. A disbelieved grader is
# worse than none.

# A real C2-exfil alert's discriminating fields (subset of the shipped fixture).
_ALERT = {
    "@id": "/api/3/alerts/d39ecc9a", "uuid": "d39ecc9a", "id": 7,
    "name": "Traffic from 10.50.60.70 to FortiGuard Malware IP 102.220.160.21",
    "ruleName": "Traffic to FortiGuard Malware IP List",
    "reportingDeviceIP": "10.10.100.1",
}


def _tctx(said, *, record=_ALERT, tools=(), results=()):
    frames = [{"type": "tool_use", "name": t} for t in tools]
    frames += [{"type": "tool_result", "display": r} for r in results]
    return t1.Ctx(ask="what happened?", mounted_yaml="", transcript=frames,
                  said=said, yaml="", tools=list(tools), record=record)


def test_no_invented_ips_flags_an_address_absent_from_all_evidence():
    ok, why = t1.no_invented_ips(_tctx(
        "The host 10.50.60.70 beaconed to 203.0.113.9, a known C2 node."))
    assert not ok and "203.0.113.9" in why


def test_no_invented_ips_passes_addresses_that_are_on_the_record():
    ok, why = t1.no_invented_ips(_tctx(
        "Internal host 10.50.60.70 sent traffic to 102.220.160.21."))
    assert ok, why


def test_no_invented_ips_passes_an_address_that_came_from_a_tool_result():
    ok, why = t1.no_invented_ips(
        _tctx("Enrichment flagged 45.9.148.3 as malicious.",
              results=['{"ip": "45.9.148.3", "verdict": "malicious"}']))
    assert ok, why


def test_grounded_in_record_rejects_a_generic_paragraph():
    ok, why = t1.grounded_in_record(need=2)(_tctx(
        "This alert indicates suspicious network activity that should be "
        "reviewed by an analyst and escalated if warranted."))
    assert not ok, why


def test_grounded_in_record_accepts_tokens_without_a_verbatim_quote():
    """The failure the author hit: a correct answer that never quotes the
    whole field value must still count as grounded."""
    ok, why = t1.grounded_in_record(need=1)(_tctx(
        "The FortiGuard Malware IP List rule fired on the reporting device "
        "at 10.10.100.1 -- this is C2 traffic."))
    assert ok, why


def test_honest_about_failed_lookups_flags_unsupported_correlation():
    ok, why = t1.honest_about_failed_lookups(_tctx(
        "I found three related alerts on the same host in the last day."))
    assert not ok and "correlation" in why


def test_honest_about_failed_lookups_passes_when_the_gap_is_admitted():
    ok, why = t1.honest_about_failed_lookups(_tctx(
        "I could not search for related alerts -- the SIEM lookup returned "
        "nothing, so I can only speak to this record."))
    assert ok, why


def test_investigated_before_concluding_flags_a_no_tool_answer():
    ok, why = t1.investigated_before_concluding(_tctx("It's malicious.", tools=()))
    assert not ok and "NO tool" in why


def test_investigated_before_concluding_passes_when_a_tool_ran():
    ok, _ = t1.investigated_before_concluding(
        _tctx("Enriched the IP.", tools=("find_enrichment_actions",)))
    assert ok


def test_no_tool_matching_blocks_a_containment_call():
    g = t1.no_tool_matching(r"block|isolate|quarantine", why="deferred")
    ok, why = g(_tctx("Blocking now.", tools=("block_ip",)))
    assert not ok and "block_ip" in why


def test_no_tool_matching_allows_unrelated_tools():
    g = t1.no_tool_matching(r"block|isolate|quarantine", why="deferred")
    ok, _ = g(_tctx("Just looking.", tools=("get_record", "find_enrichment_actions")))
    assert ok


def test_reaches_a_verdict_requires_a_disposition():
    assert not t1.reaches_a_verdict(_tctx(
        "Here is a summary of the fields on this alert record for your review."))[0]


def test_reaches_a_verdict_is_structural_prose_alone_is_not_a_verdict():
    """Pinned the other way on purpose: prose that says 'true positive' with no
    verdict card is NOT a disposition the analyst can act on. The old check
    passed it on the word."""
    ok, why = t1.reaches_a_verdict(_tctx(
        "This is a true positive -- recommend you isolate the host."))
    assert not ok and "no verdict card" in why, why


# -- replaces_step grades structure, not the name --
#
# Real playbook shape: connector/operation at the top level, and the no-op is
# `cyops_utilities` / `no_op`. The old grader required the name to vanish, and
# scored the correct in-place replacement as a failure.

_REAL_MOUNTED = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - type: start
        name: Start
        next: Manual Task
      - type: manual_input
        name: Manual Task
        options:
          - display: No Operation
            next: no_operation
      - type: connector
        name: No Operation
        params: []
        connector: cyops_utilities
        operation: no_op
"""

_REAL_IN_PLACE = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - type: start
        name: Start
        next: Manual Task
      - type: manual_input
        name: Manual Task
        options:
          - display: Block
            next: no_operation
      - type: connector
        name: No Operation
        params: {method: Quarantine Based, ip_addresses: "{{ vars.input.params.ip4AddressList }}"}
        connector: fortigate-firewall
        operation: block_ip_new
"""

_REAL_NEW_NAME_RIGHT = _REAL_IN_PLACE.replace(
    "      - type: connector\n        name: No Operation",
    "      - type: connector\n        name: Block IPv4 Addresses").replace(
    "next: no_operation", "next: block_ipv4_addresses")

_REAL_NEW_NAME_STALE = _REAL_IN_PLACE.replace(
    "      - type: connector\n        name: No Operation",
    "      - type: connector\n        name: Block IPv4 Addresses")


def _real_ctx(y):
    return t1.Ctx(ask="a", mounted_yaml=_REAL_MOUNTED, transcript=[], said="",
                  yaml=y, tools=[])


_BLOCK = dict(wired_from="Manual Task", action_op="block")


def test_replaces_step_accepts_an_in_place_replacement_under_the_same_name():
    """The diag evidence: the model turned 'No Operation' into a block_ip_new
    connector step, kept the name, and the old grader called it a failure."""
    ok, why = t1.replaces_step("No Operation", wired_from="Manual Task")(
        _real_ctx(_REAL_IN_PLACE))
    assert ok, why


def test_replaces_step_pins_the_requested_action_on_an_in_place_replacement():
    ok, why = t1.replaces_step("No Operation", **_BLOCK)(_real_ctx(_REAL_IN_PLACE))
    assert ok, why


def test_replaces_step_control_rejects_the_no_op_left_in_place():
    """Control: the no-op untouched, same name, must still fail. This one is
    expected green on main as well: it guards against over-correcting."""
    ok, why = t1.replaces_step("No Operation", wired_from="Manual Task")(
        _real_ctx(_REAL_MOUNTED))
    assert not ok and "still present" in why, why


def test_replaces_step_rejects_an_in_place_step_that_runs_the_wrong_operation():
    wrong = _REAL_IN_PLACE.replace("operation: block_ip_new",
                                   "operation: get_quarantine_hosts")
    ok, why = t1.replaces_step("No Operation", **_BLOCK)(_real_ctx(wrong))
    assert not ok and "block" in why, why


def test_replaces_step_accepts_a_new_name_wired_in_by_its_new_id():
    ok, why = t1.replaces_step("No Operation", **_BLOCK)(_real_ctx(_REAL_NEW_NAME_RIGHT))
    assert ok, why


def test_replaces_step_rejects_a_new_name_still_routed_to_by_the_old_id():
    """The trap, on the real shape: the manual step still routes to no_operation."""
    ok, why = t1.replaces_step("No Operation", **_BLOCK)(_real_ctx(_REAL_NEW_NAME_STALE))
    assert not ok and "still references" in why, why


# -- triage graders read what the analyst reads --

def _card_ctx(said, *cards, record=None):
    return t1.Ctx(ask="what happened?", mounted_yaml="", transcript=list(cards),
                  said=said, yaml="", tools=["get_record"],
                  record=_ALERT if record is None else record)


def _verdict(summary, *, disposition="true_positive", claims=()):
    return {"type": "verdict_card", "disposition": disposition, "summary": summary,
            "findings": [{"claim": c} for c in claims]}


def test_shown_is_the_prose_alone_when_no_card_was_emitted():
    assert _card_ctx("Just prose.").shown == "Just prose."


def test_shown_includes_every_card_string_but_not_the_card_type():
    c = _card_ctx("Preamble.", _verdict("Summary text.", claims=("A claim.",)))
    assert "Preamble." in c.shown and "Summary text." in c.shown
    assert "A claim." in c.shown and "verdict_card" not in c.shown


def test_grounded_in_record_reads_the_answer_when_it_is_in_the_verdict_card():
    """The flake: a two-line preamble, the grounded answer in the verdict card.
    Graded on prose alone it read as generic."""
    c = _card_ctx("I'll pull the corroborating evidence first.",
                  _verdict(_ALERT["name"] + ", which matches the rule "
                           + _ALERT["ruleName"] + "."))
    ok, why = t1.grounded_in_record(need=2)(c)
    assert ok, why


def test_no_invented_ips_catches_an_address_invented_only_in_the_card():
    """An address the analyst reads in the card is still a claim. The prose-only
    check missed it."""
    c = _card_ctx("The affected host was active.",
                  _verdict("It beaconed to 203.0.113.9, a known C2 node."))
    ok, why = t1.no_invented_ips(c)
    assert not ok and "203.0.113.9" in why, why


def test_reaches_a_verdict_passes_a_verdict_card_with_a_disposition():
    ok, why = t1.reaches_a_verdict(_card_ctx("Short preamble.", _verdict("Done.")))
    assert ok and "true_positive" in why, why


def test_reaches_a_verdict_fails_a_card_with_no_disposition():
    """The prose here uses disposition words; the card carries none. Only the
    card counts, so this must fail."""
    ok, why = t1.reaches_a_verdict(_card_ctx(
        "This looks like a true positive, so I recommend isolating the host.",
        _verdict("Done.", disposition="")))
    assert not ok and "no disposition" in why, why


def test_honest_about_failed_lookups_passes_a_negated_lookup():
    """Observed: 'found no other matching alerts or incidents' is a denial. The
    unnegated pattern flagged it as a correlation claim."""
    ok, why = t1.honest_about_failed_lookups(_card_ctx(
        "I found no other matching alerts or incidents for this host."))
    assert ok, why


def test_honest_about_failed_lookups_still_flags_a_positive_claim_after_a_denial():
    """Polarity is per sentence: a denial earlier does not excuse a claim later."""
    ok, why = t1.honest_about_failed_lookups(_card_ctx(
        "I found no sibling incidents in the SIEM. I found three related alerts "
        "on the same host."))
    assert not ok and "correlation" in why, why


# -- NOT RUN in t1 (the vacuous-pass shape, same contract as conv) ----------

_NO_LOOP_MOUNTED = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - type: start
        name: Start
        next: Set Conditions
      - type: set_variable
        name: Set Conditions
        vars: {x: "1"}
"""


def test_preserves_loops_is_not_run_when_the_playbook_has_no_loop():
    """Was (True, "mounted playbook has no loops"): a vacuous pass for a grader
    whose subject is absent. NOT RUN is the honest outcome."""
    c = t1.Ctx(ask="a", mounted_yaml=_NO_LOOP_MOUNTED, transcript=[], said="",
               yaml=_NO_LOOP_MOUNTED, tools=[])
    ok, why = t1.preserves_loops(c)
    assert ok is None and why.startswith("NOT RUN"), why


def test_preserves_branches_is_not_run_when_the_playbook_has_no_decision():
    c = t1.Ctx(ask="a", mounted_yaml=_NO_LOOP_MOUNTED, transcript=[], said="",
               yaml=_NO_LOOP_MOUNTED, tools=[])
    ok, why = t1.preserves_branches(allow_new=True)(c)
    assert ok is None and why.startswith("NOT RUN"), why


def test_resolves_containment_is_not_run_when_no_containment_was_attempted():
    """A read-only turn that never tried containment did not exercise the
    resolution path. (True, "staged without a catalog hunt") was a vacuous pass."""
    c = t1.Ctx(ask="a", mounted_yaml="", transcript=[], said="",
               yaml="", tools=["get_record"])
    ok, why = t1.resolves_containment_directly(c)
    assert ok is None and why.startswith("NOT RUN"), why


def test_resolves_containment_still_passes_a_direct_stage_with_no_hunt():
    """Control: a card staged without the catalog hunt is still the good path."""
    c = t1.Ctx(ask="a", mounted_yaml="", transcript=[], said="",
               yaml="", tools=["emit_action_card"])
    ok, why = t1.resolves_containment_directly(c)
    assert ok is True, why


# -- grounded_in_real_steps: a connector + operation pair is a real reference --

_PAIR_MOUNTED = """
collection: t
visible: true
playbooks:
  - name: P
    steps:
      - type: start
        name: Start
        next: No Op Step
      - type: connector
        name: No Op Step
        params: []
        connector: cyops_utilities
        operation: no_op
"""


def test_grounded_steps_accepts_a_connector_operation_pair_named_in_prose():
    """Observed in a capture: the model named the step as `cyops_utilities no_op`,
    which is exactly what the step runs. The flat haystack never held that pair."""
    c = t1.Ctx(ask="a", mounted_yaml=_PAIR_MOUNTED, transcript=[],
               said="The 'No Op Step' runs `cyops_utilities no_op` when empty.",
               yaml="", tools=["analyze_playbook"])
    ok, why = t1.grounded_in_real_steps(c)
    assert ok, why


def test_grounded_steps_still_rejects_an_invented_connector_operation_pair():
    c = t1.Ctx(ask="a", mounted_yaml=_PAIR_MOUNTED, transcript=[],
               said="The step runs `fortinet block_ip` on the firewall.",
               yaml="", tools=["analyze_playbook"])
    ok, why = t1.grounded_in_real_steps(c)
    assert not ok and "fortinet" in why, why


# -- the run verdict: NOT RUN is never PASS (monkeypatched drive, no model) --
# run_one is stubbed, so no provider is called. llm="frank" keeps these about
# the verdict only: under "fake", NEEDS_MODEL graders are NOT RUN by design.

def _stub_run_one(monkeypatch, outcome):
    def fake(sc, *, llm, model):
        return {g: outcome(g) for g in sc.graders}
    monkeypatch.setattr(t1, "run_one", fake)


def test_run_verdict_is_not_run_not_pass_when_a_grader_was_not_run(monkeypatch, capsys):
    name = t1.SCENARIOS[0].name
    g = next(iter(t1.BY_NAME[name].graders))
    _stub_run_one(monkeypatch, lambda gg: (None, "NOT RUN: nothing to grade")
                  if gg == g else (True, "ok"))
    code = t1.run([name], runs=1, llm="frank", model=None, jobs=1)
    out = capsys.readouterr().out
    assert code == 0
    assert f"NOT RUN {g}" in out or "(+1 not run)" in out
    assert "NO FAILURES, but 1 grader result(s) NOT RUN" in out
    assert "\nPASS" not in out


def test_run_verdict_is_pass_when_every_grader_passes(monkeypatch, capsys):
    """Control: the verdict line says PASS only when nothing was not run."""
    _stub_run_one(monkeypatch, lambda g: (True, "ok"))
    name = t1.SCENARIOS[0].name
    code = t1.run([name], runs=1, llm="frank", model=None, jobs=1)
    out = capsys.readouterr().out
    assert code == 0 and "\nPASS" in out


def test_run_verdict_is_failed_when_a_grader_fails(monkeypatch, capsys):
    _stub_run_one(monkeypatch, lambda g: (False, "nope"))
    name = t1.SCENARIOS[0].name
    code = t1.run([name], runs=1, llm="frank", model=None, jobs=1)
    out = capsys.readouterr().out
    assert code == 1 and "GRADER(S) FAILED" in out


def test_dump_turn_keeps_the_frames_and_every_verdict(tmp_path):
    """A rare failure is only explainable from the turn that produced it. The dump
    must carry the frames, the delivered YAML and each verdict, including NOT RUN."""
    import json
    sc = t1.BY_NAME[t1.SCENARIOS[0].name]
    frames = [{"type": "text", "text": "Explained."},
              {"type": "tool_use", "name": "analyze_playbook", "input": {}}]
    ctx = t1.Ctx(ask=sc.ask, mounted_yaml="", transcript=frames, said="Explained.",
                 yaml="", tools=["analyze_playbook"])
    path = t1.dump_turn(str(tmp_path), sc, ctx,
                        {"no_yaml_written": (True, "ok"),
                         "grounded_steps": (None, "NOT RUN: x")})
    body = json.loads(open(path, encoding="utf-8").read())
    assert body["scenario"] == sc.name and body["tools"] == ["analyze_playbook"]
    assert body["frames"] == frames
    assert body["grades"]["grounded_steps"] == {"ok": None, "detail": "NOT RUN: x"}


# --- grounded_in_record: structural identifiers, no percentage of wording ---

_INCIDENT = json.loads((t1.TRIAGE_FIXTURES / "incident_smithdesktop.json").read_text())


def test_grounded_in_record_passes_the_captured_host_level_answer():
    """Red-first on the captured investigate_incident_scope turn. The answer
    names the affected host and isolates it. The old 60%-token rule read the
    record's name and description word-by-word, and graded this correct answer
    0 of 2 values."""
    said = ("Smithdesktop is the affected host. Isolate smithDesktop at the "
            "network edge, then preserve the host for forensics before "
            "re-imaging.")
    ok, why = t1.grounded_in_record(need=1)(_tctx(said, record=_INCIDENT))
    assert ok, why


def test_grounded_in_record_fails_an_answer_that_names_nothing_from_the_record():
    """Control: no identifier from this record appears, so it fails. The
    generic answer must not pass just because it reads sensibly."""
    ok, why = t1.grounded_in_record(need=1)(_tctx(
        "The affected host needs a closer look before anyone acts on it, and "
        "the next step is to check the endpoint's recent activity.",
        record=_INCIDENT))
    assert not ok and "names none" in why, why


def test_record_identifiers_read_the_record_structure():
    ids = t1.record_identifiers(_INCIDENT)
    assert "smithdesktop" in ids
    assert _INCIDENT["uuid"].lower() in ids
    assert "detected" not in ids and "activities" not in ids
    assert not any(t1._DATE_LIKE.match(i) for i in ids), "dates are not identifiers"


def test_record_identifiers_include_a_lowercase_identity_field():
    """A user name is usually lowercase, so the shape rule alone misses it. It
    counts because its field is identity-typed (affectedUser), not because of
    what the answer says."""
    rec = {"name": "Odd login", "affectedUser": "jdoe"}
    assert "jdoe" in t1.record_identifiers(rec)


def test_grounded_in_record_does_not_match_inside_a_longer_address():
    """Whole-token matching: a record address 10.50.60.7 is not cited by an
    answer that says 10.50.60.70 (a different host)."""
    rec = {"name": "Odd", "srcip": "10.50.60.7"}
    ok, why = t1.grounded_in_record(need=1)(_tctx(
        "Traffic went to 10.50.60.70 only.", record=rec))
    assert not ok, why


def test_grounded_in_record_is_not_run_without_a_record():
    ok, why = t1.grounded_in_record(need=1)(_tctx("Smithdesktop is affected.", record={}))
    assert ok is None and why.startswith("NOT RUN")


# --- what the fake provider can and cannot be graded on ---------------------

def test_every_needs_model_name_is_a_real_grader():
    """NEEDS_MODEL is keyed by grader name; a renamed grader must not leave a
    dead entry behind (or a model grader silently graded on the fake)."""
    used = {g for sc in t1.SCENARIOS for g in sc.graders}
    assert set(t1.NEEDS_MODEL) <= used, sorted(set(t1.NEEDS_MODEL) - used)


def test_fake_reports_model_graders_not_run_and_keeps_structural_ones():
    grades = {"complete_playbook": (False, "no playbook"),
              "gate_held": (True, "no write reached the box"),
              "no_invented_ips": (False, "invented 10.0.0.9")}
    out = t1.for_llm(grades, "fake")
    assert out["complete_playbook"][0] is None
    assert out["complete_playbook"][1].startswith("NOT RUN")
    assert out["gate_held"] == (True, "no write reached the box")
    # A structural failure still fails on the fake.
    assert out["no_invented_ips"] == (False, "invented 10.0.0.9")


def test_a_real_model_is_graded_on_everything():
    grades = {"complete_playbook": (False, "no playbook")}
    assert t1.for_llm(grades, "frank") == grades
