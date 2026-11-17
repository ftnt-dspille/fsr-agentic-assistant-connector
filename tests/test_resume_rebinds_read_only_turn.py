"""The read-only dispatch gate must survive the HITL gap, like the affordance.

Fifth member of one family, and the one still missing when #118 shipped. State
that `chat_turn` establishes from the widget's payload -- persona, mounted record
IRI, open-playbook grounding, and this -- is invisible to
`chat_resume` (contract 4). Each was fixed one at a time, reactively, after a
live failure. `test_every_permissive_binding_is_rebound_on_resume` is the
structural gate that stops the sixth.

The specific defect here: `_READ_ONLY_TURN` is derived from `quick_action` in
`chat_turn` and nowhere else, and it defaults to **False** -- NOT read-only, the
permissive value. A read-only chip turn (explain / find_issues) keeps the HITL
emit_* cards in its slice, so it can suspend on a choice or a manual-input card.
The resume then re-entered the agent loop with the gate off, handing back the
write frontier the chip had withheld.

#117's gate REFUSES a write-frontier tool on a read-only turn rather than
carding it, so such a turn can never produce a write-frontier card to approve.
"""
from __future__ import annotations

import pytest

# `tmp_db` lives in the chat-ops suite, not conftest; re-export it so these
# turns don't touch the developer's real connector sqlite.
from tests.test_chat_operations import tmp_db  # noqa: F401
import ops_chat
import ops_manual

OPEN_PB = {"module": "workflows",
           "playbook_yaml": "playbooks:\n  - name: Demo\n    steps: []\n"}

# The gate acts at DISPATCH -- `dispatch()` returns a `read_only_turn` refusal
# envelope. Asserting on the advertised slice instead would test the DIFFERENT
# chip-driven the framework's `READ_ONLY_WITHHELD_TOOLS` mechanism and pass for the wrong reason;
# that mistake is recorded in #118's own notes.
FRONTIER_TOOL = "emit_playbook_offer"


def _observe():
    """What the agent loop sees at the moment the provider is entered."""
    from fsr_playbooks.llm import tools as T
    return {"read_only": T._is_read_only_turn()}


def _chip_turn(config, session_id, quick_action="explain"):
    """One read-only chip turn against an OPEN playbook."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from connector_fsr_agentic_assistant import operations as ops
    from tests.test_chat_operations import _sync_turn

    seen: dict = {}

    class _Observer(FakeProvider):
        async def stream(self, **kw):
            seen.update(_observe())
            async for ev in super().stream(**kw):
                yield ev

    provider = _Observer([[
        TextEvent(text="Here is what this playbook does."),
        UsageEvent(session_id=session_id, turn=1, model="fake-1",
                   input_tokens=1, output_tokens=1, cache_read=0,
                   cache_write=0, history_chars=1, stop_reason="end_turn"),
    ]])
    _prev = ops._build_provider
    ops_chat._build_provider = ops_manual._build_provider = lambda _c, _g: provider
    try:
        _sync_turn(config, {
            "session_id": session_id, "intent": "build",
            "quick_action": quick_action,
            "messages": [{"role": "user", "content": "explain this playbook"}],
            "entity": OPEN_PB})
    finally:
        ops_chat._build_provider = ops_manual._build_provider = _prev
    return seen


def _resume_observing(config, monkeypatch, session_id, params):
    """Resume and record what the resumed turn's agent loop is given."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from connector_fsr_agentic_assistant import operations as ops

    seen: dict = {}

    class _Observer(FakeProvider):
        async def stream(self, **kw):
            seen.update(_observe())
            async for ev in super().stream(**kw):
                yield ev

    provider = _Observer([[
        TextEvent(text="ok"),
        UsageEvent(session_id=session_id, turn=2, model="fake-1",
                   input_tokens=1, output_tokens=1, cache_read=0,
                   cache_write=0, history_chars=1, stop_reason="end_turn"),
    ]])
    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: provider)
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: provider)
    out = ops.chat_resume(config, {"session_id": session_id, **params})
    assert "read_only" in seen, f"resumed turn never called the provider: {out}"
    return seen


def test_the_turn_itself_is_read_only(config, tmp_db):
    """Baseline -- the gate this file is about does work on the turn. Without
    it, a green suite could mean the gate is dead everywhere."""
    seen = _chip_turn(config, "ro-baseline")
    assert seen["read_only"] is True


