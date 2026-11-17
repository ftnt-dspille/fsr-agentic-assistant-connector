"""The playbook this conversation built is edited, not re-typed.

Live (build sweep, "build, then refine twice"): refining an unsaved draft had
no open playbook, so edit_playbook was refused and the model re-typed the
draft -- it lost the incident's alert link and rewrote the escalation
condition while saying "everything else is unchanged". On a build turn with
nothing open, the connector now grounds the conversation's latest draft or the
playbook it saved, and an Apply with no designer open writes to that saved
playbook by the id recorded when it was saved.
"""
from __future__ import annotations

import time

import pytest

from connector_fsr_agentic_assistant import operations as o
from connector_fsr_agentic_assistant.storage import Storage
import ops_autonomy
import ops_chat
import ops_playbooks
import ops_resume
import ops_scheduling

PB = "collection: C\nplaybooks:\n  - name: Escalated\n    steps: []\n"


@pytest.fixture
def storage(tmp_path):
    return Storage(db_path=tmp_path / "t.db")


@pytest.fixture(autouse=True)
def _isolated_default_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "d.db"))
    from connector_fsr_agentic_assistant import storage as st
    st._DEFAULT_STORAGE = None
    yield
    st._DEFAULT_STORAGE = None


def _offer(storage, sid, yaml_text, cid="pb"):
    storage.save_card(sid, {"type": "playbook_offer", "id": cid,
                            "final_yaml": yaml_text})


def test_nothing_built_means_nothing_in_hand(storage):
    assert o._playbook_in_hand(storage, "s1") is None


def test_the_latest_offer_is_the_draft(storage, monkeypatch):
    clock = iter([1.0, 2.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))
    _offer(storage, "s1", "v1")
    _offer(storage, "s1", "v2")
    assert o._playbook_in_hand(storage, "s1") == {
        "kind": "draft", "yaml": "v2", "at": 2.0}


def test_a_save_after_the_offer_makes_it_the_saved_playbook(storage, monkeypatch):
    clock = iter([1.0, 2.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))
    _offer(storage, "s1", "v1")
    storage.set_session_saved_playbook("s1", "wf-1", "v1 as saved")
    got = o._playbook_in_hand(storage, "s1")
    assert got == {"kind": "saved", "yaml": "v1 as saved",
                   "workflow_uuid": "wf-1"}


def test_a_new_offer_after_the_save_is_the_draft_again(storage, monkeypatch):
    """Build A, save it, then build B: B is what "add a step" now means."""
    clock = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))
    _offer(storage, "s1", "A")
    storage.set_session_saved_playbook("s1", "wf-1", "A")
    _offer(storage, "s1", "B", cid="pb-b")
    assert o._playbook_in_hand(storage, "s1")["yaml"] == "B"


def test_a_trace_built_offer_carries_no_yaml_and_is_not_in_hand(storage):
    storage.save_card("s1", {"type": "playbook_offer", "id": "pb"})
    assert o._playbook_in_hand(storage, "s1") is None


def test_build_turn_with_nothing_open_grounds_the_draft(storage):
    _offer(storage, "s1", PB)
    ent = o._with_playbook_in_hand(None, "build", storage, "s1")
    assert ent == {"playbook_yaml": PB, "playbook_in_hand": "draft"}
    assert o._entity_has_open_playbook(ent)


def test_the_open_playbook_wins(storage):
    _offer(storage, "s1", PB)
    opened = {"playbook_yaml": "the designer's"}
    assert o._with_playbook_in_hand(opened, "build", storage, "s1") is opened


def test_a_triage_turn_is_left_alone(storage):
    _offer(storage, "s1", PB)
    rec = {"module": "alerts", "iri": "/api/3/alerts/1"}
    assert o._with_playbook_in_hand(rec, "triage", storage, "s1") is rec


@pytest.mark.parametrize("kind,header", [
    ("draft", "PLAYBOOK IN PROGRESS"),
    ("saved", "PLAYBOOK SAVED IN THIS CONVERSATION"),
    (None, "OPEN PLAYBOOK"),
])
def test_the_context_block_says_where_the_playbook_is(kind, header):
    ent = {"playbook_yaml": PB}
    if kind:
        ent["playbook_in_hand"] = kind
    block = o._entity_context_block(ent)
    assert header in block
    assert "edit_playbook" in block or kind is None


