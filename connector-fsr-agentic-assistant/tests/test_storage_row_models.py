"""The storage row models are WIRED (#50).

Six models described `storage.py`'s sqlite row and payload shapes and were
checked by nothing but their own unit tests: `storage.py` imported exactly one
model (`TurnStartFrame`), and everything the load/save path read or wrote was a
raw dict. An authored model that checks nothing is worse than no model, because
it reads as coverage -- the lesson #33 was filed on. So: wire them, or delete
them. This file is what makes "wired" checkable.

Why it matters more here than the type-error it catches: the persisted
transcript is what `chat_history` and `chat_resume` REPLAY, so a drifted row
does not fail at write time. It fails a turn later, on the resume, where the
cause is furthest from the symptom.

Policy under test (`storage._check_row`): permissive about unknown keys (these
rows come back from databases written by OLDER connector versions, so
strictness would be an upgrade hazard), side-effect only (never substitutes a
`model_dump()` for a dict the callers read directly), warn in production and
raise under `FSRPB_STRICT_MODELS=1`.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pydantic_models as M  # noqa: E402
import storage as S  # noqa: E402

# Every model #50 named, and the storage callable that must exercise it.
WIRED = {
    "ChatTurnRow": "list_turns",
    "ChatMessageRow": "record_chat_message",
    "TurnProgressRow": "append_turn_progress",
    "ConversationMessage": "load_conversation",
    "TranscriptUserPayload": "list_transcript",
    "TranscriptAssistantPayload": "list_transcript",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    return S.Storage(tmp_path / "t.db")


def test_the_models_all_still_exist():
    """A renamed model would make every `_check_row` call below a silent no-op
    in production (it catches the AttributeError and warns), so the names are
    pinned here rather than left to the call sites."""
    missing = [n for n in WIRED if getattr(M, n, None) is None]
    assert not missing, missing



def test_a_drifted_transcript_row_is_caught_on_the_replay_path(store):
    """The defect the wiring exists for: a row whose `text` became a dict is
    written fine and only surfaces when `chat_history` replays it."""
    store.append_transcript_user("s1", "hello")
    assert store.list_transcript("s1")[0]["user"] == "hello"

    with store._conn() as c:
        c.execute("UPDATE session_transcript SET payload_json=?",
                  (json.dumps({"text": {"not": "a string"}}),))
    with pytest.raises(Exception):
        store.list_transcript("s1")


def test_a_drifted_assistant_transcript_row_is_caught_on_replay(store):
    """The assistant-turn payload is replayed by list_transcript too; a
    `transcript` that is no longer a list must be caught on that path."""
    store.append_transcript_assistant("s1", [{"type": "text", "text": "hi"}])
    assert store.list_transcript("s1")[0]["transcript"] == [
        {"type": "text", "text": "hi"}]

    with store._conn() as c:
        c.execute("UPDATE session_transcript SET payload_json=?",
                  (json.dumps({"transcript": "not-a-list"}),))
    with pytest.raises(Exception):
        store.list_transcript("s1")


def test_a_drifted_conversation_row_is_caught_on_resume(store):
    store.save_conversation("s1", [{"role": "user", "content": "hi"}])
    assert store.load_conversation("s1") == [{"role": "user", "content": "hi"}]

    store.save_conversation("s1", [{"role": "user", "content": ["not", "text"]}])
    with pytest.raises(Exception):
        store.load_conversation("s1")


def test_a_drifted_turn_row_is_caught(store):
    store.record_chat_turn({"session": "s1", "turn": 1, "model": "m",
                            "input_tokens": 5, "output_tokens": 6})
    assert store.list_turns("s1")[0]["turn"] == 1

    with store._conn() as c:
        c.execute("UPDATE chat_turns SET input_tokens='not-a-number'")
    with pytest.raises(Exception):
        store.list_turns("s1")


def test_a_bad_write_is_caught_on_the_tables_nothing_reads_back(store):
    """`chat_messages` is never SELECTed and `turn_progress` is only projected
    two columns at a time, so for these two the INSERT is the only boundary the
    row shape as a whole ever crosses. Checking on a load path that does not
    exist would be a check that never runs."""
    store.record_chat_message("s1", 1, 0, "user", "hi")
    store.append_turn_progress("s1", 1, {"type": "text", "text": "hi"})

    with pytest.raises(Exception):
        store.record_chat_message("s1", "turn-one", 0, "user", "hi")
    with pytest.raises(Exception):
        store.append_turn_progress("s1", "turn-one", {"type": "text"})


def test_production_never_raises_over_a_drifted_row(tmp_path, monkeypatch):
    """The half that keeps this from being a new outage source. A reporting
    defect must not fail the turn it is reporting on, so outside strict mode a
    bad row warns and the caller gets its dict back unchanged."""
    monkeypatch.delenv("FSRPB_STRICT_MODELS", raising=False)
    store = S.Storage(tmp_path / "t.db")
    store.save_conversation("s1", [{"role": "user", "content": ["not", "text"]}])
    # Returned RAW -- not a model_dump, and not an exception.
    assert store.load_conversation("s1") == [
        {"role": "user", "content": ["not", "text"]}]

    store.append_transcript_user("s1", "hello")
    with store._conn() as c:
        c.execute("UPDATE session_transcript SET payload_json=?",
                  (json.dumps({"text": {"not": "a string"}}),))
    assert store.list_transcript("s1")[0]["user"] == {"not": "a string"}


def test_an_unknown_key_from_an_older_or_newer_writer_is_tolerated(store):
    """Strict mode is on for this fixture, so this would raise if the row
    models were `extra="forbid"`. They must not be: these rows are read back
    from databases written by other connector versions."""
    store.save_conversation("s1", [{"role": "user", "content": "hi",
                                    "a_key_from_a_newer_connector": True}])
    assert store.load_conversation("s1")[0]["a_key_from_a_newer_connector"] is True


def test_a_missing_model_does_not_break_the_turn(monkeypatch, tmp_path):
    """`_check_row` resolves the model by NAME, so a rename must degrade to a
    warning rather than an AttributeError inside a live save."""
    monkeypatch.delenv("FSRPB_STRICT_MODELS", raising=False)
    store = S.Storage(tmp_path / "t.db")
    S._check_row("NoSuchModelName", {"anything": 1})  # must not raise
    store.record_chat_message("s1", 1, 0, "user", "hi")
