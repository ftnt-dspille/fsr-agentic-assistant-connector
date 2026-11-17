"""Case-state spine P4 — phase transitions + the two plan evals.

Phase advances at the `_persist_session_state` chokepoint from the turn's
wire events (pushed > contained > offered > awaiting_approval; a first
eventful turn moves new → building/investigating). The evals:
- §A: a successful build session must END `offered`/`pushed` — never prose.
- Re-triage-after-resume: a resume turn must not re-open the hunt.
Pure + offline.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


def _persist(sid: str, transcript: list[dict], intent: str | None = None):
    from connector_fsr_agentic_assistant import operations as ops
    from connector_fsr_agentic_assistant.storage import default_storage
    storage = default_storage()
    if intent:
        storage.set_session_intent(sid, intent)
    ops._persist_session_state(
        storage, sid,
        [{"role": "user", "content": "go"},
         {"role": "assistant", "content": "ok"}],
        transcript)
    return (storage.get_case_state(sid) or {}).get("phase")


TEXT = {"type": "text", "text": "some prose"}


def test_action_card_moves_to_awaiting_approval(config, tmp_db):
    phase = _persist("p4-a", [TEXT, {"type": "action_card", "id": "c1",
                                     "connector": "fgt", "operation": "block"}],
                     intent="triage")
    assert phase == "awaiting_approval"


def test_playbook_offer_moves_to_offered(config, tmp_db):
    phase = _persist("p4-b", [TEXT, {"type": "playbook_offer", "id": "o1"}],
                     intent="build")
    assert phase == "offered"


def test_pushed_info_card_moves_to_pushed(config, tmp_db):
    phase = _persist("p4-c", [
        {"type": "info_card", "id": "pushed-o1", "variant": "playbook_pushed",
         "title": "Saved"}, TEXT], intent="build")
    assert phase == "pushed"


def test_approved_tier3_exec_moves_to_contained(config, tmp_db):
    phase = _persist("p4-d", [
        {"type": "tool_use", "id": "exec-1", "name": "run_op", "tier": 3,
         "input": {"connector": "fgt", "op": "block"}},
        {"type": "tool_result", "tool_use_id": "exec-1",
         "content": {"ok": True, "data": {}}},
        TEXT], intent="triage")
    assert phase == "contained"


def test_failed_tier3_exec_does_not_contain(config, tmp_db):
    phase = _persist("p4-e", [
        {"type": "tool_use", "id": "exec-1", "name": "run_op", "tier": 3,
         "input": {}},
        {"type": "tool_result", "tool_use_id": "exec-1",
         "content": {"ok": False, "code": "exec_dispatch_failed"}},
        TEXT], intent="triage")
    assert phase == "investigating"


def test_first_turn_phases_by_intent(config, tmp_db):
    assert _persist("p4-f1", [TEXT], intent="triage") == "investigating"
    assert _persist("p4-f2", [TEXT], intent="build") == "building"


def test_phase_is_sticky_across_prose_turns(config, tmp_db):
    """A later prose-only turn must not regress an advanced phase."""
    sid = "p4-g"
    assert _persist(sid, [TEXT, {"type": "playbook_offer", "id": "o1"}],
                    intent="build") == "offered"
    assert _persist(sid, [TEXT]) == "offered"


def test_pushed_wins_over_offer_in_same_turn(config, tmp_db):
    phase = _persist("p4-h", [
        {"type": "playbook_offer", "id": "o1"},
        {"type": "info_card", "id": "pushed-o1",
         "variant": "playbook_pushed", "title": "Saved"}], intent="build")
    assert phase == "pushed"


# ---- eval 1 (§A): a successful build session ends offered/pushed ----------

BUILD_TERMINAL_PHASES = ("offered", "pushed")


def test_eval_build_session_ends_offered_or_pushed(config, tmp_db):
    """The §A eval: after a full build flow (offer turn, then accept+push
    turn), the persisted phase is a terminal build phase — never a
    prose-tail 'building'."""
    sid = "p4-eval-a"
    _persist(sid, [TEXT, {"type": "playbook_offer", "id": "o1"}],
             intent="build")
    phase = _persist(sid, [
        {"type": "info_card", "id": "pushed-o1",
         "variant": "playbook_pushed", "title": "Saved"}, TEXT])
    assert phase in BUILD_TERMINAL_PHASES


def test_eval_prose_only_build_session_is_flagged(config, tmp_db):
    """The failure mode the eval exists to catch: a build session whose
    turns are all prose stays 'building' — detectably NOT terminal."""
    sid = "p4-eval-b"
    _persist(sid, [TEXT], intent="build")
    phase = _persist(sid, [{"type": "text", "text": "here is your yaml ..."}])
    assert phase not in BUILD_TERMINAL_PHASES
    assert phase == "building"


# ---- eval 2: transcript scan for re-triage-after-resume -------------------

# Tools whose appearance in a RESUME turn mean the agent restarted the hunt
# instead of acting on the analyst's decision (the §4.6 live defect).
_HUNT_OPENERS = {"get_record", "search_module_records", "triage_preflight"}


def _retriage_after_resume(resume_transcript: list[dict]) -> bool:
    """Transcript-scan eval: True when a resume turn re-opens the hunt."""
    return any(ev.get("type") == "tool_use"
               and ev.get("name") in _HUNT_OPENERS
               for ev in resume_transcript)


def test_eval_retriage_scan_flags_hunt_reopen():
    assert _retriage_after_resume([
        {"type": "tool_use", "id": "t1", "name": "get_record", "input": {}},
    ]) is True
    assert _retriage_after_resume([
        {"type": "tool_use", "id": "t1", "name": "search_module_records",
         "input": {"module": "alerts"}},
    ]) is True


def test_eval_retriage_scan_passes_decision_action():
    """The §4.7-correct shape: act on the decision, zero re-hunt."""
    assert _retriage_after_resume([
        {"type": "tool_use", "id": "t1", "name": "find_containment_actions",
         "input": {"target_type": "ip"}},
        {"type": "tool_use", "id": "t2", "name": "emit_action_card",
         "input": {}},
        {"type": "text", "text": "staged the block for approval"},
    ]) is False
