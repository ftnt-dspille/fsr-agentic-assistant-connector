"""Phase 4 -- triage intent.

Asserts the connector branches on the `intent` discriminator (contract §2):
- triage loads the incident-response prompt and a tool slice that drops the
  YAML-authoring / playbook-mutation tools;
- a containment ask under triage produces an `action_card` (the model is
  steered to emit_action_card rather than running a mutating op silently);
- the session's intent persists so a resume re-enters the same prompt/slice;
- a triage->build handoff carrying a prior triage transcript (text +
  tool_use/tool_result blocks) is collapsed to safe text history and runs
  under the build prompt + full tool set.

LLM behavior is scripted via FakeProvider; we never call Anthropic.
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
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


# --- pure-helper unit tests ----------------------------------------------

def test_resolve_intent_discriminator_vs_legacy():
    from connector_fsr_agentic_assistant.operations import _resolve_intent, _legacy_intent_text
    assert _resolve_intent({"intent": "triage"}) == "triage"
    assert _resolve_intent({"intent": "build"}) == "build"
    # Legacy free-text still resolves to build (that shape is unchanged).
    assert _resolve_intent({"intent": "block 1.2.3.4"}) == "build"
    # An ABSENT intent no longer defaults to build. Since the Phase-1 soft page
    # prior, no intent means "derive it from page facts": build on an authoring
    # page, triage everywhere else. With no page facts at all there is nothing
    # authoring about the turn, so the prior is triage. This assertion predates
    # that change and was asserting the old unconditional build default.
    assert _resolve_intent({}) == "triage"
    assert _resolve_intent({"in_playbook_editor": True}) == "build"
    # Legacy text is surfaced only when intent isn't a discriminator.
    assert _legacy_intent_text({"intent": "block 1.2.3.4"}) == "block 1.2.3.4"
    assert _legacy_intent_text({"intent": "triage"}) is None
    assert _legacy_intent_text({"intent": "build"}) is None


def test_triage_slice_is_full_surface_minus_persona_only():
    """Phase 3 full-surface switch: the triage prior no longer walls off the
    authoring surface -- focus comes from the disposition prompt, not tool
    absence. Only the deliberately persona-allowlist-only names (the
    build-only ∩ triage-only intersection, minus the run verb) stay out."""
    from connector_fsr_agentic_assistant.operations import ResolvedPersona, _advertised_tools
    triage = {t["name"] for t in _advertised_tools({}, ResolvedPersona(intent="triage"), include_mcp=False)}
    # The authoring surface is now advertised in triage.
    for keep in ("find", "get_op_schema", "run_op", "emit_card",
                 "list_configured_connectors",
                 "verify_playbook", "emit_card", "validate_yaml"):
        assert keep in triage, keep
    # Persona-allowlist-only posture survives the switch.
    assert "test_template" not in triage


def test_build_slice_gains_grounding_surface_full_switch():
    """Phase 3 full-surface switch (Track C5 wall DOWN): build advertises the
    live investigation surface for GROUNDING steps -- the disposition prompt
    keeps authoring focused, and safety still comes from the tier-3 approval
    gate at dispatch, never from tool absence."""
    import fsr_soc_triage  # noqa: F401 -- triggers register_triage_tools()
    from connector_fsr_agentic_assistant.operations import ResolvedPersona, _advertised_tools

    build = {t["name"] for t in _advertised_tools({}, ResolvedPersona(intent="build"), include_mcp=False)}
    # The grounding/hunt surface is now advertised in build.
    for keep in ("run_op", "get_record", "search_module_records",
                 "find", "get_op_schema",
                 "validate_yaml", "verify_playbook", "analyze_playbook",
                 "verify_enhancement", "emit_card"):
        assert keep in build, f"build slice missing {keep!r}"
    # Persona-allowlist-only posture survives the switch.
    assert "test_template" not in build


def test_build_slice_advertises_trace_tool_without_a_trace():
    """Phase 3.1 reversal, with the original reason preserved.

    This tool WAS dropped without a trace: on 'create a new playbook...' a weak
    model reached for it first and burned the turn on an `empty_trace`. The drop
    fixed that -- but it also meant the advertised tool list changed the moment a
    triage accumulated a trace, and that list is a prompt-cache prefix, so the
    commonest arc we have (triage, then "bottle it") rebuilt the whole cached
    prefix mid-session.

    The trade is deliberate: one possible wasted call on a create, against a
    cache rebuild on every bottle. `empty_trace` carries "fall back to the
    hand-author build path", so the wasted call teaches and does not dead-end.
    the framework's `make tool-gate` (select_build_offer) is the gate that keeps the first half
    of that trade honest -- if a model starts burning creates on it again, that
    fixture regresses and this decision gets revisited."""
    import fsr_soc_triage  # noqa: F401 -- register_triage_tools()
    from connector_fsr_agentic_assistant.operations import ResolvedPersona, _advertised_tools
    with_trace = {t["name"] for t in _advertised_tools({}, ResolvedPersona(intent="build"), include_mcp=False, has_trace=True)}
    without = {t["name"] for t in _advertised_tools({}, ResolvedPersona(intent="build"), include_mcp=False, has_trace=False)}
    assert "build_playbook_from_trace" in with_trace
    assert "build_playbook_from_trace" in without
    # The whole point: the surface does not move when a trace arrives.
    assert with_trace == without
    # Dropping it doesn't disturb the rest of the authoring surface.
    for keep in ("find", "validate_yaml", "verify_playbook"):
        assert keep in without


def test_session_has_trace_none_empty_and_fail_open():
    from connector_fsr_agentic_assistant.operations import _session_has_trace

    class _S:
        def __init__(self, raw):
            self._raw = raw

        def get_session_trace(self, sid):
            return self._raw

    assert _session_has_trace(_S(None), "s1") is False
    assert _session_has_trace(_S("   "), "s1") is False
    # Unparseable trace → fail-open (don't silently hide the authoring tool).
    assert _session_has_trace(_S("not json at all"), "s1") is True


def test_load_prompt_intent_specific_and_cached():
    from connector_fsr_agentic_assistant.operations import _load_prompt
    triage = _load_prompt("triage")
    build = _load_prompt("build")
    assert 'card_type="action"' in triage
    assert "containment" in triage.lower()
    assert "playbook author" in build.lower()
    assert triage != build
    # Cached read returns the same object.
    assert _load_prompt("triage") is triage


def test_wire_messages_replays_block_history_natively():
    """Handoff: a prior transcript carrying tool_use/tool_result blocks
    replays as paired native tool turns -- never as `[called ...]` text, which
    teaches the model to write calls instead of making them."""
    from connector_fsr_agentic_assistant.operations import _wire_messages
    prior = [
        {"role": "user", "content": "Is 1.2.3.4 malicious?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking reputation."},
            {"type": "tool_use", "name": "run_op",
             "input": {"connector": "vt", "operation": "ip_report",
                       "ip": "1.2.3.4"}},
            {"type": "tool_result", "content": {"score": 9}},
        ]},
    ]
    out = _wire_messages(prior, legacy_text=None)
    assert [m.role for m in out] == ["user", "assistant", "user"]
    (text, call) = out[1].content
    assert text == {"type": "text", "text": "Checking reputation."}
    assert call["type"] == "tool_use" and call["name"] == "run_op"
    (result,) = out[2].content
    assert result["tool_use_id"] == call["id"] and '"score": 9' in result["content"]
    assert "[called" not in json.dumps([m.content for m in out])


# --- end-to-end through chat_turn ----------------------------------------

def _capturing_factory(monkeypatch, turns):
    """Install a FakeProvider scripted with `turns`; return a one-element
    list that will hold the constructed provider for assertions."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    holder = []

    def _factory(_config, _gateway):
        p = FakeProvider(turns)
        holder.append(p)
        return p

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return holder


