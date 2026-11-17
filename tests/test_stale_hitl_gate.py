"""A HITL gate the analyst walked away from must stop reading as pending.

`suspended_sessions` rows are only removed when the approval is actually
answered (`SuspendedStore.pop`) or when they age out at TTL. An analyst who
ignores the gate and simply types the next message leaves the row behind, so
both the Live Sessions panel (`Storage.list_active_sessions`) and the Pending
HITL card (`operations.list_pending`) used to report a session as parked with
nothing pending in it. Later turn activity is the signal that the conversation
ran on past the gate.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def storage(tmp_path):
    from connector_fsr_agentic_assistant.storage import Storage
    return Storage(db_path=tmp_path / "gate.db")


def _park(storage, session_id: str, approval_id: str, at: float) -> None:
    """Write a suspended row directly. The real path pickles a bound
    SuspendedSession, but nothing under test unpickles it -- status derivation
    reads only (session_id, created_at)."""
    with storage._conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO suspended_sessions "
            "(approval_id, session_id, payload, created_at) VALUES (?,?,?,?)",
            (approval_id, session_id, b"", at),
        )


def _activity(storage, session_id: str, turn: int, at: float) -> None:
    """A turn_progress frame stamped at `at` (the writer always stamps now())."""
    with storage._conn() as c:
        c.execute(
            "INSERT INTO turn_progress "
            "(session_id, turn, frame_json, is_terminal, stop_reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, turn, "{}", 1, "end_turn", at),
        )


def _status(storage, session_id: str) -> str | None:
    for s in storage.list_active_sessions(limit=50):
        if s["session_id"] == session_id:
            return s["status"]
    return None


# ── the unit that decides it ──────────────────────────────────────────────

def test_gate_with_no_later_activity_is_live():
    from connector_fsr_agentic_assistant.storage import _gate_is_live
    now = time.time()
    assert _gate_is_live(now, now - 5) is True


def test_gate_is_live_when_the_session_has_no_frames_at_all():
    """Fail open: hiding a real pending approval is worse than showing a stale
    one, so an unknown last-activity leaves the gate standing."""
    from connector_fsr_agentic_assistant.storage import _gate_is_live
    assert _gate_is_live(time.time(), None) is True


def test_activity_inside_the_grace_window_does_not_kill_the_gate():
    """Suspending a turn writes its own terminal frame at essentially the same
    instant as the suspended row; that must not read as 'moved on'."""
    from connector_fsr_agentic_assistant.storage import _gate_is_live
    now = time.time()
    assert _gate_is_live(now, now + 1) is True


def test_activity_past_the_grace_window_kills_the_gate():
    from connector_fsr_agentic_assistant.storage import (
        _GATE_GRACE_SECONDS, _gate_is_live,
    )
    now = time.time()
    assert _gate_is_live(now, now + _GATE_GRACE_SECONDS + 30) is False


def test_no_gate_is_never_live():
    from connector_fsr_agentic_assistant.storage import _gate_is_live
    assert _gate_is_live(None, time.time()) is False


# ── Live Sessions panel ───────────────────────────────────────────────────

def test_live_gate_reports_waiting_approval(storage):
    now = time.time()
    storage.upsert_chat_session("sess-live", current_prompt="block it?")
    _activity(storage, "sess-live", 1, now)
    _park(storage, "sess-live", "appr-live", now)
    assert _status(storage, "sess-live") == "waiting_approval"


def test_abandoned_gate_falls_back_to_idle(storage):
    """The reported bug: the analyst ignored the approval and typed the next
    message, so the session is idle -- not pending."""
    now = time.time()
    storage.upsert_chat_session("sess-stale", current_prompt="never mind")
    _park(storage, "sess-stale", "appr-stale", now - 600)
    _activity(storage, "sess-stale", 1, now - 600)   # the gate's own frame
    _activity(storage, "sess-stale", 2, now)         # the analyst moved on
    assert _status(storage, "sess-stale") == "idle"


def test_status_filter_excludes_an_abandoned_gate(storage):
    """Drilling in from the Pending HITL card must not surface stale rows."""
    now = time.time()
    storage.upsert_chat_session("sess-stale", current_prompt="never mind")
    _park(storage, "sess-stale", "appr-stale", now - 600)
    _activity(storage, "sess-stale", 1, now - 600)
    _activity(storage, "sess-stale", 2, now)
    rows = storage.list_active_sessions(limit=50, status="waiting_approval")
    assert [r["session_id"] for r in rows] == []


# ── Pending HITL card ─────────────────────────────────────────────────────

def test_list_pending_drops_the_abandoned_gate_and_keeps_the_live_one(
    storage, monkeypatch,
):
    from connector_fsr_agentic_assistant import operations, storage as storage_mod
    monkeypatch.setattr(storage_mod, "default_storage", lambda: storage)

    now = time.time()
    storage.upsert_chat_session("sess-live")
    _activity(storage, "sess-live", 1, now)
    _park(storage, "sess-live", "appr-live", now)

    storage.upsert_chat_session("sess-stale")
    _park(storage, "sess-stale", "appr-stale", now - 600)
    _activity(storage, "sess-stale", 1, now - 600)
    _activity(storage, "sess-stale", 2, now)

    pending = operations.list_pending({}, {})["pending"]
    assert [p["session_id"] for p in pending] == ["sess-live"]


# ── the central queue: stored approval cards ─────────────────────────────

def _card(storage, session_id, card, at):
    with storage._conn() as c:
        c.execute("INSERT OR REPLACE INTO session_cards (session_id, card_id, card_json, "
                  "created_at) VALUES (?,?,?,?)",
                  (session_id, card["id"], __import__("json").dumps(card), at))


_UPDATE = {"type": "action_card", "id": "card-1", "approval_id": "card-1",
           "tool": "update_record", "summary": "Update ztpf_templates/7d762bbe",
           "target": {"module": "ztpf_templates", "uuid": "7d762bbe", "name": "Loopback"},
           "args": {"script": "x" * 2000},
           "changes": [{"field": "script", "before": "old", "after": "x" * 2000}]}


@pytest.fixture
def queue(storage, monkeypatch):
    from connector_fsr_agentic_assistant import operations, storage as storage_mod
    monkeypatch.setattr(storage_mod, "default_storage", lambda: storage)
    return lambda: operations.list_pending({}, {})["pending"]


def test_a_card_past_the_suspend_ttl_is_still_in_the_queue_with_what_it_asks(storage, queue):
    """Approving executes the stored card, so it stays answerable after the
    suspended row expires; a reviewer outside the chat sees what it changes."""
    now = time.time()
    _activity(storage, "s1", 1, now - 3600)
    _card(storage, "s1", _UPDATE, now - 3600)
    [p] = queue()
    assert p["card_id"] == "card-1" and p["card"]["tool"] == "update_record"
    assert p["card"]["target"]["name"] == "Loopback"
    assert p["card"]["title"] == "Change script on Loopback"
    ch = p["card"]["changes"][0]
    assert ch["before"] == "old" and ch["after"].endswith("(2000 chars)")


def test_a_decided_card_leaves_the_queue(storage, queue):
    now = time.time()
    _card(storage, "s1", _UPDATE, now)
    storage.claim_card_decision("s1", "card-1", "approve", "Lead")
    assert queue() == []


def test_a_day_old_card_and_non_approval_cards_are_not_pending(storage, queue):
    now = time.time()
    _card(storage, "s1", _UPDATE, now - 2 * 86400)
    _card(storage, "s2", {"type": "playbook_offer", "id": "offer-1"}, now)
    _card(storage, "s3", {"type": "choice", "id": "ch-1"}, now)
    assert queue() == []


def test_a_suspended_gate_and_its_card_are_one_entry(storage, queue):
    now = time.time()
    _activity(storage, "s1", 1, now)
    _park(storage, "s1", "card-1", now)
    _card(storage, "s1", _UPDATE, now)
    [p] = queue()
    assert p["approval_id"] == "card-1" and p["card"] is not None
