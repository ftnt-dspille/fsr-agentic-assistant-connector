"""Per-session analyst rating (thumbs up/down) for history triage.

The history browser lets an analyst flag sessions worth investigating; the
rating persists on the session store and surfaces in list_sessions so the UI
can filter flagged rows. Storage is importable standalone (no `connectors`
platform import), so we exercise it directly.
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


def test_rate_up_down_roundtrip(store):
    store.append_transcript_user("s1", "Summarize steps on this device")
    store.set_session_rating("s1", "down", "search returned 0")
    assert store.get_session_rating("s1") == {"rating": "down",
                                              "note": "search returned 0"}


def test_rating_surfaces_in_list_sessions(store):
    store.append_transcript_user("s1", "hello")
    store.set_session_rating("s1", "up")
    row = next(r for r in store.list_sessions() if r["session_id"] == "s1")
    assert row["rating"] == {"rating": "up", "note": None}
    assert row["title"] == "hello"


def test_last_write_wins_and_clear(store):
    store.append_transcript_user("s1", "hi")
    store.set_session_rating("s1", "down", "bad")
    store.set_session_rating("s1", "up")           # overwrite
    assert store.get_session_rating("s1")["rating"] == "up"
    store.set_session_rating("s1", "")             # clear
    assert store.get_session_rating("s1") is None


def test_unrated_is_none(store):
    store.append_transcript_user("s1", "hi")
    assert store.get_session_rating("s1") is None
    row = next(r for r in store.list_sessions() if r["session_id"] == "s1")
    assert row["rating"] is None


def test_bad_rating_rejected(store):
    with pytest.raises(ValueError):
        store.set_session_rating("s1", "sideways")
