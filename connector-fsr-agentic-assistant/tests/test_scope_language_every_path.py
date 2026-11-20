"""The declared language and scope on the turns that are not a typed ask.

Pins defects from a live demo box: the attack-timeline quick action had no
declared language (its verdict was Spanish); "lets do 6 hours", answering the
duration a staged block asked for, was declared an investigation and re-ran the
hunt; a playbook build resumed from a choice card had no language and answered
an English ask in Spanish.
"""
from __future__ import annotations

import sys

import pytest
from test_ask_scope_turn import _Result, env, operations  # noqa: F401  (fixtures)


class _Recording:
    model = "fake"

    def __init__(self, declares):
        self.declares, self.asked = declares, []

    async def stream(self, **kw):
        from fsr_playbooks.llm.provider import TextEvent
        self.asked.append(kw["messages"][0].content)
        if self.declares:
            yield TextEvent(text=self.declares)


def _use(env, declares):
    p = _Recording(declares)
    env.monkeypatch.setattr(env.ops_chat, "_build_provider", lambda config, gateway: p)
    return p


def _run(env, messages, sid, **extra):
    import threading
    params = {"intent": "triage", "session_id": sid,
              "entity": {"module": "alerts", "uuid": "a-1", "fields": {"name": "Rule X"}},
              "messages": messages, **extra}
    env.ops.chat_turn({}, params)
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(5.0)
    return env.seen


def test_a_quick_action_gets_a_language_but_keeps_its_own_flow(env):
    _use(env, '{"scope": "investigate", "reason": "r", "language": "English"}')
    seen = _run(env, [{"role": "user", "content": "Build an attack timeline for this alert"}],
                "qa-1", quick_action="attack_timeline")
    assert seen["language"] == "English"
    assert seen["bound"] is None


def test_a_reply_to_the_assistant_question_is_declared_with_that_question(env):
    p = _use(env, '{"scope": "act", "reason": "r", "language": "English"}')
    seen = _run(env, [
        {"role": "user", "content": "Block 1.1.1.1"},
        {"role": "assistant", "content": "I need a value for time_to_live to stage the block."},
        {"role": "user", "content": "lets do 6 hours"}], "follow-1")
    assert "Assistant's last message: I need a value for time_to_live" in p.asked[0]
    assert seen["bound"] == "act"


def test_a_failed_declaration_keeps_the_session_language(env):
    _use(env, '{"scope": "act", "reason": "r", "language": "French"}')
    _run(env, [{"role": "user", "content": "bloque 1.1.1.1"}], "keep-1")
    _use(env, None)
    seen = _run(env, [{"role": "user", "content": "bloque 1.1.1.1"},
                      {"role": "assistant", "content": "ok"},
                      {"role": "user", "content": "6h"}], "keep-1")
    assert seen["language"] == "French"


def test_the_next_declaration_is_told_the_earlier_language(env):
    _use(env, '{"scope": "act", "reason": "r", "language": "French"}')
    _run(env, [{"role": "user", "content": "bloque 1.1.1.1"}], "prior-1")
    p = _use(env, '{"scope": "act", "reason": "r", "language": "French"}')
    _run(env, [{"role": "user", "content": "bloque 1.1.1.1"},
               {"role": "assistant", "content": "ok"},
               {"role": "user", "content": "oui"}], "prior-1")
    assert "Language of the analyst's earlier messages: French" in p.asked[0]


def test_a_card_resume_runs_in_the_session_language(operations, tmp_path, monkeypatch):  # noqa: F811
    from fsr_playbooks.llm import ask_scope
    storage = sys.modules["_fsrpb_conn_scope.storage"].Storage(tmp_path / "r.db")
    storage.set_session_intent("sess", "build")
    storage.set_session_reply("sess", language="English", scope="build")
    storage.save_conversation("sess", [{"role": "user", "content": "build a playbook"}])
    seen = {}

    async def _fake(**kw):
        seen["language"] = ask_scope.current_language()
        return _Result()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _fake)
    operations._resume_conversation(
        {}, {"session_id": "sess", "choice_id": "trigger_choice", "value": "alert_create"},
        "choice", storage, provider=object())
    assert seen["language"] == "English"
    assert not ask_scope.current_language(), "the resume language leaked past the turn"


def test_the_stored_reply_keeps_what_a_later_turn_did_not_declare(tmp_path, operations):  # noqa: F811
    storage = sys.modules["_fsrpb_conn_scope.storage"].Storage(tmp_path / "s.db")
    assert storage.get_session_reply("x") == (None, None)
    storage.set_session_reply("x", language="German", scope="act")
    storage.set_session_reply("x", language=None, scope=None)
    assert storage.get_session_reply("x") == ("German", "act")
    storage.set_session_reply("x", language="English", scope="build")
    assert storage.get_session_reply("x") == ("English", "build")


@pytest.mark.parametrize("msgs,want", [
    ([], ""),
    ([{"role": "user", "content": "a"}], ""),
    ([{"role": "assistant", "content": "Q?"}, {"role": "user", "content": "a"}], "Q?"),
    ([{"role": "assistant", "content": [{"type": "text", "text": "Pick one"}]},
      {"role": "user", "content": "a"}], "Pick one"),
    ([{"role": "assistant", "content": "old"}, {"role": "user", "content": "b"},
      {"role": "assistant", "content": "new"}, {"role": "user", "content": "a"}], "new"),
])
def test_the_previous_assistant_text(operations, msgs, want):  # noqa: F811
    ops_scope = sys.modules["_fsrpb_conn_scope.ops_scope"]
    assert ops_scope._previous_assistant_text(msgs) == want