def test_triage_turn_emits_action_card(config, tmp_db, monkeypatch):
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)
    card_args = {
        "id": "card-block-smtp-1",
        "connector": "fortinet-fortigate",
        "operation": "add_to_address_group",
        "summary": "FortiGate: block 12.62.213.134 (SMTP intrusion source)",
        "args": {"ip": "12.62.213.134", "group": "blocked-hosts"},
        "editable_fields": ["ip", "group"],
    }
    turns = [[
        ToolUseEvent(call_id="tu-tri-1", name="emit_action_card",
                     arguments=card_args, tier=0),
        ToolResultEvent(call_id="tu-tri-1",
                        result={"ok": True,
                                "card": {**card_args, "type": "action_card"}}),
        UsageEvent(session_id="s", turn=1, model="fake-1",
                   input_tokens=12, output_tokens=3, cache_read=0,
                   cache_write=0, history_chars=40, stop_reason="tool_use"),
    ]]
    holder = _capturing_factory(monkeypatch, turns)

    out = _sync_turn(config, {
        "session_id": "triage-1",
        "intent": "triage",
        "messages": [{"role": "user", "content":
                      "Alert: 192.168.77.30 -> 12.62.213.134 SMTP intrusion. "
                      "Block the source at the edge."}],
    })

    assert out["ok"] is True
    assert out["stop_reason"] == "awaiting_action_card"
    assert out["transcript"][-1]["type"] == "action_card"
    assert out["transcript"][-1]["operation"] == "add_to_address_group"

    provider = holder[0]
    # Triage prompt + sliced tools were sent to the model.
    assert 'card_type="action"' in provider.last_system
    # Phase 3 full-surface: the authoring tools ride along; the disposition
    # prompt (not tool absence) keeps the triage turn focused.
    sent = {t["name"] for t in provider.last_tools}
    assert "run_op" in sent and "validate_yaml" in sent
    assert "emit_card" in sent
    assert provider.last_tags.get("intent") == "triage"

    # Session intent persisted for resume continuity.
    from connector_fsr_agentic_assistant.storage import default_storage
    assert default_storage().get_session_intent("triage-1") == "triage"


