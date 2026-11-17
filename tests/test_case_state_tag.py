"""Envelope tags carry a compact case_state summary (widget status strip).

`_case_state_summary_tag` mirrors the persist chokepoint's load+advance so
the tag matches what gets persisted; it must fail-open (None) rather than
ever failing a turn. Pure + offline.
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


TEXT = {"type": "text", "text": "some prose"}


def _tag(sid: str, transcript: list[dict], intent: str | None = None,
         case_state=None):
    from connector_fsr_agentic_assistant import operations as ops
    from connector_fsr_agentic_assistant.storage import default_storage
    storage = default_storage()
    if intent:
        storage.set_session_intent(sid, intent)
    return ops._case_state_summary_tag(storage, sid, transcript,
                                       case_state=case_state)


def test_tag_shape_and_phase_advance(config, tmp_db):
    tag = _tag("cst-a", [TEXT, {"type": "action_card", "id": "c1",
                                "connector": "fgt", "operation": "block"}],
               intent="triage")
    assert tag is not None
    assert tag["phase"] == "awaiting_approval"
    assert set(tag) == {"phase", "record_key", "scenario", "searched",
                        "enriched", "unavailable_connectors"}
    assert tag["searched"] == 0 and tag["enriched"] == 0
    assert tag["unavailable_connectors"] == []


def test_tag_matches_persisted_phase(config, tmp_db):
    """The tag's phase must equal what _persist_session_state stores for the
    same transcript (idempotent double-advance)."""
    from connector_fsr_agentic_assistant import operations as ops
    from connector_fsr_agentic_assistant.storage import default_storage
    storage = default_storage()
    sid = "cst-b"
    storage.set_session_intent(sid, "build")
    transcript = [TEXT, {"type": "playbook_offer", "id": "o1"}]
    tag = ops._case_state_summary_tag(storage, sid, transcript)
    ops._persist_session_state(
        storage, sid,
        [{"role": "user", "content": "go"},
         {"role": "assistant", "content": "ok"}],
        transcript)
    stored = storage.get_case_state(sid) or {}
    assert tag["phase"] == stored.get("phase") == "offered"


def test_tag_reports_investigation_and_capabilities(config, tmp_db):
    from connector_fsr_agentic_assistant import operations as ops
    cs_mod = ops._case_state_mod()
    if cs_mod is None:
        pytest.skip("case_state module unavailable in this wheel")
    cs = cs_mod.CaseState()
    cs.investigation.searched = ["10.0.0.1", "evil.example"]
    cs.investigation.enriched = ["51.15.43.205"]
    cs.capabilities.unavailable = {"whois-rdap": "connector_not_configured"}
    from connector_fsr_agentic_assistant.storage import default_storage
    tag = ops._case_state_summary_tag(default_storage(), "cst-c", [TEXT],
                                      case_state=cs)
    assert tag["searched"] == 2
    assert tag["enriched"] == 1
    assert tag["unavailable_connectors"] == ["whois-rdap"]


def test_tag_fails_open_on_broken_storage(config, tmp_db):
    from connector_fsr_agentic_assistant import operations as ops

    class _Broken:
        def get_case_state(self, sid):
            raise RuntimeError("boom")

        def get_session_intent(self, sid):
            raise RuntimeError("boom")

    assert ops._case_state_summary_tag(_Broken(), "cst-d", [TEXT]) is None
