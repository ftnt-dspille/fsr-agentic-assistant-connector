"""A turn-scoped instruction must never become permanent history.

**The strand.** Live on the demo box 2026-08-05 (connector 0.5.100), with the
approval -> park -> form -> submit arc otherwise working end to end, the very
next question stranded. "How do I triage a phishing report?" came back as:

    "I will not resume the run again. I don't yet have the playbook run
     identifier (run_pk) needed to fetch its current workflow status.
     Please either: paste the run_pk / workflow URL..."

That turn ran ZERO tools. It was not confused about the run and it did not try
and fail -- it was obeying an instruction that was still live. `respond_manual_
input` appends the manual-input outcome to the conversation as a **user
message**, and that message ended with:

    "Do NOT call resume_playbook for this gate again. Report what the run does
     next, using get_playbook_run if you need its current status."

Correct for the resumed turn; every clause of it was added to fix a real bug.
But it was persisted verbatim, so on every later turn the model read a standing
order from the analyst -- about a run that had already finished -- competing
with whatever they actually just asked. The readback is nearly literal.

The rule this file enforces: **facts persist, imperatives do not.** The analyst
never said "do not resume this run again", and they certainly never said it
about every future turn.

Same class as the record-referent drift fixed in 0.5.90 (stale context
outliving its turn) and, one level up, the same family as the rest of
PLAN_testing_that_can_fail: nothing crashed, nothing logged, and the wire
transcript for that turn looks entirely healthy.
"""
from __future__ import annotations

import pytest

from tests.test_chat_operations import (tmp_db, _sync_turn,  # noqa: F401
                                        _manual_input_factory)
import ops_chat
import ops_manual

# Clauses that are INSTRUCTIONS to the model, not statements of what happened.
# If any of these reaches the stored conversation it becomes a standing order.
IMPERATIVES = ("Do NOT call resume_playbook", "Report what the run does next",
               "Tell me plainly", "do NOT describe", "Say the values could not")


