"""The TurnPlan install must actually be in effect DURING the live turn.

`test_every_permissive_binding_is_rebound_on_resume` proves the RESUME half
(`_load_session_state`); the framework's `test_turn_plan` proves what the
gate does once a plan is installed. Neither proves chat_turn installs one
around the real agent loop -- and dispatch with no plan is fail-open, so an
install that never fires looks exactly like a gate that passes.

Same observer pattern as test_read_only_chip_bound_during_turn: the
provider records `active_turn_plan()` at the moment it is called, which is
precisely the window the model's tool calls run in.
"""
from __future__ import annotations

import pytest

from tests.test_chat_operations import tmp_db  # noqa: F401
import ops_chat
import ops_manual

OPEN_PB = {"playbook_yaml": "playbooks:\n  - name: Demo\n    steps: []\n"}


@pytest.fixture
def observing_provider(monkeypatch):
    """A provider that records the active TurnPlan seen mid-turn."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from fsr_playbooks.llm.turn_plan import active_turn_plan

    seen: dict = {}

    class _Observer(FakeProvider):
        async def stream(self, **kw):
            seen["plan"] = active_turn_plan()
            seen["system"] = kw.get("system") or ""
            async for ev in super().stream(**kw):
                yield ev

    def _factory(_config, _gateway):
        return _Observer([[
            TextEvent(text="ok"),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=1, output_tokens=1, cache_read=0,
                       cache_write=0, history_chars=1,
                       stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return seen


def _run(config, seen, params):
    from tests.test_chat_operations import _sync_turn
    _sync_turn(config, params)
    assert "plan" in seen, "provider was never called"
    return seen


def test_live_turn_installs_a_plan(config, tmp_db, observing_provider):
    seen = _run(config, observing_provider, {
        "session_id": "plan-live", "intent": "build",
        "messages": [{"role": "user", "content": "build me a playbook"}]})
    assert seen["plan"] is not None, "chat_turn ran with NO TurnPlan installed"


def test_plan_context_reflects_the_open_playbook(config, tmp_db,
                                                 observing_provider):
    seen = _run(config, observing_provider, {
        "session_id": "plan-open", "intent": "build",
        "messages": [{"role": "user", "content": "explain this playbook"}],
        "entity": OPEN_PB})
    assert seen["plan"] is not None
    assert seen["plan"].context.has_open_playbook is True
    # With a playbook open the enhance frontier is afforded.
    assert seen["plan"].gate_refusal("verify_enhancement", {}) is None


def test_plan_gates_enhance_with_no_open_playbook(config, tmp_db,
                                                  observing_provider):
    seen = _run(config, observing_provider, {
        "session_id": "plan-none", "intent": "build",
        "messages": [{"role": "user", "content": "enhance the playbook"}]})
    assert seen["plan"] is not None
    assert seen["plan"].context.has_open_playbook is False
    refusal = seen["plan"].gate_refusal(
        "emit_card", {"card_type": "enhancement_offer"})
    assert refusal is not None and refusal["code"] == "not_afforded"


def test_live_prompt_states_the_plan_constraints(config, tmp_db,
                                                 observing_provider):
    """Phase 2 goal 3: the model is told its budget / tier / never-dead-end
    constraints. plan_turn composes the block; the live prompt must carry it."""
    seen = _run(config, observing_provider, {
        "session_id": "plan-cons", "intent": "build",
        "messages": [{"role": "user", "content": "build me a playbook"}]})
    assert "## Constraints (stated up front)" in seen["system"]
    assert "Tool rounds" in seen["system"]
    assert "Never dead-end the analyst" in seen["system"]


def test_the_plan_does_not_leak_past_the_turn(config, tmp_db,
                                              observing_provider):
    """Workers are pooled. A latched plan would gate the NEXT analyst's turn
    on THIS turn's page state."""
    from fsr_playbooks.llm.turn_plan import active_turn_plan
    _run(config, observing_provider, {
        "session_id": "plan-leak", "intent": "build",
        "messages": [{"role": "user", "content": "explain this"}],
        "entity": OPEN_PB})
    assert active_turn_plan() is None


def test_a_resume_keeps_the_build_turns_playbook_tools_gate(tmp_db):
    """A choice-card click mid-build resumes with no intent on the wire. The
    resume plan must carry the session's stored intent, or the build turn's
    playbook-tools-only gate is off and run_op probes run (analyst-sim: a
    ServiceNow create_incident "probe" staged during a build)."""
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_playbooks.llm.turn_plan import active_turn_plan

    class _Store:
        def get_session_playbook_yaml(self, _sid):
            return None

        def get_session_intent(self, _sid):
            return "build"

    token = ops._bind_session_state(ops._load_session_state(_Store(), "resume-build"))
    try:
        plan = active_turn_plan()
        refusal = plan.gate_refusal("run_op", {"connector": "servicenow"})
        assert refusal is not None and refusal["code"] == "not_a_playbook_tool"
    finally:
        ops._unbind_session_state(token)


def test_a_triage_resume_still_runs_connector_ops(tmp_db):
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_playbooks.llm.turn_plan import active_turn_plan

    class _Store:
        def get_session_playbook_yaml(self, _sid):
            return None

        def get_session_intent(self, _sid):
            return "triage"

    token = ops._bind_session_state(ops._load_session_state(_Store(), "resume-triage"))
    try:
        assert active_turn_plan().gate_refusal("run_op", {}) is None
    finally:
        ops._unbind_session_state(token)
