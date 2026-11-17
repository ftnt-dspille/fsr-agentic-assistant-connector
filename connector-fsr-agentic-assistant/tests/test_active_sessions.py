"""Live Sessions panel status derivation (agentic-assistant-monitor §10.2).

`list_active_sessions` must DERIVE a session's status from the connector's
own stores -- never from a stored column that can drift:

  active            -- the session's highest turn_progress turn has no terminal
                      frame (a turn is streaming right now).
  waiting_approval  -- the session is in suspended_sessions (parked at a HITL
                      gate). Wins over `active`.
  idle              -- otherwise.

Storage is importable standalone (no `connectors` platform import), so we
exercise it directly with tmp DBs -- mirroring test_turn_reservation /
test_session_rating.
"""
from __future__ import annotations

import time

from storage import Storage


def _seed(store, sid, *, turn=1, terminal=True, prompt="hi", record_uuid=None,
          record_module=None):
    store.upsert_chat_session(sid, record_uuid=record_uuid,
                              record_module=record_module, current_prompt=prompt)
    store.append_turn_progress(sid, turn, {"type": "turn_start"},
                               terminal=False)
    if terminal:
        store.append_turn_progress(sid, turn, {"type": "stream_end", "ok": True},
                                   terminal=True, stop_reason="end_turn")


def _park(store, sid):
    # suspended_sessions is normally written by PersistedApprovalGateway.stash
    # with a pickled payload; here we only need the session_id row to exist.
    with store._conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO suspended_sessions "
            "(approval_id, session_id, payload, created_at) VALUES (?, ?, ?, ?)",
            ("apr-" + sid, sid, b"x", time.time()),
        )


def test_idle_when_turn_terminal(tmp_path):
    st = Storage(tmp_path / "a.db")
    _seed(st, "s1", terminal=True)
    rows = st.list_active_sessions()
    assert rows[0]["status"] == "idle"
    assert rows[0]["turn_count"] == 1


def test_active_when_turn_in_flight(tmp_path):
    st = Storage(tmp_path / "a.db")
    _seed(st, "s1", terminal=False)  # no terminal frame → streaming
    rows = st.list_active_sessions()
    assert rows[0]["status"] == "active"


def test_only_the_last_turn_decides_active(tmp_path):
    """A turn abandoned mid-stream earlier does not make the session active
    once a later turn finished; an open latest turn does, whatever came
    before. (The query behind this was rewritten for speed; the meaning holds.)"""
    st = Storage(tmp_path / "a.db")
    _seed(st, "done-after-abandoned", turn=1, terminal=False)
    _seed(st, "done-after-abandoned", turn=2, terminal=True)
    _seed(st, "open-after-done", turn=1, terminal=True)
    _seed(st, "open-after-done", turn=2, terminal=False)
    by_id = {r["session_id"]: r["status"] for r in st.list_active_sessions()}
    assert by_id == {"done-after-abandoned": "idle", "open-after-done": "active"}


def test_waiting_approval_wins_over_active(tmp_path):
    st = Storage(tmp_path / "a.db")
    _seed(st, "s1", terminal=False)  # would be active…
    _park(st, "s1")                  # …but it's HITL-parked
    rows = st.list_active_sessions()
    assert rows[0]["status"] == "waiting_approval"


def test_waiting_approval_over_idle(tmp_path):
    st = Storage(tmp_path / "a.db")
    _seed(st, "s1", terminal=True)
    _park(st, "s1")
    assert st.list_active_sessions()[0]["status"] == "waiting_approval"


def test_newest_first_by_last_activity(tmp_path):
    st = Storage(tmp_path / "a.db")
    _seed(st, "old", turn=1)
    time.sleep(0.02)
    _seed(st, "new", turn=1)
    rows = st.list_active_sessions()
    assert rows[0]["session_id"] == "new"
    assert rows[1]["session_id"] == "old"
    # last_activity prefers the streaming-feed watermark.
    assert rows[0]["last_activity"] >= rows[1]["last_activity"]


def test_upsert_started_at_first_writer_wins(tmp_path):
    st = Storage(tmp_path / "a.db")
    st.upsert_chat_session("s1", current_prompt="first")
    started = st.list_active_sessions()[0]["started_at"]
    time.sleep(0.02)
    st.upsert_chat_session("s1", record_module="alerts", current_prompt="second")
    row = st.list_active_sessions()[0]
    assert row["started_at"] == started          # unchanged
    assert row["current_prompt"] == "second"     # last-writer-wins
    assert row["record_module"] == "alerts"