def _drive_resume(config, monkeypatch, forward, values=None, decision="approve"):
    """chat_turn (emits a manual_input gate) -> respond_manual_input.

    Returns (last message the model saw on the resume turn, stored conversation).
    """
    from connector_fsr_agentic_assistant.operations import respond_manual_input
    from connector_fsr_agentic_assistant.storage import default_storage
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    mi = _manual_input_factory(monkeypatch)
    _sync_turn(config, {"session_id": "strand", "intent": "run the sweep"})

    # Force the forward OUTCOME under test. Off-SOAR the real forward always
    # degrades to `crudhub_unavailable`, so the forwarded branch -- the one the
    # live strand came from -- would never be exercised.
    monkeypatch.setattr(ops_manual, "_forward_manual_input", lambda *a, **k: forward)

    seen = {}

    class _Capture(FakeProvider):
        async def stream(self, *, system, messages, tools, **kw):
            seen["last"] = messages[-1].content
            async for ev in super().stream(system=system, messages=messages,
                                           tools=tools, **kw):
                yield ev

    def _factory(_c, _g):
        return _Capture([[
            TextEvent(text="The run finished."),
            UsageEvent(session_id="strand", turn=2, model="fake-1",
                       input_tokens=5, output_tokens=4, cache_read=0,
                       cache_write=0, history_chars=30, stop_reason="end_turn")]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    respond_manual_input(config, {
        "session_id": "strand", "input_id": mi["id"],
        "workflow_run_iri": mi["workflow_run_iri"],
        "decision": decision, "values": values or {"note": "hello"}})
    return seen.get("last", ""), default_storage().load_conversation("strand")


FORWARDS = {
    "forwarded": {"forwarded": True, "run": "9308"},
    "gate_expired": {"forwarded": False, "reason": "gate_expired",
                     "expired_seconds_ago": 12, "run": "9308"},
    "unreachable": {"forwarded": False, "reason": "crudhub_unavailable"},
}


@pytest.mark.parametrize("case", sorted(FORWARDS), ids=sorted(FORWARDS))
def test_no_imperative_survives_into_history(config, tmp_db, monkeypatch, case):
    """THE strand test. No branch may persist an instruction.

    Parametrized over every forward outcome, not just the one that stranded:
    each branch grew its own directive for its own reason, so each is a way back
    into the same bug.
    """
    _, convo = _drive_resume(config, monkeypatch, FORWARDS[case])
    stored = "\n".join(str(m.get("content", "")) for m in convo)
    leaked = [c for c in IMPERATIVES if c in stored]
    assert not leaked, (
        f"[{case}] instruction(s) persisted as a user message: {leaked}\n"
        "On every later turn the model reads this as a standing order from the "
        "analyst and answers the wrong question.\n\nStored conversation:\n"
        + stored)


@pytest.mark.parametrize("case", sorted(FORWARDS), ids=sorted(FORWARDS))
def test_the_directive_still_reaches_the_resumed_turn(config, tmp_db,
                                                      monkeypatch, case):
    """The other half -- deleting the directive would also pass the test above.

    Each of these instructions exists because of a live bug: without "Do NOT
    call resume_playbook", the model resumed a second time using the gate id as
    the run pk (live: `resume_playbook(run='388')` for run 6839 -> 400 "Select a
    valid choice", which the analyst saw as "approved, then nothing happens").
    Turn-scoped must mean scoped to the turn, not dropped.
    """
    last, _ = _drive_resume(config, monkeypatch, FORWARDS[case])
    assert any(c in last for c in IMPERATIVES), (
        f"[{case}] the resumed turn got no directive at all:\n{last}")


def test_history_still_carries_the_facts(config, tmp_db, monkeypatch):
    """Stripping imperatives must not strip the truth along with them.

    The failure branches state something history genuinely needs: that the
    values did NOT reach the run. If that fact rode along inside the directive,
    dropping the directive would leave the conversation asserting a success that
    never happened -- the P2-honesty bug the message split was built around.
    """
    _, convo = _drive_resume(config, monkeypatch, FORWARDS["gate_expired"])
    stored = "\n".join(str(m.get("content", "")) for m in convo)
    assert "NOT applied" in stored, (
        "history lost the fact that the values never landed:\n" + stored)
    assert "did NOT reach the run" in stored

    _, convo2 = _drive_resume(config, monkeypatch, FORWARDS["forwarded"])
    stored2 = "\n".join(str(m.get("content", "")) for m in convo2)
    assert "already been resumed" in stored2
    assert "9308" in stored2, "the run pk is the anchor for any follow-up"


def test_reject_also_splits(config, tmp_db, monkeypatch):
    _, convo = _drive_resume(config, monkeypatch, FORWARDS["unreachable"],
                             decision="reject")
    stored = "\n".join(str(m.get("content", "")) for m in convo)
    assert "I declined the manual-input gate" in stored
    assert "acknowledge and ask how" not in stored, (
        "the reject branch persisted its instruction:\n" + stored)


def test_the_forwarded_outcome_carries_the_run_pk(config, tmp_db, monkeypatch):
    """The resume message must NAME the run, not ask the analyst for it.

    Live on .206 (0.5.102) the turn right after a successful submit said:

        "I need the playbook run identifier (the workflow run PK / @id) to
         query its current status. Please paste the run_pk..."

    -- one turn after the analyst watched that same run get resumed. Cause: the
    SUCCESS path of `_forward_manual_input` was the only branch that did not
    return `run`, so the message carried `manual_input_id=<id>` and nothing
    else, while the directive told the model to report what the run does next
    "using get_playbook_run". With no pk to pass, it asked the human for one we
    were holding.

    Asserting on the message rather than the forwarder so the property under
    test is the one that matters: whatever the run pk's provenance, the analyst
    must never be asked for it.
    """
    from connector_fsr_agentic_assistant import operations as O

    durable, directive = O._manual_input_user_message(
        "545", "approve", {"note": "hello"},
        {"forwarded": True, "manual_input_id": 545, "run": "9412"})
    assert "9412" in durable, (
        "the resume message does not name the run, so the model has no pk to "
        f"pass to get_playbook_run:\n{durable}")
    assert "workflow run pk 9412" in durable
    # The directive no longer points at a status tool at all -- `get_playbook_run`
    # was never registered, and telling the model to call it sent it improvising
    # REST paths in front of the analyst (see test_directives_name_real_tools).
    # The run pk still belongs in the message: it is the anchor for anything the
    # analyst or a later turn asks about this run.
    assert "get_playbook_run" not in directive
    assert "Do NOT call resume_playbook" in directive
