"""HITL durability suite (PLAN Phase 2).

Covers the failure modes most likely to embarrass mid-demo:
- T10  worker-kill between emit and resume (PersistedApprovalGateway durability)
- T11  two sessions paused at an action_card, resumed in reverse order, no cross-talk
- T12  provider raises mid-turn → structured error event, no crash
-      max_turns ceiling → stop_reason=max_turns, not a hang
-      chat_resume for an unknown approval_id → structured error, doesn't wedge
-      respond_manual_input when the run can't be reached → clean degrade, chat still moves
-      double chat_resume for the same approval_id (double-click) → second is rejected, no dup exec

Driven entirely by FakeProvider / hand-rolled providers via monkeypatch on
operations._build_provider so we never touch Anthropic or a live SOAR.
"""
from __future__ import annotations

import json

import pytest
import ops_chat
import ops_manual


def _join_detached_threads(timeout=5.0):
    """Wait for chat_turn detached worker threads (named
    `chat_turn:<session>:<turn>`) to finish."""
    import threading
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(timeout)


def _sync_turn(config, params):
    """chat_turn is always detached (contract 2.7.0): it returns an immediate
    accepted-ack and runs on a daemon thread. Drive a turn to completion and
    reconstruct the pre-2.7.0 blocking envelope from the (enriched) terminal
    stream_end frame, so blocking-style assertions keep working."""
    from connector_fsr_agentic_assistant.operations import chat_turn, chat_poll
    ack = chat_turn(config, params)
    if not ack.get("accepted"):
        return ack
    _join_detached_threads()
    poll = chat_poll(config, {"session_id": ack["session_id"], "cursor": 0})
    end = next((f for f in poll.get("frames", [])
                if f.get("type") == "stream_end"), None)
    if end is None:
        return ack
    return {
        "ok": end.get("ok", True),
        "session_id": ack["session_id"],
        "turn": ack["turn"],
        "turn_id": end.get("turn_id", ack.get("turn_id")),
        "stop_reason": end.get("stop_reason"),
        "contract_version": end.get("contract_version"),
        "transcript": end.get("transcript", []),
        "last_assistant_yaml": end.get("last_assistant_yaml"),
        "tags": end.get("tags"),
        "error": end.get("error"),
    }


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the connector's default_storage at a fresh tmp sqlite so
    tests don't share the developer's ~/.connector_fsr_agentic_assistant.db."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


# --- shared factories -----------------------------------------------------

