"""A choice card's answer is stored, so a reloaded chat shows it as answered.

Live: the analyst picked "Start from an alert" on the "what should this
playbook start from?" card; after a reload the card came back open, as if
unanswered -- the pick lived only on the card object in the browser.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def storage(tmp_path):
    from connector_fsr_agentic_assistant.storage import Storage
    return Storage(db_path=tmp_path / "test.db")


def _ops():
    from connector_fsr_agentic_assistant import operations
    return operations


CARD = {"type": "choice_card", "id": "scope",
        "options": [{"label": "Start from an alert", "value": "alert_start"},
                    {"label": "Start from an incident", "value": "incident_start"}]}


def test_answer_row_replays_between_the_turns(storage):
    storage.append_transcript_user("s1", "I want to create a new playbook.")
    storage.append_transcript_assistant("s1", [CARD])
    storage.append_transcript_resume("s1", "scope", "alert_start",
                                     label="Start from an alert",
                                     author={"name": "CS Admin"})
    storage.append_transcript_assistant("s1", [{"type": "text", "text": "ok"}])
    turns = storage.list_transcript("s1")
    assert [next(k for k in ("user", "transcript", "resume") if k in t)
            for t in turns] == ["user", "transcript", "resume", "transcript"]
    assert turns[2]["resume"] == {"choice_id": "scope", "value": "alert_start",
                                  "label": "Start from an alert"}
    assert turns[2]["author"] == {"name": "CS Admin"}


def test_answer_row_is_not_a_user_message(storage):
    """Titles, initiator fallback and search read role='user' rows."""
    storage.append_transcript_resume("s1", "scope", "typed words")
    storage.append_transcript_user("s1", "the real opener")
    rows = [t for t in storage.list_transcript("s1") if "user" in t]
    assert rows == [{"user": "the real opener", "ts": rows[0]["ts"]}]


@pytest.mark.parametrize("value,label", [
    ("alert_start", "Start from an alert"),
    (["alert_start", "incident_start"], "Start from an alert · Start from an incident"),
    ("enrich alert IPs on VirusTotal", None),          # typed text needs no label
    (["alert_start", "typed"], None),                  # partial match: no fake label
])
def test_label_is_the_option_label_or_none_for_typed_text(value, label):
    assert _ops()._choice_answer_label(CARD, value) == label


def test_record_choice_answer_reads_the_stored_card(storage):
    storage.save_card("s1", CARD)
    _ops()._record_choice_answer(storage, "s1", "scope", "incident_start",
                                 {"name": "CS Admin"})
    (row,) = [t for t in storage.list_transcript("s1") if "resume" in t]
    assert row["resume"]["label"] == "Start from an incident"


def test_record_choice_answer_never_raises():
    class Broken:
        def get_card(self, *a):
            raise RuntimeError("db gone")
    _ops()._record_choice_answer(Broken(), "s1", "scope", "x", None)
