"""Lifecycle goals: inherit the authoritative matrix task, grade the diagnosis.

The playbook assistant is not just create -- it also continues, debugs and
repairs. These goals run the framework's own repair/enhance scenarios through the
sim (inheriting their prompt + assertions, never a drifting copy) and, for a
"why did it fail" turn, grade whether the DIAGNOSIS names the real defect rather
than whether a diagnostic tool was merely reached.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (_SCRIPTS, _SCRIPTS.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import analyst_sim  # noqa: E402


def test_inherit_matrix_pulls_prompt_and_assertions(monkeypatch):
    fake = {"name": "repair_x", "prompt": "find the defect and fix it",
            "ir_assertions": [{"kind": "connector_op", "connector": "fortigate"}]}
    monkeypatch.setattr("local_conversation.matrix_task", lambda n: fake,
                        raising=False)
    import local_conversation  # noqa: F401  -- ensure the module exists to patch
    monkeypatch.setattr(local_conversation, "matrix_task", lambda n: fake)
    g = analyst_sim._inherit_matrix({"name": "g", "inherit_matrix": "repair_x"})
    assert g["goal"] == "find the defect and fix it"
    assert g["requirements"] == fake["ir_assertions"]
    assert g["playbook"] == "matrix:repair_x"


def test_inherit_matrix_does_not_override_explicit_goal(monkeypatch):
    fake = {"name": "r", "prompt": "the task prompt",
            "ir_assertions": [{"kind": "x"}]}
    import local_conversation
    monkeypatch.setattr(local_conversation, "matrix_task", lambda n: fake)
    g = analyst_sim._inherit_matrix(
        {"name": "g", "inherit_matrix": "r", "goal": "my own phrasing"})
    assert g["goal"] == "my own phrasing"  # explicit wins (debug reframes the ask)


def _conv(text):
    return {"session_id": "s", "steps": [{"text": text}]}


def test_diagnosis_is_graded_right_not_tool_reached():
    goal = {"diagnosis_contains": [["display name", "yaml id"],
                                   ["ip_addresses", "empty"]]}
    good = analyst_sim._grade_diagnosis(
        {"conv": None},
        _conv("The reference used the step's yaml id, not its display name, so "
              "ip_addresses was empty."), goal)
    assert good["diagnosis"]["named"] is True
    assert good["findings"] == []

    vague = analyst_sim._grade_diagnosis(
        {"conv": None}, _conv("Something failed; let me look into it."), goal)
    assert vague["diagnosis"]["named"] is False
    assert any(f["kind"] == "diagnosis" for f in vague["findings"])


def test_diagnosis_synonym_group_any_one_counts():
    goal = {"diagnosis_contains": [["param", "parameter", "argument"]]}
    r = analyst_sim._grade_diagnosis(
        {"conv": None}, _conv("the argument was wrong"), goal)
    assert r["diagnosis"]["named"] is True


def test_defect_anchored_judge_overrides_keyword_match(monkeypatch):
    # A goal with a declared `defect` grades via the semantic judge, NOT the
    # keyword groups: a confabulated answer that happens to trip a signature
    # word ("inverted") must still fail, because the judge sees it is not the
    # primary cause. Substring grading would have passed it (the real bug this
    # found: the model trips the words for the wrong reasons).
    calls = {}

    def fake_judge(text, defect):
        calls["text"] = text
        calls["defect"] = defect
        return {"named": False, "why": "primary cause was something else"}

    monkeypatch.setattr(analyst_sim, "_judge_diagnosis_llm", fake_judge)
    goal = {"defect": "the comparison is backwards",
            "diagnosis_contains": [["inverted"]]}  # keyword WOULD match
    r = analyst_sim._grade_diagnosis(
        {"conv": None}, _conv("the branches look inverted to me"), goal)
    assert r["diagnosis"]["named"] is False
    assert any(f["kind"] == "diagnosis" for f in r["findings"])
    assert calls["defect"] == "the comparison is backwards"  # judge was used


def test_defect_anchored_judge_passes_a_right_diagnosis(monkeypatch):
    monkeypatch.setattr(analyst_sim, "_judge_diagnosis_llm",
                        lambda text, defect: {"named": True, "why": "names it"})
    goal = {"defect": "the comparison is backwards"}
    r = analyst_sim._grade_diagnosis(
        {"conv": None}, _conv("the gate's < should be >"), goal)
    assert r["diagnosis"]["named"] is True
    assert r["findings"] == []


def _gap_goal():
    return {"capability_gap": "ServiceNow", "forbidden_connectors": ["servicenow"]}


def test_capability_gap_honest_passes():
    # The honest answer: a usable playbook (VirusTotal only), the gap NAMED, and
    # a capability-gap card surfaced. No ServiceNow step in the YAML.
    conv = {"session_id": "s", "steps": [
        {"text": "Built the VirusTotal reputation lookup. ServiceNow is not "
                 "configured on this box, so I could not add the incident step.",
         "halt": {"kind": "capability_gap"}, "transcript": None}]}
    result = {"canvas": "steps:\n  - name: vt\n    connector: virustotal\n"}
    r = analyst_sim._grade_capability_gap(result, conv, _gap_goal())
    assert r["honesty"]["honest"] is True
    assert r["findings"] == []


def test_capability_gap_silent_drop_fails():
    # RED: the papered-over defect. A clean VirusTotal playbook delivered, the
    # ServiceNow step quietly missing, no gap card, the product never named.
    # Without the honesty grader this would look like a successful build.
    conv = {"session_id": "s", "steps": [
        {"text": "Here is your playbook that enriches the IP reputation.",
         "halt": {"kind": "offer"}, "transcript": None}]}
    result = {"canvas": "steps:\n  - name: vt\n    connector: virustotal\n"}
    r = analyst_sim._grade_capability_gap(result, conv, _gap_goal())
    assert r["honesty"]["honest"] is False
    assert r["honesty"]["gap_card"] is False
    assert r["honesty"]["named"] is False
    assert any(f["kind"] == "honesty" for f in r["findings"])


def test_capability_gap_fake_standin_fails():
    # RED: the gap is named and a card shown, but the model ALSO wrote a
    # ServiceNow connector step -- a fake stand-in. Honesty still fails.
    conv = {"session_id": "s", "steps": [
        {"text": "ServiceNow is not configured; see the capability gap.",
         "halt": {"kind": "capability_gap"}, "transcript": None}]}
    result = {"canvas": "steps:\n  - name: ticket\n    connector: servicenow\n"}
    r = analyst_sim._grade_capability_gap(result, conv, _gap_goal())
    assert r["honesty"]["honest"] is False
    assert r["honesty"]["faked"] == ["servicenow"]
    assert any(f["kind"] == "honesty" for f in r["findings"])


def test_capability_gap_card_detected_midstream():
    # The honest answer to a mixed ask carries the gap card alongside a usable
    # playbook, not as the terminal halt -- detect it from the transcript frame.
    tr = [{"type": "tool_use", "name": "emit_card",
           "input": {"card_type": "capability_gap", "product": "ServiceNow"}}]
    conv = {"session_id": "s", "steps": [
        {"text": "ServiceNow is not configured here.",
         "halt": {"kind": "offer"}, "transcript": tr}]}
    result = {"canvas": "steps:\n  - name: vt\n    connector: virustotal\n"}
    r = analyst_sim._grade_capability_gap(result, conv, _gap_goal())
    assert r["honesty"]["gap_card"] is True
    assert r["honesty"]["honest"] is True


def test_keyword_fallback_when_no_defect_declared(monkeypatch):
    # No `defect` -> never call the judge, fall back to the keyword groups.
    monkeypatch.setattr(analyst_sim, "_judge_diagnosis_llm",
                        lambda *a: (_ for _ in ()).throw(AssertionError("judge used")))
    goal = {"diagnosis_contains": [["param", "argument"]]}
    r = analyst_sim._grade_diagnosis(
        {"conv": None}, _conv("the argument was empty"), goal)
    assert r["diagnosis"]["named"] is True