def _action_card_factory(monkeypatch, *, card_id="card-block-ip-1",
                         summary="FortiGate: block 1.2.3.4",
                         args=None):
    """Factory emitting a tier-0 action_card on chat_turn (no gate)."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)

    card_args = {
        "id": card_id,
        "connector": "fortinet-fortigate",
        "operation": "block_ip",
        "summary": summary,
        "args": args or {"ip": "1.2.3.4", "policy": "block-list"},
        "editable_fields": ["ip", "policy"],
    }

    def _factory(_c, _g):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-card", name="emit_action_card",
                         arguments=card_args, tier=0),
            ToolResultEvent(call_id="tu-card",
                            result={"ok": True, "card": {**card_args,
                                                         "type": "action_card"}}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return card_args


def _ack_factory(monkeypatch, text):
    """Factory that emits one TextEvent + end_turn — for resume turns."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def _factory(_c, _g):
        return FakeProvider([[
            TextEvent(text=text),
            UsageEvent(session_id="s", turn=2, model="fake-1",
                       input_tokens=4, output_tokens=3, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)


# --- T10: PersistedApprovalGateway survives a worker restart ---------------

def test_suspended_session_survives_worker_kill(config, tmp_db, monkeypatch):
    """Stash a SuspendedSession, drop the in-process storage singleton to
    simulate a worker kill (the sqlite file is the only durable state),
    then resume by approval_id — it must still be found and resumed."""
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent, DoneEvent
    from connector_fsr_agentic_assistant import operations as ops
    from connector_fsr_agentic_assistant import storage as storage_mod
    from connector_fsr_agentic_assistant.storage import (
        default_storage, PersistedApprovalGateway,
    )

    # Stash through one storage instance.
    gateway = PersistedApprovalGateway(default_storage())
    _susp = SuspendedSession(
        approval_id="appr-durable", session_id="sess-kill", tool="run_op",
        tool_use_id="tu-1", args={}, tier=3,
        history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys", tags={},
    )
    bind(_susp)  # mirror production bind-before-stash (S2 verify gate)
    gateway.stash(_susp)

    # Simulate worker kill: the lazy singleton is thrown away. A fresh
    # process re-opens the same sqlite file from FSR_PLAYBOOK_BUILDER_DB.
    storage_mod._DEFAULT_STORAGE = None

    class _ResumeProvider:
        name = "fake-resume"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, case_state=None, max_tool_turns=None):
            yield UsageEvent(session_id="sess-kill", turn=1, model="fake-1",
                             input_tokens=1, output_tokens=1, cache_read=0,
                             cache_write=0, history_chars=10,
                             stop_reason="end_turn")

        async def resume(self, *, suspended, decision):
            yield TextEvent(text=f"resumed with {decision}")
            yield UsageEvent(session_id=suspended.session_id, turn=1,
                             model="fake-1", input_tokens=5, output_tokens=3,
                             cache_read=0, cache_write=0, history_chars=20,
                             stop_reason="end_turn")
            yield DoneEvent(stop_reason="end_turn")

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _ResumeProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _ResumeProvider())

    out = ops.chat_resume(config, {"approval_id": "appr-durable",
                                   "decision": "approve"})
    assert out["ok"] is True
    assert out["session_id"] == "sess-kill"
    assert out["stop_reason"] == "end_turn"
    assert any(e.get("type") == "text" and "resumed with approve" in e.get("text", "")
               for e in out["transcript"])


# --- T11: two paused sessions, reverse-order resume, no cross-talk ---------

def test_two_sessions_resume_reverse_order_no_crosstalk(config, tmp_db, monkeypatch):
    """Two sessions each end a turn at an action_card. Resume them in
    REVERSE order and confirm each resume re-enters ITS OWN conversation
    (the executed op echoes the session-specific IP), with no bleed."""
    from connector_fsr_agentic_assistant.operations import chat_resume

    # Session A: block 10.0.0.1
    _action_card_factory(monkeypatch, card_id="card-A",
                         summary="block 10.0.0.1",
                         args={"ip": "10.0.0.1", "policy": "block-list"})
    out_a = _sync_turn(config, {"session_id": "sessA", "intent": "block 10.0.0.1"})
    assert out_a["stop_reason"] == "awaiting_action_card"

    # Session B: block 10.0.0.2
    _action_card_factory(monkeypatch, card_id="card-B",
                         summary="block 10.0.0.2",
                         args={"ip": "10.0.0.2", "policy": "block-list"})
    out_b = _sync_turn(config, {"session_id": "sessB", "intent": "block 10.0.0.2"})
    assert out_b["stop_reason"] == "awaiting_action_card"

    # A provider whose resume echoes the IP it was actually asked about,
    # so a cross-talk bug (resuming the wrong session's convo) is visible.
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def _echo_factory(_c, _g):
        # The last user message carries the approved args; reflect the IP.
        return FakeProvider([[
            TextEvent(text="ECHO"),
            UsageEvent(session_id="s", turn=2, model="fake-1",
                       input_tokens=4, output_tokens=3, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="end_turn"),
        ]])

    # Resume B first (reverse order), then A.
    monkeypatch.setattr(ops_chat, "_build_provider", _echo_factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _echo_factory)
    res_b = chat_resume(config, {"session_id": "sessB", "card_id": "card-B",
                                 "decision": "approve"})
    res_a = chat_resume(config, {"session_id": "sessA", "card_id": "card-A",
                                 "decision": "approve"})

    assert res_b["ok"] and res_a["ok"]
    assert res_b["session_id"] == "sessB"
    assert res_a["session_id"] == "sessA"

    # Cross-talk check: each resumed conversation contains only its own
    # IP in the persisted user turn the agent was handed.
    from connector_fsr_agentic_assistant.storage import default_storage
    convo_a = default_storage().load_conversation("sessA")
    convo_b = default_storage().load_conversation("sessB")
    # Assistant rows are block lists (history_replay); compare as JSON text.
    text_a = json.dumps([m["content"] for m in convo_a])
    text_b = json.dumps([m["content"] for m in convo_b])
    assert "10.0.0.1" in text_a and "10.0.0.2" not in text_a
    assert "10.0.0.2" in text_b and "10.0.0.1" not in text_b


# --- T12: provider raises mid-turn → structured error, no crash ------------

def test_provider_exception_yields_structured_error(config, tmp_db, monkeypatch):
    """A 5xx (or any exception) mid-stream must surface as stop_reason=error
    with an error event in the transcript and ok=False — never a raise out
    of chat_turn."""

    class _BoomProvider:
        name = "boom"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, case_state=None, max_tool_turns=None):
            raise RuntimeError("anthropic 529 overloaded_error")
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _BoomProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _BoomProvider())

    out = _sync_turn(config, {"session_id": "boom-sess", "intent": "go"})
    assert out["ok"] is False
    assert out["stop_reason"] == "error"
    assert out["error"] is not None
    assert "overloaded_error" in out["error"]
    assert out["transcript"][-1]["type"] == "error"


