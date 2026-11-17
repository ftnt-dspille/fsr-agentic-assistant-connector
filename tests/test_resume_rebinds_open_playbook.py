"""A tier-3 tool runs on the RESUME path, so grounding must be bound there.

Live on a lab box: the `emit_playbook_offer` guard (framework
`_guard_against_open_playbook`, which refuses an offer that would delete the
open playbook's steps) was shipped, approved at the tier gate, and returned
`ok=True`. No refusal. The guard was inert in production.

The reason is structural, not a typo. `set_grounded_yaml` was bound only in
`chat_turn`, from the widget's `entity` block. `emit_playbook_offer` is tier 3,
so it NEVER executes on the turn -- it suspends, a human approves, and it runs
inside `chat_resume`, which carries no entity (contract §4). The guard compared
the offer against nothing and passed.

The offline tests missed it because they bind the grounding by hand, which is
not how the real path works -- so they were green while the box was unguarded.
These tests exist to close exactly that gap: they never call
`set_grounded_yaml`, they drive `chat_turn` then `chat_resume` the way the
widget does, and they assert on what the tool sees at the moment it executes.

Third member of the family `_set_resume_record_iri` and the persona re-bind
already belong to. The open playbook is the one nobody re-bound.
"""
from __future__ import annotations


# `tmp_db` lives in the chat-ops suite, not conftest; re-export it so these
# turns don't touch the developer's real connector sqlite.
from tests.test_chat_operations import tmp_db  # noqa: F401
import ops_chat
import ops_manual

OPEN_YAML = (
    "playbooks:\n"
    "  - name: Hunt Indicators\n"
    "    steps:\n"
    "      - name: Start\n"
    "        type: start\n"
    "      - name: Hunt Domains\n"
    "        type: connector\n"
    "      - name: Hunt Files\n"
    "        type: connector\n"
)
# What the live model offered: the two steps whose connectors aren't installed,
# swapped for placeholders, summarised as "ready for deployment".
PLACEHOLDERED = OPEN_YAML.replace("name: Hunt Domains",
                                  "name: Hunt Domains Placeholder") \
                         .replace("name: Hunt Files",
                                  "name: Hunt Files Placeholder")

OPEN_PB = {"module": "workflows", "playbook_yaml": OPEN_YAML}


