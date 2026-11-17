"""A resume's outcome must reach the model's history as a fact.

A playbook save and an approved action run happen on a chat_resume, which the
connector answers without a model turn. Before this fix the widget's
`messages[]` carried neither, so the next chat_turn replayed a conversation in
which the save and the run never happened, and the model said "the save was
not confirmed". The connector now writes the outcome into the stored
conversation marked `outcome`, and chat_turn merges it back in.
"""
from __future__ import annotations

from fsr_playbooks.llm.fake_provider import FakeProvider
from fsr_playbooks.llm.provider import TextEvent, UsageEvent

import threading

import ops_chat
from connector_fsr_agentic_assistant import history_replay
from connector_fsr_agentic_assistant.storage import default_storage

SAVED = ("Done -- I saved this investigation as the playbook “Notepad++ "
         "Advisory Patch Verification”.")
FAILED = ("Apply software patches on NinjaOne did not complete. NinjaOne could "
          "not find the item this action targets.")


def _stored_convo():
    """What the connector writes after a save and a failed approve."""
    return [
        {"role": "user", "content": "Block 1.2.3.4"},
        {"role": "assistant", "content": "I staged a block for you to approve."},
        {"role": "user", "outcome": True,
         "content": "Save this triage as a playbook named 'Notepad++'."},
        {"role": "assistant", "outcome": True, "content": SAVED},
        {"role": "user", "outcome": True,
         "content": "I approve action card 'act-1'. Execute ninjaone.apply_software_patches."},
        {"role": "assistant", "outcome": True, "content": [
            {"type": "tool_use", "id": "exec-1", "name": "run_op", "input": {}},
            {"type": "tool_result", "tool_use_id": "exec-1", "content": {"ok": False}},
            {"type": "text", "text": FAILED}]},
    ]


WIDGET_PRIOR = [
    {"role": "user", "content": "Block 1.2.3.4"},
    {"role": "assistant", "content": [{"type": "text",
                                       "text": "I staged a block for you to approve."}]},
    {"role": "user", "content": "test"},
]