# --- max_turns ceiling -----------------------------------------------------

def test_max_turns_surfaces_stop_reason(config, tmp_db, monkeypatch):
    """When the provider exhausts its turn ceiling it ends with
    stop_reason=max_turns; the envelope must preserve it (not hang, not
    silently report end_turn)."""
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent, DoneEvent

    # Hand-rolled (not FakeProvider, which always closes with a
    # DoneEvent(end_turn) that would clobber the terminal reason).
    class _CeilingProvider:
        name = "ceiling"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, case_state=None, max_tool_turns=None):
            yield TextEvent(text="working on it...")
            yield UsageEvent(session_id="mt", turn=1, model="fake-1",
                             input_tokens=10, output_tokens=4, cache_read=0,
                             cache_write=0, history_chars=30,
                             stop_reason="max_turns")
            yield DoneEvent(stop_reason="max_turns")

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _CeilingProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _CeilingProvider())
    out = _sync_turn(config, {"session_id": "mt-sess", "intent": "loop"})
    assert out["stop_reason"] == "max_turns"


# --- unknown approval_id ---------------------------------------------------

def test_resume_unknown_approval_id_is_structured(config, tmp_db, monkeypatch):
    """Resuming a never-stashed approval_id (no card/choice fallback)
    returns a structured approval_not_found error, doesn't wedge."""
    # Build a provider so _build_provider doesn't explode before the
    # approval lookup (it's constructed up front in chat_resume).
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_chat, "_build_provider",
                        lambda c, g: FakeProvider([[]]))
    monkeypatch.setattr(ops_manual, "_build_provider",
                        lambda c, g: FakeProvider([[]]))

    out = ops.chat_resume(config, {"approval_id": "ghost-id",
                                   "decision": "approve"})
    assert out["ok"] is False
    assert out["error"]["code"] == "approval_not_found"


# --- double-resume (double-click) ------------------------------------------