def _open_the_playbook(config, session_id):
    """Drive one ordinary chat_turn with the playbook open, exactly as the
    widget does. Everything the resume can know about the mount has to come
    from here -- there is no second chance to pass an entity."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from connector_fsr_agentic_assistant import operations as ops
    from tests.test_chat_operations import _sync_turn

    provider = FakeProvider([[
        TextEvent(text="ok"),
        UsageEvent(session_id=session_id, turn=1, model="fake-1",
                   input_tokens=1, output_tokens=1, cache_read=0,
                   cache_write=0, history_chars=1, stop_reason="end_turn"),
    ]])
    _prev = ops._build_provider
    ops_chat._build_provider = ops_manual._build_provider = lambda _c, _g: provider
    try:
        _sync_turn(config, {
            "session_id": session_id, "intent": "build",
            "messages": [{"role": "user",
                          "content": "explain this playbook step by step"}],
            "entity": OPEN_PB})
    finally:
        ops_chat._build_provider = ops_manual._build_provider = _prev


def _stash_tier3(session_id, approval_id, tool="emit_playbook_offer"):
    """Park a SuspendedSession the way the tier gate does, so the resume takes
    the real approval branch instead of the conversational one."""
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    from connector_fsr_agentic_assistant.storage import (
        default_storage, PersistedApprovalGateway,
    )

    susp = SuspendedSession(
        approval_id=approval_id, session_id=session_id, tool=tool,
        tool_use_id="tu-1", args={}, tier=3,
        history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys",
        tags={"session_id": session_id},
    )
    bind(susp)  # mirror production bind-before-stash (S2 verify gate)
    PersistedApprovalGateway(default_storage()).stash(susp)


def _resume_observing(config, monkeypatch, session_id, approval_id, observe):
    """Resume the parked approval with a provider whose `resume()` runs
    `observe()` -- i.e. inside the exact window an approved tier-3 tool
    executes in."""
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent, DoneEvent
    from connector_fsr_agentic_assistant import operations as ops

    class _Observer:
        name = "fake-resume"
        approval_gateway = None

        async def stream(self, **kw):  # pragma: no cover - not used here
            yield UsageEvent(session_id=session_id, turn=1, model="fake-1",
                             input_tokens=1, output_tokens=1, cache_read=0,
                             cache_write=0, history_chars=1,
                             stop_reason="end_turn")

        async def resume(self, *, suspended, decision):
            observe()
            yield TextEvent(text=f"resumed with {decision}")
            yield UsageEvent(session_id=session_id, turn=1, model="fake-1",
                             input_tokens=1, output_tokens=1, cache_read=0,
                             cache_write=0, history_chars=1,
                             stop_reason="end_turn")
            yield DoneEvent(stop_reason="end_turn")

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _Observer())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _Observer())
    return ops.chat_resume(config, {"session_id": session_id,
                                    "approval_id": approval_id,
                                    "decision": "approve"})


def test_turn_persists_the_open_playbook_for_the_resume(config, tmp_db):
    """`chat_resume` carries no entity, so the turn is the only place the open
    playbook can be captured. If this stops persisting, the re-bind below has
    nothing to re-bind and every resume-path guard silently passes again."""
    from connector_fsr_agentic_assistant.storage import default_storage

    _open_the_playbook(config, "ground-persist")
    assert default_storage().get_session_playbook_yaml("ground-persist") \
        == OPEN_YAML


def test_tier3_resume_sees_the_open_playbook(config, tmp_db, monkeypatch):
    """The regression itself: the approved tool must execute WITH the mount.

    Nothing here binds grounding by hand -- that is the whole point. Before the
    re-bind, `get_grounded_yaml()` was None here and the shipped guard was
    inert on the only path it ever runs on."""
    from fsr_playbooks.mcp_server._shared import get_grounded_yaml

    _open_the_playbook(config, "ground-resume")
    _stash_tier3("ground-resume", "appr-ground")

    seen: dict = {}
    out = _resume_observing(config, monkeypatch, "ground-resume",
                            "appr-ground",
                            lambda: seen.update(yaml=get_grounded_yaml()))
    assert out["ok"] is True
    assert "yaml" in seen, "provider.resume was never called"
    assert seen["yaml"] == OPEN_YAML


def test_the_shipped_offer_guard_actually_fires_on_the_resume(
        config, tmp_db, monkeypatch):
    """End-to-end on the live shape: run the REAL guard inside the resume.

    This is the assertion the live run contradicted -- approve returned ok=True
    with the guard silent. It bites only because the grounding reaches here on
    its own."""
    from fsr_playbooks.mcp_server.tools_emit import _offer_from_yaml

    _open_the_playbook(config, "ground-guard")
    _stash_tier3("ground-guard", "appr-guard")

    seen: dict = {}
    _resume_observing(
        config, monkeypatch, "ground-guard", "appr-guard",
        lambda: seen.update(offer=_offer_from_yaml(
            "offer-1", "ready for deployment", PLACEHOLDERED,
            title_suggestion=None, editable_title=True)))

    offer = seen.get("offer")
    assert offer is not None, "provider.resume was never called"
    assert offer.get("ok") is False, (
        "the offer that would delete Hunt Domains / Hunt Files was accepted -- "
        "the guard is inert on the resume path, which is the ONLY path a "
        "tier-3 tool runs on")
    assert offer.get("code") == "offer_drops_open_steps"


def test_a_session_with_nothing_open_still_resumes(config, tmp_db,
                                                   monkeypatch):
    """Fail-open: no persisted playbook ⇒ no bind, and the resume is unchanged.

    A genuine new build must not start refusing its own offer because the
    re-bind guessed at a mount that was never there."""
    from fsr_playbooks.mcp_server._shared import get_grounded_yaml

    _stash_tier3("ground-none", "appr-none")
    seen: dict = {}
    out = _resume_observing(config, monkeypatch, "ground-none", "appr-none",
                            lambda: seen.update(yaml=get_grounded_yaml()))
    assert out["ok"] is True
    assert seen["yaml"] is None


def test_the_rebind_does_not_leak_past_the_resume(config, tmp_db, monkeypatch):
    """Workers are pooled. A latched ContextVar would ground an unrelated later
    session against THIS analyst's playbook -- and grounding silently replaces
    a document, so the leak would surface as someone else's steps appearing."""
    from fsr_playbooks.mcp_server._shared import get_grounded_yaml

    _open_the_playbook(config, "ground-leak")
    _stash_tier3("ground-leak", "appr-leak")
    _resume_observing(config, monkeypatch, "ground-leak", "appr-leak",
                      lambda: None)
    assert get_grounded_yaml() is None