@pytest.mark.parametrize("params", [
    {"choice_id": "c1", "decision": "choice", "value": "skip"},
    {"card_id": "card-1", "decision": "reject"},
], ids=["choice", "reject"])
def test_a_resume_of_a_read_only_turn_stays_read_only(
        config, tmp_db, monkeypatch, params):
    """The defect. The originating ask was an `explain` chip -- the analyst
    asked to be told something, not to have anything changed. Answering a
    choice card mid-flow cannot silently convert that into a write turn."""
    sid = f"ro-{params['decision']}"
    _chip_turn(config, sid)
    seen = _resume_observing(config, monkeypatch, sid, params)
    assert seen["read_only"] is True, (
        "the read-only dispatch gate is OFF on the resume of a read-only "
        "turn -- the write frontier the chip withheld is handed back by "
        "answering a card")


def test_the_frontier_is_actually_refused_on_that_resume(
        config, tmp_db, monkeypatch):
    """The ContextVar is a means; this is the end. A hallucinated frontier call
    on the resumed turn must come back as a `read_only_turn` refusal, not as an
    unrequested approval card -- that card IS the offer the analyst never asked
    for (#117)."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from connector_fsr_agentic_assistant import operations as ops

    _chip_turn(config, "ro-dispatch")
    seen: dict = {}

    class _Observer(FakeProvider):
        async def stream(self, **kw):
            from fsr_playbooks.llm import tools as T
            seen["result"] = T.dispatch(FRONTIER_TOOL, {})
            async for ev in super().stream(**kw):
                yield ev

    provider = _Observer([[
        TextEvent(text="ok"),
        UsageEvent(session_id="ro-dispatch", turn=2, model="fake-1",
                   input_tokens=1, output_tokens=1, cache_read=0,
                   cache_write=0, history_chars=1, stop_reason="end_turn"),
    ]])
    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: provider)
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: provider)
    ops.chat_resume(config, {"session_id": "ro-dispatch", "choice_id": "c1",
                             "decision": "choice", "value": "skip"})
    result = seen.get("result")
    assert isinstance(result, dict), f"dispatch never ran: {seen}"
    assert result.get("code") == "read_only_turn", (
        f"{FRONTIER_TOOL} was not refused on the resume of a read-only turn: "
        f"{result}")


def test_a_write_chip_resume_is_unaffected(config, tmp_db, monkeypatch):
    """Silencing case. `add_step` is not a read-only chip, so its resume must
    keep the frontier. A re-bind that latched read-only for everyone would
    break every ordinary edit flow that passes through a card."""
    _chip_turn(config, "ro-write-chip", quick_action="add_step")
    seen = _resume_observing(config, monkeypatch, "ro-write-chip",
                             {"choice_id": "c1", "decision": "choice",
                              "value": "skip"})
    assert seen["read_only"] is False


def test_a_free_typed_resume_is_unaffected(config, tmp_db, monkeypatch):
    """Silencing case 2. No chip at all is not a read-only turn: a free-typed
    turn keeps its write tools, and the two must stay distinct. Conflating them
    would silence free-typed edit asks."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from connector_fsr_agentic_assistant import operations as ops
    from tests.test_chat_operations import _sync_turn

    provider = FakeProvider([[
        TextEvent(text="ok"),
        UsageEvent(session_id="ro-free", turn=1, model="fake-1",
                   input_tokens=1, output_tokens=1, cache_read=0,
                   cache_write=0, history_chars=1, stop_reason="end_turn"),
    ]])
    _prev = ops._build_provider
    ops_chat._build_provider = ops_manual._build_provider = lambda _c, _g: provider
    try:
        _sync_turn(config, {
            "session_id": "ro-free", "intent": "build",
            "messages": [{"role": "user", "content": "what does this do?"}],
            "entity": OPEN_PB})
    finally:
        ops_chat._build_provider = ops_manual._build_provider = _prev

    seen = _resume_observing(config, monkeypatch, "ro-free",
                             {"choice_id": "c1", "decision": "choice",
                              "value": "skip"})
    assert seen["read_only"] is False


def test_a_session_with_nothing_recorded_fails_open(config, tmp_db,
                                                    monkeypatch):
    """No turn was ever recorded for this session (an older build, a wiped db).
    Absence must not invent a refusal -- the resume behaves as before."""
    from connector_fsr_agentic_assistant.storage import default_storage

    default_storage().save_conversation(
        "ro-none", [{"role": "user", "content": "hi"}])
    seen = _resume_observing(config, monkeypatch, "ro-none",
                             {"choice_id": "c1", "decision": "choice",
                              "value": "skip"})
    assert seen["read_only"] is False