def test_saving_a_draft_records_the_saved_playbook(storage, monkeypatch):
    monkeypatch.setattr(ops_chat, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {}})
    monkeypatch.setattr(o, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {}})
    monkeypatch.setattr(ops_resume, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {}})
    monkeypatch.setattr(ops_autonomy, "push_playbook", lambda *a, **k: {
        "ok": True, "workflow_uuids": ["wf-9"], "collection_uuid": "c-9"})
    monkeypatch.setattr(o, "push_playbook", lambda *a, **k: {
        "ok": True, "workflow_uuids": ["wf-9"], "collection_uuid": "c-9"})
    monkeypatch.setattr(ops_playbooks, "push_playbook", lambda *a, **k: {
        "ok": True, "workflow_uuids": ["wf-9"], "collection_uuid": "c-9"})
    monkeypatch.setattr(ops_resume, "push_playbook", lambda *a, **k: {
        "ok": True, "workflow_uuids": ["wf-9"], "collection_uuid": "c-9"})
    monkeypatch.setattr(ops_scheduling, "push_playbook", lambda *a, **k: {
        "ok": True, "workflow_uuids": ["wf-9"], "collection_uuid": "c-9"})
    _offer(storage, "s1", PB)
    o._resume_playbook_offer_accept({}, {"session_id": "s1", "offer_id": "pb"},
                                    storage)
    saved = storage.get_session_saved_playbook("s1")
    assert saved["workflow_uuid"] == "wf-9" and "Escalated" in saved["yaml"]


def _apply(storage, monkeypatch, params, final_yaml=PB):
    calls = []
    monkeypatch.setattr(ops_chat, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {"x": 1}})
    monkeypatch.setattr(o, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {"x": 1}})
    monkeypatch.setattr(ops_resume, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {"x": 1}})

    def _update(config, p):
        calls.append(p)
        return {"ok": True, "workflow_uuid": p.get("workflow_uuid")}
    monkeypatch.setattr(o, "update_playbook", _update)
    monkeypatch.setattr(ops_resume, "update_playbook", _update)
    storage.save_card("s1", {"type": "enhancement_offer", "id": "eo",
                             "final_yaml": final_yaml})
    resp = o._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "eo", **params}, storage)
    return resp, calls


def test_apply_with_no_designer_writes_to_the_saved_playbook(storage, monkeypatch):
    storage.set_session_saved_playbook("s1", "wf-1", PB)
    edited = PB.replace("steps: []", "steps: [{name: Start, type: start}]")
    resp, calls = _apply(storage, monkeypatch, {}, final_yaml=edited)
    assert calls and calls[0]["workflow_uuid"] == "wf-1"
    # the next edit starts from what was just written
    assert storage.get_session_saved_playbook("s1")["yaml"] == edited


def test_apply_never_writes_a_different_playbook_by_name(storage, monkeypatch):
    storage.set_session_saved_playbook("s1", "wf-1",
                                       PB.replace("Escalated", "Something Else"))
    resp, calls = _apply(storage, monkeypatch, {})
    assert calls == []
    assert "missing_workflow_target" in str(resp)


def test_an_open_designer_target_still_wins(storage, monkeypatch):
    storage.set_session_saved_playbook("s1", "wf-1", PB)
    resp, calls = _apply(storage, monkeypatch,
                         {"workflow_iri": "/api/3/workflows/open-one"})
    assert calls[0].get("workflow_iri") == "/api/3/workflows/open-one"
    assert "workflow_uuid" not in calls[0]


def test_the_grounded_source_round_trips_for_the_resume(storage):
    from fsr_playbooks.mcp_server._shared import get_grounded_source
    storage.set_session_grounded_source("s1", "draft")
    tok = o._bind_session_state(o._load_session_state(storage, "s1"))
    try:
        assert get_grounded_source() == "draft"
    finally:
        o._unbind_session_state(tok)
    assert get_grounded_source() is None
    storage.set_session_grounded_source("s1", None)
    assert o._load_session_state(storage, "s1")["grounded_source"] is None
