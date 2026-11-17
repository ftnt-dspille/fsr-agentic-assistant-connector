"""Capability-gap re-check resume (§E / case-state spine P3).

The card's "Re-check & continue" click must be deterministic: clear the
session's learned unavailable-connector facts (or the new capability guard
re-blocks the retry), bust the framework's availability caches (stale
unhealthy TTL), and hand the agent an explicit re-check instruction.
A "skip" alternative changes nothing — it resumes as a plain choice echo.
Pure + offline (FakeProvider, tmp sqlite).
"""
from __future__ import annotations

import pytest
import ops_chat
import ops_manual


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


def _seed_gap_session(session_id: str, card_id: str,
                      unavailable: dict[str, str]):
    """Persist a conversation, a capability_gap card, and case state with
    learned unavailable connectors — the state a real gap turn leaves behind."""
    from connector_fsr_agentic_assistant.storage import default_storage
    from fsr_playbooks.agent.case_state import CaseState

    storage = default_storage()
    storage.save_conversation(session_id, [
        {"role": "user", "content": "triage this alert"},
        {"role": "assistant", "content": "I hit a capability gap."},
    ])
    storage.save_card(session_id, {
        "type": "capability_gap",
        "id": card_id,
        "missing": "the 'whois-rdap' connector",
        "why": "no active configuration",
        "fix_steps": ["Configure it under Settings → Connectors."],
        "resume": {"label": "Re-check & continue", "value": "recheck_connector"},
        "alternatives": [{"label": "Skip this & note the gap",
                          "value": "skip_gap"}],
    })
    state = CaseState()
    state.capabilities.unavailable.update(unavailable)
    storage.save_case_state(session_id, state.to_dict())
    return storage


def _capture_provider(monkeypatch):
    """FakeProvider-style provider that records the messages it was resumed
    with, so tests can assert on the resume user message."""
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    seen: dict = {}

    class _Prov:
        name = "fake-capgap"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None,
                         case_state=None, **kw):
            seen["messages"] = messages
            seen["system"] = system
            seen["case_state"] = case_state
            yield TextEvent(text="re-checked and continuing")
            yield UsageEvent(
                session_id=tags.get("session_id") if tags else "s", turn=1,
                model="fake-1", input_tokens=1, output_tokens=1,
                cache_read=0, cache_write=0, history_chars=10,
                stop_reason="end_turn",
            )

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _Prov())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _Prov())
    return seen


def test_recheck_clears_capabilities_and_instructs_retry(
        config, tmp_db, monkeypatch):
    from connector_fsr_agentic_assistant.operations import chat_resume
    from connector_fsr_agentic_assistant.storage import default_storage
    import fsr_playbooks.mcp_server.tools_execution as te

    forgotten: list = []
    monkeypatch.setattr(te, "forget_connector_availability",
                        lambda c=None: forgotten.append(c),
                        raising=False)

    sid, cid = "sess-capgap", "capgap_cfg_whois-rdap"
    _seed_gap_session(sid, cid, {"whois-rdap": "connector_not_configured"})
    seen = _capture_provider(monkeypatch)

    out = chat_resume(config, {
        "session_id": sid, "decision": "choice",
        "choice_id": cid, "value": "recheck_connector",
    })
    assert out["ok"] is True

    # The resume user message explicitly instructs a re-check, not a re-hunt.
    last_user = seen["messages"][-1]
    assert last_user.role == "user"
    assert "re-check" in last_user.content
    assert "Do not restart the hunt" in last_user.content

    # The framework availability caches were busted for the named connector.
    assert forgotten == ["whois-rdap"]

    # The persisted case state no longer lists the connector unavailable —
    # the capability guard will allow the retry.
    persisted = default_storage().get_case_state(sid)
    assert persisted["capabilities"]["unavailable"] == {}


def test_skip_alternative_keeps_facts_and_plain_echo(
        config, tmp_db, monkeypatch):
    from connector_fsr_agentic_assistant.operations import chat_resume
    from connector_fsr_agentic_assistant.storage import default_storage
    import fsr_playbooks.mcp_server.tools_execution as te

    forgotten: list = []
    monkeypatch.setattr(te, "forget_connector_availability",
                        lambda c=None: forgotten.append(c),
                        raising=False)

    sid, cid = "sess-capgap-skip", "capgap_cfg_whois-rdap"
    _seed_gap_session(sid, cid, {"whois-rdap": "connector_not_configured"})
    seen = _capture_provider(monkeypatch)

    out = chat_resume(config, {
        "session_id": sid, "decision": "choice",
        "choice_id": cid, "value": "skip_gap",
    })
    assert out["ok"] is True

    # Plain choice echo, no re-check instruction, caches untouched.
    last_user = seen["messages"][-1]
    assert "I chose" in last_user.content
    assert "re-check it now" not in last_user.content
    assert forgotten == []

    # Learned facts survive: the guard keeps short-circuiting the dead probe.
    persisted = default_storage().get_case_state(sid)
    assert persisted["capabilities"]["unavailable"] == {
        "whois-rdap": "connector_not_configured"}


def test_plain_choice_card_resume_unaffected(config, tmp_db, monkeypatch):
    """A choice_card (not capability_gap) resume takes the ordinary path."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    from connector_fsr_agentic_assistant.storage import default_storage
    import fsr_playbooks.mcp_server.tools_execution as te

    forgotten: list = []
    monkeypatch.setattr(te, "forget_connector_availability",
                        lambda c=None: forgotten.append(c),
                        raising=False)

    sid, cid = "sess-choice", "choice-1"
    storage = default_storage()
    storage.save_conversation(sid, [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "pick one"},
    ])
    storage.save_card(sid, {"type": "choice_card", "id": cid,
                            "options": [{"label": "A", "value": "a"}]})
    seen = _capture_provider(monkeypatch)

    out = chat_resume(config, {
        "session_id": sid, "decision": "choice",
        "choice_id": cid, "value": "a",
    })
    assert out["ok"] is True
    assert forgotten == []
    assert "I chose" in seen["messages"][-1].content
