"""History rehydration must show the REAL sender + original time.

`chat_history` (via storage.list_transcript) returned only `{user: text}`, so
the widget filled the author with the current viewer and the time with now. The
fix persists the widget-supplied actor per user turn + once as the session
initiator, and returns the author and the original `created_at` on replay.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _storage(tmp_path):
    from storage import Storage
    return Storage(tmp_path / "hist.db")


AUTHOR = {"name": "Wendy Smith", "iri": "/api/3/people/abc"}


def test_user_turn_persists_author_and_ts(tmp_path):
    st = _storage(tmp_path)
    st.append_transcript_user("s1", "hello there", author=AUTHOR)
    turns = st.list_transcript("s1")
    assert len(turns) == 1
    t = turns[0]
    assert t["user"] == "hello there"
    assert t["author"] == AUTHOR
    assert isinstance(t["ts"], (int, float)) and t["ts"] > 0


def test_user_turn_without_author_omits_it_but_keeps_ts(tmp_path):
    """Older widgets send no actor — no author key, but the original time still
    rides so the timestamp is correct on replay."""
    st = _storage(tmp_path)
    st.append_transcript_user("s2", "no actor here")
    t = st.list_transcript("s2")[0]
    assert t["user"] == "no actor here"
    assert "author" not in t
    assert t["ts"] > 0


def test_assistant_turn_carries_ts(tmp_path):
    st = _storage(tmp_path)
    st.append_transcript_assistant("s3", [{"type": "text", "text": "hi"}])
    t = st.list_transcript("s3")[0]
    assert t["transcript"] == [{"type": "text", "text": "hi"}]
    assert t["ts"] > 0


def test_session_initiator_first_writer_wins(tmp_path):
    st = _storage(tmp_path)
    assert st.get_session_initiator("s4") is None
    st.set_session_initiator("s4", AUTHOR)
    other = {"name": "Bob Later", "iri": "/api/3/people/xyz"}
    st.set_session_initiator("s4", other)   # must NOT overwrite
    assert st.get_session_initiator("s4") == AUTHOR


def test_empty_author_is_noop(tmp_path):
    st = _storage(tmp_path)
    st.set_session_initiator("s5", {})       # empty → no row
    assert st.get_session_initiator("s5") is None


def test_list_sessions_includes_initiator(tmp_path):
    st = _storage(tmp_path)
    st.append_transcript_user("s6", "start", author=AUTHOR)
    st.set_session_initiator("s6", AUTHOR)
    rows = {r["session_id"]: r for r in st.list_sessions()}
    assert rows["s6"]["initiator"] == AUTHOR