def _texts(msgs):
    out = []
    for m in msgs:
        c = m.get("content")
        out.append(c if isinstance(c, str)
                   else " ".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text"))
    return out


# --- the pure merge -------------------------------------------------------

def test_outcomes_the_widget_lacks_go_in_before_the_latest_turn():
    merged = history_replay.with_resume_outcomes(WIDGET_PRIOR, _stored_convo())
    texts = _texts(merged)
    assert texts[-1] == "test", "the analyst's turn must stay last"
    assert SAVED in texts and FAILED in texts
    assert texts.index(SAVED) < texts.index("test")
    assert texts.index(FAILED) < texts.index("test")


def test_the_request_an_outcome_answers_comes_with_it():
    merged = history_replay.with_resume_outcomes(WIDGET_PRIOR, _stored_convo())
    texts = _texts(merged)
    i = texts.index(SAVED)
    assert merged[i - 1]["role"] == "user"
    assert "Notepad++" in texts[i - 1]


def test_an_outcome_the_widget_already_shows_is_not_repeated():
    prior = WIDGET_PRIOR[:-1] + [
        {"role": "assistant", "content": SAVED},
        {"role": "user", "content": "test"}]
    merged = history_replay.with_resume_outcomes(prior, _stored_convo())
    texts = _texts(merged)
    assert texts.count(SAVED) == 1
    assert texts.count(FAILED) == 1


def test_merging_twice_is_the_same_as_merging_once():
    once = history_replay.with_resume_outcomes(WIDGET_PRIOR, _stored_convo())
    twice = history_replay.with_resume_outcomes(once, _stored_convo())
    assert _texts(twice) == _texts(once)


def test_nothing_to_merge_leaves_the_history_alone():
    plain = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"}]
    assert history_replay.with_resume_outcomes(plain, plain) == plain
    assert history_replay.with_resume_outcomes([], _stored_convo()) == []


def test_the_model_is_given_the_outcome_as_history():
    merged = history_replay.with_resume_outcomes(WIDGET_PRIOR, _stored_convo())
    turns = history_replay.replay_turns(merged)
    joined = " ".join(str(c) for _r, c in turns)
    assert "Notepad++ Advisory Patch Verification" in joined
    assert "did not complete" in joined


# --- through chat_turn ----------------------------------------------------

def _usage():
    return UsageEvent(session_id="s", turn=1, model="fake-1", input_tokens=5,
                      output_tokens=2, cache_read=0, cache_write=0,
                      history_chars=0, stop_reason="end_turn", tool_calls=[])


def test_chat_turn_hands_the_model_the_stored_outcome(config, monkeypatch):
    st = default_storage()
    st.set_session_intent("sess-out", "triage")
    st.save_conversation("sess-out", _stored_convo())

    seen: list[list[str]] = []
    real_wire = ops_chat._wire_messages

    def _spy(prior, legacy_text=None):
        seen.append(_texts(prior or []))
        return real_wire(prior, legacy_text)

    monkeypatch.setattr(ops_chat, "_wire_messages", _spy)
    provider = FakeProvider([[TextEvent(text="Understood."), _usage()]])
    monkeypatch.setattr(ops_chat, "_build_provider", lambda _c, _g: provider)

    from connector_fsr_agentic_assistant.operations import chat_turn
    chat_turn(config, {"session_id": "sess-out", "messages": list(WIDGET_PRIOR)})
    for t in list(threading.enumerate()):  # chat_turn runs on a detached thread
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(5.0)

    assert seen, "chat_turn never built the model's history"
    model_view = seen[-1]
    assert model_view[-1] == "test"
    assert any(SAVED == t for t in model_view)
    assert any(FAILED == t for t in model_view)

    # The outcome survives the turn's own save, so the NEXT turn still has it.
    kept = st.load_conversation("sess-out")
    assert any(m.get("outcome") for m in kept)


def test_a_failed_approve_reaches_the_next_turn_end_to_end(config, monkeypatch):
    """Approve a card that the vendor rejects, then chat again from a widget
    history that never saw the failure. The model must still be told."""
    import fsr_playbooks.llm.tools as tools
    from connector_fsr_agentic_assistant import operations

    st = default_storage()
    st.set_session_intent("sess-e2e", "triage")
    st.save_conversation("sess-e2e", [{"role": "user", "content": "Block 1.2.3.4"}])
    card = {"type": "action_card", "id": "act-e2e", "connector": "ninjaone",
            "operation": "apply_software_patches", "args": {"device_id": 1},
            "editable_fields": ["device_id"]}
    st.save_card("sess-e2e", card)
    monkeypatch.setattr(tools, "dispatch", lambda n, a, **kw: {
        "ok": False, "code": "op_request_rejected", "status": "400",
        "message": 'rejected the request (HTTP 400): [404]: Entity not found'})

    operations._resume_action_card_execute(
        config, {"session_id": "sess-e2e", "card_id": "act-e2e", "decision": "approve"},
        "approve", st, None)

    stored_text = [m for m in st.load_conversation("sess-e2e")
                   if m.get("outcome") and m.get("role") == "assistant"]
    assert stored_text, "the approve route did not record its outcome"
    said = history_replay.text_of(stored_text[-1]["content"])
    assert "did not complete" in said
    assert "NinjaOne could not find" in said

    seen: list[list[str]] = []
    real_wire = ops_chat._wire_messages

    def _spy(prior, legacy_text=None):
        seen.append(_texts(prior or []))
        return real_wire(prior, legacy_text)

    monkeypatch.setattr(ops_chat, "_wire_messages", _spy)
    provider = FakeProvider([[TextEvent(text="Noted."), _usage()]])
    monkeypatch.setattr(ops_chat, "_build_provider", lambda _c, _g: provider)
    from connector_fsr_agentic_assistant.operations import chat_turn
    chat_turn(config, {"session_id": "sess-e2e", "messages": [
        {"role": "user", "content": "Block 1.2.3.4"},
        {"role": "user", "content": "what happened with the patch?"}]})
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(5.0)
    assert seen and any("did not complete" in t for t in seen[-1])


def test_a_saved_playbook_is_recorded_as_an_outcome(config, monkeypatch):
    """The save route writes its outcome the same way: the next chat_turn must
    be able to say the playbook exists, and by what name."""
    from connector_fsr_agentic_assistant import ops_resume

    st = default_storage()
    st.set_session_intent("sess-save", "triage")
    st.save_conversation("sess-save", [{"role": "user", "content": "triage this"}])
    final_yaml = ("playbooks:\n- name: Notepad++ Advisory Patch Verification\n"
                  "  steps:\n  - {name: Start, type: start}\n")
    st.save_card("sess-save", {"type": "playbook_offer", "id": "offer-1",
                               "final_yaml": final_yaml, "ops_summary": []})
    monkeypatch.setattr(ops_resume, "compile_yaml", lambda _c, _p: {
        "ok": True, "workflow_json": {}})
    monkeypatch.setattr(ops_resume, "push_playbook", lambda _c, _p: {
        "ok": True, "workflow_uuids": ["d9df9236-0000-0000-0000-000000000000"],
        "collection_uuid": "0bac2744-0000-0000-0000-000000000000"})

    ops_resume._resume_playbook_offer_accept(
        config, {"session_id": "sess-save", "offer_id": "offer-1"}, st)

    said = [history_replay.text_of([m]) if isinstance(m["content"], list)
            else m["content"] for m in st.load_conversation("sess-save")
            if m.get("outcome") and m.get("role") == "assistant"]
    assert said and "Notepad++ Advisory Patch Verification" in said[-1]
    assert "Done" in said[-1]