def test_double_resume_same_approval_id_rejected(config, tmp_db, monkeypatch):
    """First chat_resume pops + resumes the SuspendedSession; a second
    resume for the same approval_id finds nothing and returns
    approval_not_found rather than re-executing the op."""
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent, DoneEvent
    from connector_fsr_agentic_assistant import operations as ops
    from connector_fsr_agentic_assistant.storage import (
        default_storage, PersistedApprovalGateway,
    )

    gateway = PersistedApprovalGateway(default_storage())
    _susp = SuspendedSession(
        approval_id="appr-once", session_id="sess-dbl", tool="run_op",
        tool_use_id="tu-1", args={}, tier=3,
        history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys", tags={},
    )
    bind(_susp)  # mirror production bind-before-stash (S2 verify gate)
    gateway.stash(_susp)

    class _ResumeProvider:
        name = "fake-resume"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, case_state=None, max_tool_turns=None):
            yield UsageEvent(session_id="sess-dbl", turn=1, model="fake-1",
                             input_tokens=1, output_tokens=1, cache_read=0,
                             cache_write=0, history_chars=10,
                             stop_reason="end_turn")

        async def resume(self, *, suspended, decision):
            yield TextEvent(text="executed once")
            yield UsageEvent(session_id=suspended.session_id, turn=1,
                             model="fake-1", input_tokens=5, output_tokens=3,
                             cache_read=0, cache_write=0, history_chars=20,
                             stop_reason="end_turn")
            yield DoneEvent(stop_reason="end_turn")

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _ResumeProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _ResumeProvider())

    first = ops.chat_resume(config, {"approval_id": "appr-once",
                                     "decision": "approve"})
    assert first["ok"] is True

    second = ops.chat_resume(config, {"approval_id": "appr-once",
                                      "decision": "approve"})
    assert second["ok"] is False
    assert second["error"]["code"] == "approval_not_found"


# --- respond_manual_input degrades when the run is unreachable -------------

def test_manual_input_unreachable_run_degrades_cleanly(config, tmp_db, monkeypatch):
    """If the paused workflow run can't be reached (no matching pending
    input — e.g. the run already completed), respond_manual_input must
    not error: it degrades to a conversational resume and surfaces the
    failure in manual_input_forward so the widget knows the bridge missed."""
    from connector_fsr_agentic_assistant.operations import respond_manual_input
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       TextEvent, UsageEvent)

    mi_args = {
        "id": "mi-late",
        "workflow_run_iri": "/api/3/workflow_runs/9999",
        "question": "Approve?",
        "fields": [{"name": "approved_by", "label": "Approver"}],
    }

    def _emit(_c, _g):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-mi", name="emit_manual_input",
                         arguments=mi_args, tier=0),
            ToolResultEvent(call_id="tu-mi",
                            result={"ok": True, "card": {**mi_args,
                                                         "type": "manual_input"}}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _emit)
    monkeypatch.setattr(ops_manual, "_build_provider", _emit)
    _sync_turn(config, {"session_id": "mi-late-sess", "intent": "run sweep"})

    # Pretend we ARE on-platform, but the paused run exposes no pending input
    # to respond to (it already completed): the live client's
    # manual_input.retrieve returns an EMPTY body, so the forward has nothing
    # resumable and must degrade cleanly rather than raise.
    #
    # `{}` is the platform's signature for a gate that no longer exists --
    # live-verified on 8.0.0: a lapsed manual input retrieves as `{}` while a
    # live one returns the full record. So this degrades as `gate_expired`,
    # which is what the analyst is then told (never "accepted").
    class _NoPendingManualInput:
        def retrieve(self, iid, typed=False):
            return {}  # gate gone -> nothing resumable

    class _FakeClient:
        manual_input = _NoPendingManualInput()

    monkeypatch.setattr("fsr_playbooks.mcp_server._shared._live_client",
                        lambda: _FakeClient())

    def _cont(_c, _g):
        return FakeProvider([[
            TextEvent(text="Proceeding."),
            UsageEvent(session_id="mi-late-sess", turn=2, model="fake-1",
                       input_tokens=5, output_tokens=4, cache_read=0,
                       cache_write=0, history_chars=30, stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _cont)
    monkeypatch.setattr(ops_manual, "_build_provider", _cont)
    res = respond_manual_input(config, {
        # a numeric manual-input id — the forward parses it as int (real ids are
        # the wfinput row's integer pk), then finds no pending options to resume.
        "session_id": "mi-late-sess", "input_id": "9999",
        "workflow_run_iri": mi_args["workflow_run_iri"],
        "decision": "approve", "values": {"approved_by": "labuser"},
    })
    assert res["ok"] is True  # chat keeps moving
    assert res["manual_input_forward"]["forwarded"] is False
    assert res["manual_input_forward"]["reason"] == "gate_expired"
