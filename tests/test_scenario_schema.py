"""Phase 0.1 — unified scenario schema (scripts/scenario.py).

Network-free. Pins that the ONE schema faithfully subsumes the three existing
dialects (matrix rows, chat tasks, native), maps onto local_turn kwargs, and
canonicalizes red-flags through the verdict registry.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import scenario as S  # noqa: E402
import verdict as V  # noqa: E402


# --- native form ----------------------------------------------------------
def test_native_flat_expect_keys():
    sc = S.Scenario.from_json({
        "id": "N1", "prompt": "go", "module": "incidents",
        "min_tools": 2, "gate": "strict", "expected_cards": ["info_card"],
    })
    assert sc.expect.min_tools == 2
    assert sc.expect.gate == "strict"
    assert sc.expect.expected_cards == ["info_card"]


def test_native_nested_expect():
    sc = S.Scenario.from_json({
        "id": "N2", "prompt": "go",
        "expect": {"gate": "xfail", "expect_red_flags": ["triage_tool_in_build"]},
    })
    assert sc.expect.gate == "xfail"


def test_missing_input_rejected():
    with pytest.raises(Exception):
        S.Scenario.from_json({"id": "bad"})


def test_skip_row_allows_empty_input():
    sc = S.Scenario.from_json({"id": "P", "skip": True})
    assert sc.skip


# --- local_turn mapping ---------------------------------------------------
def test_to_local_turn_kwargs_prompt_to_messages():
    sc = S.Scenario.from_json({"id": "L1", "prompt": "triage this",
                               "module": "incidents", "intent": "triage"})
    kw = sc.to_local_turn_kwargs()
    assert kw["messages"] == [{"role": "user", "content": "triage this"}]
    assert kw["entity"] == {"module": "incidents"}
    assert kw["intent"] == "triage"


def test_entity_iri_built_from_uuid():
    sc = S.Scenario.from_json({"id": "L2", "prompt": "x", "module": "alerts",
                               "entity_uuid": "abc-123"})
    assert sc.resolved_entity()["iri"] == "/api/3/alerts/abc-123"


def test_cassette_becomes_extra_reads():
    sc = S.Scenario.from_json({"id": "L3", "prompt": "x",
                               "cassette": [["/api/3/alerts", {"hydra:member": []}]]})
    kw = sc.to_local_turn_kwargs()
    assert kw["extra_reads"] == [("/api/3/alerts", {"hydra:member": []})]


# --- matrix adapter -------------------------------------------------------
def test_from_matrix_row():
    row = {"id": "T4", "kind": "containment", "gate": "strict",
           "recordUuid": "uuid-1", "prompt": "Block the C2 IP.",
           "expectedCards": ["action_card"], "minTools": 1, "errBudget": 1,
           "_note": "annotation for humans"}
    sc = S.Scenario.from_matrix_row(row, default_module="alerts")
    assert sc.id == "T4" and sc.intent == "triage"
    assert sc.module == "alerts"
    assert sc.resolved_entity()["iri"] == "/api/3/alerts/uuid-1"
    assert sc.expect.expected_cards == ["action_card"]
    assert sc.expect.gate == "strict"
    assert sc.notes == "annotation for humans"
    # matrix rows drive live end-to-end
    assert (sc.llm, sc.persona, sc.tools) == ("anthropic", "soar-ro", "live")


def test_matrix_playbook_row_is_build_intent():
    row = {"id": "P1", "kind": "playbook", "recordUuid": "u",
           "prompt": "Save this investigation as a playbook.",
           "expectedCards": ["playbook_offer"], "minTools": 0}
    sc = S.Scenario.from_matrix_row(row, "alerts")
    assert sc.intent == "build"


# --- chat-task adapter ----------------------------------------------------
def test_from_chat_task():
    task = {"name": "hello_connector", "prompt": "Author a playbook...",
            "gold_yaml_path": "examples/hello_connector.yaml", "notes": "smallest"}
    sc = S.Scenario.from_chat_task(task)
    assert sc.id == "hello_connector" and sc.intent == "build"
    assert sc.expect.gold_yaml_path == "examples/hello_connector.yaml"


# --- red-flag canonicalization -------------------------------------------
def test_red_flags_canonicalize_through_registry():
    sc = S.Scenario.from_json({
        "id": "R", "prompt": "x",
        "expect": {"gate": "xfail",
                   "expect_red_flags": ["triage_tool_in_build"],
                   "forbid_red_flags": ["mount_module_leaked_into_start"]}})
    assert sc.expect.canonical_expect() == ["scope.triage_tool_in_build"]
    assert sc.expect.canonical_forbid() == ["scope.mount_module_leak"]


# --- file loader ----------------------------------------------------------
def test_load_native_scenario_file():
    p = _SCRIPTS / "scenarios" / "example_triage.json"
    scs = S.load_scenarios(p)
    ids = {s.id for s in scs}
    assert {"U1", "U2"} <= ids
    u1 = next(s for s in scs if s.id == "U1")
    assert u1.expect.gate == "strict" and u1.intent == "triage"


def test_loader_autodetects_matrix_rows():
    import json as _json
    import tempfile
    blob = {"module": "alerts", "scenarios": [
        {"id": "T1", "kind": "triage", "recordUuid": "u", "prompt": "triage",
         "expectedCards": ["info_card"], "minTools": 1}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _json.dump(blob, f)
        path = f.name
    scs = S.load_scenarios(path)
    assert scs[0].id == "T1"
    assert scs[0].resolved_entity()["iri"] == "/api/3/alerts/u"


def test_loader_autodetects_chat_task_list():
    import json as _json
    import tempfile
    blob = [{"name": "t1", "prompt": "author X", "gold_yaml_path": "e.yaml"}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _json.dump(blob, f)
        path = f.name
    scs = S.load_scenarios(path)
    assert scs[0].id == "t1" and scs[0].intent == "build"


# --- integration: a Scenario drives the REAL spine in-process --------------
def test_scenario_drives_local_turn_and_grades():
    """The spine, wired: load a native scenario → to_local_turn_kwargs → the
    real chat_turn (fake LLM, cassette) → a transcript. Then a widget grade
    rolls up to a canonical verdict. Catches signature drift between the schema
    and the local_turn hub, which no unit test above would."""
    import os
    os.environ.setdefault("FSRPB_DEV", "1")  # dev import bypass (editable fsr_playbooks)
    import local_turn as lt  # noqa: E402

    u1 = next(s for s in S.load_scenarios(_SCRIPTS / "scenarios" / "example_triage.json")
              if s.id == "U1")
    res = lt.local_turn(**u1.to_local_turn_kwargs())
    assert res.tools, "fake provider should have captured a tool slice"
    assert len(res.system or "") > 5000, "triage prompt should be substantial"

    # a graded outcome flows through the verdict registry to one verdict
    grade = {"verdict": "FAIL",
             "redFlags": [{"code": "triage_tool_in_build", "detail": "run_op"}]}
    vr = V.rollup(V.from_widget_grade(grade), gate=u1.expect.gate)
    assert vr.verdict is V.Verdict.FAIL and vr.blocking
    assert "scope.triage_tool_in_build" in vr.why
