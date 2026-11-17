"""An offer accept's outcome must survive into chat_history.

The offer-accept paths (`playbook_offer` / `enhancement_offer`) return their
end_turn directly from chat_resume and never pass the turn-persist chokepoint,
so a failed apply left NO trace in the session history: live on 8.0 an
enhancement apply 500'd twice (FK violation on `collection`) and the stored
session read as if Apply was never clicked -- the analyst saw nothing and
retried. `_offer_end_turn` now appends its transcript to the render-fidelity
history itself.
"""
import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


def test_offer_end_turn_lands_in_history(tmp_db):
    from connector_fsr_agentic_assistant import operations as o
    from connector_fsr_agentic_assistant.storage import default_storage

    resp = o._offer_end_turn(
        "sess-hist-1",
        [{"type": "text",
          "text": "I compiled the edit but saving it to FortiSOAR failed."}],
        saved=False, code="update_failed")
    assert resp["ok"] is True and resp["stop_reason"] == "end_turn"

    rows = default_storage().list_transcript("sess-hist-1", limit=10)
    assert rows, "the apply outcome vanished from chat_history"
    frames = rows[-1].get("transcript") or []
    assert any("saving it to FortiSOAR failed" in str(f.get("text", ""))
               for f in frames if isinstance(f, dict))


def test_offer_end_turn_survives_a_broken_history_store(tmp_db, monkeypatch):
    """History persistence is best-effort: a storage failure must not turn a
    successful (or honestly-failed) apply into a resume error."""
    from connector_fsr_agentic_assistant import operations as o
    from connector_fsr_agentic_assistant import storage as st

    def _boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(st, "default_storage", _boom)

    resp = o._offer_end_turn("sess-hist-2", [{"type": "text", "text": "hi"}],
                             saved=True, code=None)
    assert resp["ok"] is True


def test_a_persisted_save_is_not_written_twice(tmp_db):
    """The successful Accept already writes its transcript through
    _persist_session_state; _offer_end_turn appending it again rendered
    "Saved ..." twice after every refresh (live, .159)."""
    from connector_fsr_agentic_assistant import operations as o
    from connector_fsr_agentic_assistant.storage import default_storage

    frames = [{"type": "text", "text": "Done -- saved."}]
    o._offer_end_turn("sess-dup-1", frames, saved=True, persisted=True)
    assert default_storage().list_transcript("sess-dup-1", limit=10) == []
    o._offer_end_turn("sess-dup-2", frames, saved=True)
    assert len(default_storage().list_transcript("sess-dup-2", limit=10)) == 1