def test_carries_user_record_and_turn_count(tmp_path):
    st = Storage(tmp_path / "a.db")
    _seed(st, "s1", turn=3, record_uuid="rec-123", record_module="alerts",
          prompt="summarize this alert")
    st.set_session_initiator("s1", {"name": "alice", "iri": "/api/3/people/a1"})
    row = st.list_active_sessions()[0]
    assert row["user"] == {"name": "alice", "iri": "/api/3/people/a1"}
    assert row["record_uuid"] == "rec-123"
    assert row["record_module"] == "alerts"
    assert row["current_prompt"] == "summarize this alert"
    assert row["turn_count"] == 3


def test_limit_caps_rows(tmp_path):
    st = Storage(tmp_path / "a.db")
    for i in range(5):
        _seed(st, f"s{i}", turn=1)
    assert len(st.list_active_sessions(limit=3)) == 3


def test_empty_when_no_sessions(tmp_path):
    st = Storage(tmp_path / "a.db")
    assert st.list_active_sessions() == []


# ── Filtering (monitor drill-downs) ────────────────────────────────────────
# The widget's "Pending HITL" card and per-user rows narrow this list. The op
# used to accept only `limit` and silently ignored everything else, so the UI
# showed a `status: waiting_approval` chip above a list of purely idle rows.


def test_status_filter_narrows_to_matching_sessions(tmp_path):
    st = Storage(tmp_path / "f.db")
    _seed(st, "idle1", terminal=True)
    _seed(st, "park1", terminal=True)
    _park(st, "park1")
    rows = st.list_active_sessions(status="waiting_approval")
    assert [r["session_id"] for r in rows] == ["park1"]
    assert st.list_active_sessions(status="idle")[0]["session_id"] == "idle1"
    assert st.list_active_sessions(status="active") == []


def test_status_filter_applies_after_limit_not_before(tmp_path):
    # The one match is the OLDEST session, so a SQL-side `LIMIT 2` applied
    # before the predicate would slice it off and return nothing.
    st = Storage(tmp_path / "g.db")
    _seed(st, "old_parked", terminal=True)
    _park(st, "old_parked")
    for i in range(10):
        _seed(st, f"newer{i}", terminal=True)
    rows = st.list_active_sessions(limit=2, status="waiting_approval")
    assert [r["session_id"] for r in rows] == ["old_parked"]


def test_limit_still_caps_the_filtered_result(tmp_path):
    st = Storage(tmp_path / "h.db")
    for i in range(5):
        _seed(st, f"s{i}", terminal=True)
    assert len(st.list_active_sessions(limit=3, status="idle")) == 3


def test_unfiltered_list_is_unchanged(tmp_path):
    st = Storage(tmp_path / "i.db")
    _seed(st, "a", terminal=True)
    _seed(st, "b", terminal=True)
    assert len(st.list_active_sessions()) == 2


def test_status_param_does_not_clobber_derived_status(tmp_path):
    # `status` is both the filter param and the loop's derived-status local.
    # If they share a name the first row's derivation overwrites the filter and
    # every later row matches whatever the first one happened to be.
    st = Storage(tmp_path / "j.db")
    _seed(st, "p1", terminal=True)
    _park(st, "p1")
    _seed(st, "p2", terminal=True)
    _park(st, "p2")
    _seed(st, "i1", terminal=True)
    rows = st.list_active_sessions(status="waiting_approval")
    assert sorted(r["session_id"] for r in rows) == ["p1", "p2"]


def test_created_records_are_per_session_and_in_order(tmp_path):
    st = Storage(tmp_path / "a.db")
    st.add_session_record("s1", "ztpf_templates", "/api/3/ztpf_templates/a", "A")
    st.add_session_record("s1", "ztpf_templates", "/api/3/ztpf_templates/b", "B")
    st.add_session_record("s1", "ztpf_templates", "/api/3/ztpf_templates/a", "A2")
    st.add_session_record("s2", "alerts", "/api/3/alerts/x", None)
    assert st.get_session_records("s1") == [
        {"module": "ztpf_templates", "iri": "/api/3/ztpf_templates/a", "name": "A2"},
        {"module": "ztpf_templates", "iri": "/api/3/ztpf_templates/b", "name": "B"}]
    assert st.get_session_records("nobody") == []
