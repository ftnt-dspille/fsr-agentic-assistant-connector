"""Search WITHIN sessions (history browser content search).

`storage.search_sessions(query)` finds conversations whose transcript CONTENT
contains a keyword — user text OR assistant event text — not just the title, and
returns a `snippet` showing the match. Storage is importable standalone (no
`connectors` platform import), so we exercise it directly.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import Storage  # noqa: E402


@pytest.fixture
def store():
    return Storage(tempfile.mktemp(suffix=".db"))


def test_matches_user_message_content(store):
    store.append_transcript_user("s1", "please fix the FIXME in the block step")
    store.append_transcript_user("s2", "summarize this alert")
    hits = store.search_sessions("fixme")
    assert [h["session_id"] for h in hits] == ["s1"]
    assert "FIXME" in hits[0]["snippet"]
    # Full list-sessions shape is preserved (title/rating/initiator carried).
    assert hits[0]["title"] == "please fix the FIXME in the block step"
    assert "rating" in hits[0] and "initiator" in hits[0]


def test_matches_assistant_event_text(store):
    store.append_transcript_user("s1", "run the playbook")
    store.append_transcript_assistant(
        "s1", [{"type": "text", "text": "left a FIXME note for later"}])
    hits = store.search_sessions("FIXME")
    assert [h["session_id"] for h in hits] == ["s1"]
    assert "FIXME" in hits[0]["snippet"]


def test_case_insensitive_and_blank_query(store):
    store.append_transcript_user("s1", "Check the DEVICE status")
    assert [h["session_id"] for h in store.search_sessions("device")] == ["s1"]
    assert store.search_sessions("") == []
    assert store.search_sessions("   ") == []


def test_no_match_returns_empty(store):
    store.append_transcript_user("s1", "nothing relevant here")
    assert store.search_sessions("FIXME") == []


def test_like_wildcards_are_literal(store):
    # A literal % / _ in the keyword must match itself, not act as a wildcard.
    store.append_transcript_user("s1", "progress is 50% done")
    store.append_transcript_user("s2", "progress is 5 done")
    hits = store.search_sessions("50%")
    assert [h["session_id"] for h in hits] == ["s1"]


def test_snippet_windows_around_match(store):
    long = "x" * 200 + " FIXME " + "y" * 200
    store.append_transcript_user("s1", long)
    snip = store.search_sessions("fixme")[0]["snippet"]
    assert "FIXME" in snip and snip.startswith("…") and snip.endswith("…")
    assert len(snip) < len(long)  # actually windowed, not the whole message


def test_newest_first(store):
    store.append_transcript_user("s1", "FIXME one")
    store.append_transcript_user("s2", "FIXME two")
    # s2 written later → newest first.
    assert [h["session_id"] for h in store.search_sessions("fixme")] == ["s2", "s1"]
