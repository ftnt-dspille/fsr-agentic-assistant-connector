"""The sessions db default path follows the connector's name, so the rename
orphaned every box's history. A new install must find the old db: copied once,
only when the new file is absent, never touching the old one."""
from __future__ import annotations

import hashlib

import pytest

from connector_fsr_agentic_assistant.storage import Storage

OLD = ".connector_fsr_soc_assistant.db"
NEW = ".connector_fsr_agentic_assistant.db"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("FSR_PLAYBOOK_BUILDER_DB", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _old_db(home):
    st = Storage(db_path=home / OLD)
    st.append_transcript_user("sess-old-1", "an old question")
    return home / OLD


def _ids(st):
    return [r["session_id"] for r in st.list_sessions()]


def test_a_new_install_finds_the_old_sessions_and_leaves_the_old_file_alone(home):
    old = _old_db(home)
    before = hashlib.sha256(old.read_bytes()).hexdigest()
    st = Storage()
    assert st.db_path == home / NEW
    assert _ids(st) == ["sess-old-1"]
    assert hashlib.sha256(old.read_bytes()).hexdigest() == before
    assert not list(home.glob("*.adopt-*"))


def test_it_copies_once_and_never_over_an_existing_new_db(home):
    _old_db(home)
    st = Storage()
    st.append_transcript_user("sess-new-1", "a new question")
    again = Storage()                      # second start: new db exists
    assert sorted(_ids(again)) == ["sess-new-1", "sess-old-1"]
    # a box whose new db already exists (and is empty of the old sessions) is untouched
    (home / NEW).unlink()
    Storage(db_path=home / NEW).append_transcript_user("sess-only-1", "x")
    assert _ids(Storage()) == ["sess-only-1"]


def test_an_explicit_path_or_the_env_override_is_never_adopted_into(home, monkeypatch, tmp_path):
    _old_db(home)
    assert _ids(Storage(db_path=tmp_path / "elsewhere.db")) == []
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "pinned.db"))
    assert _ids(Storage()) == []


def test_no_legacy_db_means_a_fresh_db(home):
    assert _ids(Storage()) == []