def _scripted_factory(monkeypatch, scripts):
    """Install a fresh FakeProvider per `_build_provider` call, each dequeuing
    the next per-call script from `scripts` (a list of round-trip lists). Unlike
    `_capturing_factory` (one shared script replayed every call), this lets a
    multi-turn flow -- turn / resume-continuation / next-turn -- drive distinct
    model behaviour at each step. Returns the holder list of built providers."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    pending = list(scripts)
    holder = []

    def _factory(_config, _gateway):
        turn = pending.pop(0) if pending else []
        p = FakeProvider([turn])
        holder.append(p)
        return p

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return holder


def test_triage_offer_after_approved_containment(config, tmp_db, monkeypatch):
    """A1 (Phase 6 Track A, offline): the full triage→offer arc through the
    connector. Turn 1 emits an action_card; chat_resume(approve) executes the
    mutating op (recording it into the session's SkillTrace, exactly as the
    live run_op recorder does); a follow-up triage turn then calls
    emit_playbook_offer and the turn terminates at `awaiting_playbook_offer`.

    What this proves that the tool-unit test (`test_playbook_offer.py`) cannot:
    the trace recorded under one turn's `_session_trace_scope` *persists* and is
    *reloaded* on the next turn, so an offer is reachable after a real approval.
    Card-content-derived-from-trace is the tool unit's job; the model's offer
    *timing* is the `-m live` behavioural variant below."""
    from fsr_playbooks.llm.provider import (TextEvent, ToolUseEvent, ToolResultEvent,
                                       UsageEvent)
    from connector_fsr_agentic_assistant.operations import chat_resume
    from connector_fsr_agentic_assistant.storage import default_storage
    from fsr_playbooks.agent import skill_trace
    import fsr_playbooks.llm.tools as fsr_tools

    card_args = {
        "id": "card-iso-1",
        "connector": "fortiedr",
        "operation": "isolate_host",
        "summary": "FortiEDR: isolate 12.62.213.134 (C2 beacon source)",
        "args": {"host": "12.62.213.134"},
        "editable_fields": ["host"],
    }
    offer_args = {
        "id": "offer-1",
        "summary": "Save this containment as a re-runnable playbook?",
        "title_suggestion": "C2 Host Isolation",
    }
    # Three model round-trips, one per _build_provider call:
    #   1) turn 1 -> action_card
    #   2) resume-continuation (post-approval re-entry, 2.5.1) -> plain text
    #   3) turn 2 -> playbook_offer
    scripts = [
        [ToolUseEvent(call_id="tu-iso-1", name="emit_action_card",
                      arguments=card_args, tier=0),
         ToolResultEvent(call_id="tu-iso-1",
                         result={"ok": True,
                                 "card": {**card_args, "type": "action_card"}}),
         UsageEvent(session_id="s", turn=1, model="fake-1", input_tokens=10,
                    output_tokens=3, cache_read=0, cache_write=0,
                    history_chars=40, stop_reason="tool_use")],
        [TextEvent(text="Host isolated. The containment is complete."),
         UsageEvent(session_id="s", turn=2, model="fake-1", input_tokens=8,
                    output_tokens=4, cache_read=0, cache_write=0,
                    history_chars=30, stop_reason="end_turn")],
        [ToolUseEvent(call_id="tu-offer-1", name="emit_playbook_offer",
                      arguments=offer_args, tier=0),
         ToolResultEvent(call_id="tu-offer-1",
                         result={"ok": True,
                                 "card": {**offer_args,
                                          "type": "playbook_offer"}}),
         UsageEvent(session_id="s", turn=3, model="fake-1", input_tokens=9,
                    output_tokens=3, cache_read=0, cache_write=0,
                    history_chars=35, stop_reason="tool_use")],
    ]
    _scripted_factory(monkeypatch, scripts)

    # Turn 1: triage produces the action_card.
    out = _sync_turn(config, {
        "session_id": "offer-arc-1",
        "intent": "triage",
        "messages": [{"role": "user", "content":
                      "Alert: beacon to 12.62.213.134. Isolate the host."}],
    })
    assert out["stop_reason"] == "awaiting_action_card"
    card_id = out["transcript"][-1]["id"]

    # Approve: the connector executes run_op directly. The live run_op records
    # the executed op into the active trace; stub dispatch to do the same so the
    # trace plumbing under _session_trace_scope is exercised end-to-end.
    def _recording_dispatch(name, arguments, **kwargs):
        if name == "run_op":
            skill_trace.record_run_op(
                arguments.get("connector"), arguments.get("op"),
                arguments.get("params"), {"status": "isolated"},
                ref_prefix="data")
        return {"ok": True, "data": {"status": "isolated"}}

    monkeypatch.setattr(fsr_tools, "dispatch", _recording_dispatch)
    res = chat_resume(config, {
        "session_id": "offer-arc-1", "card_id": card_id, "decision": "approve",
        "args": {"host": "12.62.213.134"},
    })
    assert res["ok"] is True
    assert res["tags"]["executed"] is True

    # The trace persisted across the resume: the executed op is durable.
    trace_json = default_storage().get_session_trace("offer-arc-1")
    assert trace_json
    trace = skill_trace.SkillTrace.from_json(trace_json)
    assert len(trace) == 1
    persisted = trace.to_dict()["calls"][0]
    assert persisted["skill_id"] == "run_connector_action"
    assert persisted["resolved_inputs"]["connector"] == "fortiedr"
    assert persisted["resolved_inputs"]["operation"] == "isolate_host"

    # Turn 2: the agent offers to save the playbook; the turn gates on it.
    out2 = _sync_turn(config, {
        "session_id": "offer-arc-1",
        "intent": "triage",
        "messages": [{"role": "user", "content":
                      "Can I reuse this next time?"}],
    })
    assert out2["ok"] is True
    assert out2["stop_reason"] == "awaiting_playbook_offer"
    assert out2["transcript"][-1]["type"] == "playbook_offer"
    assert out2["transcript"][-1]["id"] == "offer-1"


def test_build_handoff_runs_under_build_prompt(config, tmp_db, monkeypatch):
    """A build turn opening with a populated triage history + directive runs
    under the build prompt with the full tool set, and the block history is
    collapsed before reaching the provider."""
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    turns = [[
        TextEvent(text="Here's a re-runnable playbook:\n```yaml\n"
                       "collection: triage_handoff\nplaybooks: []\n```"),
        UsageEvent(session_id="s", turn=1, model="fake-1",
                   input_tokens=30, output_tokens=10, cache_read=0,
                   cache_write=0, history_chars=80, stop_reason="end_turn"),
    ]]
    holder = _capturing_factory(monkeypatch, turns)

    out = _sync_turn(config, {
        "session_id": "handoff-1",
        "intent": "build",
        "messages": [
            {"role": "user", "content": "Is 12.62.213.134 malicious?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "run_op",
                 "input": {"connector": "fortinet-fortigate",
                           "operation": "add_to_address_group"}},
                {"type": "tool_result", "content": {"ok": True}},
            ]},
            {"role": "user", "content":
                "Design a re-runnable playbook. Operations used during "
                "triage: fortinet-fortigate.add_to_address_group."},
        ],
    })

    assert out["ok"] is True
    assert out["last_assistant_yaml"] is not None

    provider = holder[0]
    assert "playbook author" in provider.last_system.lower()
    # A build turn advertises the playbook tools only: dispatch refuses
    # run_op there, so offering it cost a refused round. The triage history
    # still replays its run_op call (below) -- a past call needs no tool.
    build_names = {t["name"] for t in provider.last_tools}
    assert build_names, "build must advertise a non-empty tool slice"
    assert "run_op" not in build_names
    for keep in ("validate_yaml", "verify_playbook", "emit_card"):
        assert keep in build_names, f"build dropped essential authoring tool {keep!r}"
    # Block-list history replays as a native, id-paired run_op call.
    calls = [b for m in provider.last_messages if isinstance(m.content, list)
             for b in m.content if b.get("type") == "tool_use"]
    assert any(c["name"] == "run_op" for c in calls)
    assert "[called" not in json.dumps([m.content for m in provider.last_messages])
