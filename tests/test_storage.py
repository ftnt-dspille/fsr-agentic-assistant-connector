"""Tests for the connector's sqlite storage + PersistedApprovalGateway."""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def storage(tmp_path):
    from connector_fsr_agentic_assistant.storage import Storage
    return Storage(db_path=tmp_path / "test.db")


@pytest.fixture
def suspended():
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    s = SuspendedSession(
        approval_id="appr-123", session_id="sess-1", tool="run_op",
        tool_use_id="tu-1", args={"connector": "x", "op": "y"},
        tier=3, history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys prompt", tags={},
    )
    # Production binds the HMAC token before stash (anthropic/openai provider);
    # the PersistedApprovalGateway's S2 verify gate fails closed without it.
    bind(s)
    return s


# --- history sink ---------------------------------------------------------

def test_record_chat_turn_roundtrip(storage):
    storage.record_chat_turn({
        "session": "s1", "turn": 1, "model": "claude-x",
        "input_tokens": 100, "output_tokens": 50,
        "stop_reason": "end_turn", "tags": {"intent": "compose"},
        "tool_calls": [{"name": "validate_yaml"}],
    })
    rows = storage.list_turns("s1")
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-x"
    assert rows[0]["tags"] == {"intent": "compose"}
    assert rows[0]["tool_calls"] == [{"name": "validate_yaml"}]


def test_next_turn_idx_increments_without_usage_row(storage):
    # Regression: turn_idx must NOT be derived from chat_turns alone. A turn
    # that fails before any UsageEvent (e.g. LLM gateway down) writes a
    # turn_start frame but no chat_turns row; the NEXT turn must still get a
    # fresh, strictly-greater turn_idx so the widget's `since_turn` poll fence
    # doesn't exclude the live turn (turn=None, no live messages).
    assert storage.next_turn_idx("s1") == 1
    # Turn 1 only ever writes a turn_start frame (no usage → no chat_turns row).
    storage.append_turn_progress("s1", 1, {"type": "turn_start", "turn": 1})
    assert storage.next_turn_idx("s1") == 2
    # A later turn that DOES record usage still advances normally.
    storage.append_turn_progress("s1", 2, {"type": "turn_start", "turn": 2})
    storage.record_chat_turn({"session": "s1", "turn": 2, "model": "m",
                              "input_tokens": 1, "output_tokens": 1,
                              "stop_reason": "end_turn"})
    assert storage.next_turn_idx("s1") == 3
    # Isolated per session.
    assert storage.next_turn_idx("other") == 1


# --- render-fidelity transcript (refresh durability) ---------------------

def _exchange(turns):
    """The exchange itself, without the `ts`/`author` envelope.

    Entries carry a send time (and, for user rows, the real sender) alongside
    the content. Those are asserted directly in
    `test_transcript_carries_send_time_and_author`; every other test here is
    about ordering/isolation/limit, so it compares content only rather than
    re-pinning a timestamp it doesn't care about.
    """
    return [{k: v for k, v in t.items() if k not in ("ts", "author")}
            for t in turns]


def test_transcript_roundtrip_chronological(storage):
    storage.append_transcript_user("s1", "first q")
    storage.append_transcript_assistant("s1", [{"type": "text", "text": "first a"}])
    storage.append_transcript_user("s1", "second q")
    storage.append_transcript_assistant(
        "s1", [{"type": "tool_use", "name": "get_record"},
               {"type": "text", "text": "second a"}])
    turns = storage.list_transcript("s1")
    assert _exchange(turns) == [
        {"user": "first q"},
        {"transcript": [{"type": "text", "text": "first a"}]},
        {"user": "second q"},
        {"transcript": [{"type": "tool_use", "name": "get_record"},
                        {"type": "text", "text": "second a"}]},
    ]


def test_transcript_carries_send_time_and_author(storage):
    """Every entry carries its ORIGINAL send time, and a user entry carries the
    real sender — so a rehydrated chat renders when it actually happened and who
    actually said it, not the refresh time and the current viewer.

    `author` is absent (not null) on rows written without one: the connector
    runs as the service account and cannot derive a sender, so pre-actor rows
    must fall through to history's neutral label rather than claim an identity.
    """
    who = {"name": "Ada L", "iri": "/api/3/people/1"}
    storage.append_transcript_user("s1", "with author", author=who)
    storage.append_transcript_user("s1", "no author")
    storage.append_transcript_assistant("s1", [{"type": "text", "text": "a"}])
    authored, anon, assistant = storage.list_transcript("s1")

    assert authored["author"] == who
    assert "author" not in anon
    # Assistant entries are timestamped too (the widget renders both sides).
    for entry in (authored, anon, assistant):
        assert isinstance(entry["ts"], (int, float))
    # Chronological, and time does not run backwards across the exchange.
    assert authored["ts"] <= anon["ts"] <= assistant["ts"]


def test_transcript_empty_is_skipped(storage):
    # Opener turns (no user text) and empty transcripts write nothing.
    storage.append_transcript_user("s1", "")
    storage.append_transcript_assistant("s1", [])
    assert storage.list_transcript("s1") == []


def test_list_sessions_newest_first_with_title(storage):
    # Two sessions; the second is more recently active and should sort first.
    storage.append_transcript_user("old", "how do I block an IP?")
    storage.append_transcript_assistant("old", [{"type": "text", "text": "use the firewall"}])
    storage.append_transcript_user("new", "is 1.2.3.4 malicious?")
    sessions = storage.list_sessions()
    assert [s["session_id"] for s in sessions] == ["new", "old"]
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["old"]["title"] == "how do I block an IP?"
    assert by_id["old"]["entries"] == 2
    assert by_id["new"]["title"] == "is 1.2.3.4 malicious?"
    assert by_id["new"]["last_activity"] >= by_id["old"]["last_activity"]


def test_list_sessions_placeholder_title_when_no_user_text(storage):
    # An opener-only session (assistant transcript, no user row) still lists,
    # with a placeholder title.
    storage.append_transcript_assistant("opener", [{"type": "text", "text": "hello"}])
    sessions = storage.list_sessions()
    assert sessions[0]["session_id"] == "opener"
    assert sessions[0]["title"] == "(no message)"


def test_list_sessions_limit(storage):
    for i in range(5):
        storage.append_transcript_user(f"s{i}", f"q{i}")
    assert len(storage.list_sessions(limit=3)) == 3


def test_transcript_isolated_by_session(storage):
    storage.append_transcript_user("a", "alpha")
    storage.append_transcript_user("b", "beta")
    assert _exchange(storage.list_transcript("a")) == [{"user": "alpha"}]
    assert _exchange(storage.list_transcript("b")) == [{"user": "beta"}]


def test_transcript_limit_keeps_most_recent_in_order(storage):
    for i in range(5):
        storage.append_transcript_user("s1", f"q{i}")
    turns = storage.list_transcript("s1", limit=2)
    # Most recent two, still chronological.
    assert _exchange(turns) == [{"user": "q3"}, {"user": "q4"}]


def test_active_session_upsert(storage):
    storage.write_active_session("s1")
    storage.write_active_session("s2")  # replaces, doesn't append
    with storage._conn() as c:
        rows = c.execute("SELECT session_id FROM active_session").fetchall()
    assert rows == [("s2",)]


def test_record_chat_message_pk_collision_replaces(storage):
    storage.record_chat_message("s1", 1, 0, kind="user", content="hi")
    storage.record_chat_message("s1", 1, 0, kind="user", content="updated")
    with storage._conn() as c:
        row = c.execute(
            "SELECT content FROM chat_messages WHERE session_id=? AND turn=? AND seq=?",
            ("s1", 1, 0),
        ).fetchone()
    assert row[0] == "updated"


# --- turn progress feed (action-based streaming, contract 2.5.0) ----------

def test_turn_progress_cursor_pagination(storage):
    storage.append_turn_progress("s1", 1, {"type": "turn_start", "turn": 1})
    storage.append_turn_progress("s1", 1, {"type": "text", "text": "I'll help"})
    storage.append_turn_progress("s1", 1, {"type": "tool_use", "name": "run_op"})

    first = storage.read_turn_progress("s1", 0)
    assert first["turn"] == 1
    assert first["done"] is False
    assert [f["type"] for f in first["frames"]] == ["turn_start", "text", "tool_use"]
    assert all("cursor" in f for f in first["frames"])

    # Re-poll from the watermark: no new frames.
    again = storage.read_turn_progress("s1", first["cursor"])
    assert again["frames"] == []
    assert again["cursor"] == first["cursor"]


def test_turn_progress_terminal_done(storage):
    storage.append_turn_progress("s1", 1, {"type": "text", "text": "hi"})
    storage.append_turn_progress(
        "s1", 1, {"type": "stream_end", "stop_reason": "awaiting_action_card"},
        terminal=True, stop_reason="awaiting_action_card")

    res = storage.read_turn_progress("s1", 0)
    assert res["done"] is True
    assert res["stop_reason"] == "awaiting_action_card"

    # A final empty poll past the terminal frame still reports done.
    after = storage.read_turn_progress("s1", res["cursor"])
    assert after["frames"] == []
    assert after["done"] is True
    assert after["stop_reason"] == "awaiting_action_card"


def test_turn_progress_scopes_to_latest_turn(storage):
    # Turn 1 finished; turn 2 starts. A poll from cursor 0 must only see
    # turn 2's frames, never replay turn 1.
    storage.append_turn_progress("s1", 1, {"type": "text", "text": "old"})
    storage.append_turn_progress(
        "s1", 1, {"type": "stream_end", "stop_reason": "end_turn"},
        terminal=True, stop_reason="end_turn")
    storage.append_turn_progress("s1", 2, {"type": "turn_start", "turn": 2})
    storage.append_turn_progress("s1", 2, {"type": "text", "text": "new"})

    res = storage.read_turn_progress("s1", 0)
    assert res["turn"] == 2
    assert res["done"] is False
    assert [f.get("text") for f in res["frames"] if f["type"] == "text"] == ["new"]


def test_turn_progress_empty_session(storage):
    res = storage.read_turn_progress("nope", 0)
    assert res == {"frames": [], "cursor": 0, "turn": None,
                   "done": False, "stop_reason": None, "last_activity": None}


def test_turn_progress_since_turn_fences_completed_prior_turn(storage):
    # The stale-replay race: turn 1 finished (terminal, done=true). The widget
    # fires the turn-2 build and its first poll RACES chat_turn's prologue —
    # turn 2's turn_start isn't written yet, so the only turn in the feed is the
    # completed turn 1. Without a fence the poll returns turn 1 done=true and the
    # widget commits the prior enrichment as the build (export sess-ei6esw96).
    storage.append_turn_progress("s1", 1, {"type": "turn_start", "turn": 1})
    storage.append_turn_progress("s1", 1, {"type": "text", "text": "enrichment"})
    storage.append_turn_progress(
        "s1", 1, {"type": "stream_end", "stop_reason": "end_turn",
                  "transcript": [{"type": "text", "text": "IOC clean"}]},
        terminal=True, stop_reason="end_turn")

    # Legacy unfenced poll (since_turn=0) still sees the completed turn — the bug.
    assert storage.read_turn_progress("s1", 0)["turn"] == 1

    # Fenced poll for turn > 1: the completed turn 1 is excluded. Until turn 2's
    # first frame lands, there's nothing to serve → turn=None, done=false, so the
    # widget just keeps polling instead of committing the stale turn.
    fenced = storage.read_turn_progress("s1", 0, since_turn=1)
    assert fenced["turn"] is None
    assert fenced["done"] is False
    assert fenced["frames"] == []

    # Once turn 2 actually starts, the fenced poll picks it up (and never replays
    # turn 1's frames).
    storage.append_turn_progress("s1", 2, {"type": "turn_start", "turn": 2})
    storage.append_turn_progress("s1", 2, {"type": "text", "text": "building"})
    after = storage.read_turn_progress("s1", 0, since_turn=1)
    assert after["turn"] == 2
    assert after["done"] is False
    assert [f.get("text") for f in after["frames"] if f["type"] == "text"] == ["building"]


# --- approval gateway -----------------------------------------------------

def test_persisted_gateway_stash_pop_singleuse(storage, suspended):
    from connector_fsr_agentic_assistant.storage import PersistedApprovalGateway
    gw = PersistedApprovalGateway(storage)
    gw.stash(suspended)
    got = gw.pop(suspended.approval_id)
    assert got is not None
    assert got.approval_id == suspended.approval_id
    assert got.args == {"connector": "x", "op": "y"}
    # Single-use: a second pop returns nothing.
    assert gw.pop(suspended.approval_id) is None


def test_persisted_gateway_peek_nondestructive(storage, suspended):
    from connector_fsr_agentic_assistant.storage import PersistedApprovalGateway
    gw = PersistedApprovalGateway(storage)
    gw.stash(suspended)
    assert gw.peek(suspended.approval_id) is not None
    assert gw.peek(suspended.approval_id) is not None  # still there
    assert gw.pop(suspended.approval_id) is not None


def test_persisted_gateway_expired_session(storage, suspended):
    from connector_fsr_agentic_assistant.storage import PersistedApprovalGateway
    gw = PersistedApprovalGateway(storage)
    suspended.created_at = time.time() - 7200  # 2hr ago, well past TTL
    gw.stash(suspended)
    # _gc on the next call should evict; pop returns None
    assert gw.pop(suspended.approval_id) is None


def test_persisted_gateway_survives_new_instance(storage, suspended):
    """The whole point of PersistedApprovalGateway vs the in-memory
    one — paused turns survive a process / worker restart."""
    from connector_fsr_agentic_assistant.storage import (
        PersistedApprovalGateway, Storage,
    )
    gw1 = PersistedApprovalGateway(storage)
    gw1.stash(suspended)
    # New Storage + new gateway, same on-disk DB.
    storage2 = Storage(db_path=storage.db_path)
    gw2 = PersistedApprovalGateway(storage2)
    got = gw2.pop(suspended.approval_id)
    assert got is not None
    assert got.approval_id == suspended.approval_id


def test_persisted_gateway_satisfies_protocol(storage):
    """Structural conformance check against fsr_playbooks.protocols.ApprovalGateway."""
    from fsr_playbooks.protocols import ApprovalGateway  # noqa: F401
    from connector_fsr_agentic_assistant.storage import PersistedApprovalGateway
    gw = PersistedApprovalGateway(storage)
    # Protocol is structural; this asserts the methods exist with the
    # expected names. Mypy/pyright will do the static check; here we
    # just confirm callable attributes are present.
    for name in ("stash", "peek", "pop", "clear"):
        assert callable(getattr(gw, name))


# --- session trace (SKILL_BASED_PLAYBOOK_PLAN §2) -------------------------

def test_session_trace_roundtrip(storage):
    assert storage.get_session_trace("s-trace") is None
    storage.set_session_trace("s-trace", '{"calls": []}')
    assert storage.get_session_trace("s-trace") == '{"calls": []}'
    # Overwrite (sessions accumulate across turns).
    storage.set_session_trace("s-trace", '{"calls": [1]}')
    assert storage.get_session_trace("s-trace") == '{"calls": [1]}'


def test_session_trace_scope_installs_and_persists(storage):
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_playbooks.agent import skill_trace

    skill_trace.clear_active_trace()
    with ops._session_trace_scope(storage, "sess-A") as trace:
        # The active recorder is installed for the turn.
        assert skill_trace.get_active_trace() is trace
        skill_trace.record_run_op("virustotal", "get_ip",
                                  {"ip": "1.2.3.4"}, {"verdict": "clean"})
    # Cleared after the turn, and persisted to storage.
    assert skill_trace.get_active_trace() is None
    saved = storage.get_session_trace("sess-A")
    assert saved is not None
    rt = skill_trace.SkillTrace.from_json(saved)
    assert len(rt) == 1
    assert rt.calls[0].observed_output == {"verdict": "clean"}


def test_session_trace_scope_stamps_and_persists_module(storage):
    """The triaged record's module, stamped inside the scope, survives to
    storage so the trace-build path binds the start trigger to it."""
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_playbooks.agent import skill_trace

    skill_trace.clear_active_trace()
    with ops._session_trace_scope(storage, "sess-M"):
        skill_trace.set_active_trace_module("/api/3/alerts/abc-123")
        skill_trace.record_run_op("virustotal", "get_ip", {"ip": "1.2.3.4"}, {})
    rt = skill_trace.SkillTrace.from_json(storage.get_session_trace("sess-M"))
    assert rt.module == "alerts"  # normalized from the IRI, persisted


def test_session_trace_scope_accumulates_across_turns(storage):
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_playbooks.agent import skill_trace

    with ops._session_trace_scope(storage, "sess-B"):
        skill_trace.record_run_op("c", "first", {"x": "seed_value"}, {})
    with ops._session_trace_scope(storage, "sess-B"):
        # Rehydrates the prior call, then adds another.
        skill_trace.record_run_op("c", "second", {"y": "next_value"}, {})
    rt = skill_trace.SkillTrace.from_json(storage.get_session_trace("sess-B"))
    assert [c.step_name for c in rt.calls] == ["First", "Second"]


# --- case state (CASE_STATE_SPINE_DESIGN P1/P2) ----------------------------

def test_case_state_save_load_roundtrip(storage):
    """Save and recover case state as a dict."""
    state = {"case_id": "case-1", "verdict": "suspicious", "tags": ["urgent"]}
    storage.save_case_state("s-case", state)
    recovered = storage.get_case_state("s-case")
    assert recovered == state


def test_case_state_missing_returns_none(storage):
    """A session with no case state row returns None."""
    assert storage.get_case_state("s-nonexistent") is None


def test_case_state_corrupt_json_returns_none(storage):
    """Corrupt state_json (invalid JSON) fails open and returns None."""
    with storage._conn() as c:
        c.execute(
            "INSERT INTO session_case_state "
            "(session_id, state_json, updated_at) VALUES (?, ?, ?)",
            ("s-corrupt", "not valid json {{{", time.time()),
        )
    # get_case_state should return None rather than raising
    assert storage.get_case_state("s-corrupt") is None


def test_case_state_overwrite_updates(storage):
    """Overwriting case state replaces the prior dict."""
    storage.save_case_state("s-update", {"v": 1})
    storage.save_case_state("s-update", {"v": 2, "extra": "field"})
    recovered = storage.get_case_state("s-update")
    assert recovered == {"v": 2, "extra": "field"}


def test_case_state_complex_nested_dict(storage):
    """Case state with nested structures (lists, dicts) round-trips."""
    state = {
        "case_id": "c-123",
        "events": [
            {"type": "created", "timestamp": 1234567890.0},
            {"type": "updated", "timestamp": 1234567900.0},
        ],
        "metadata": {
            "severity": "high",
            "tactics": ["execution", "persistence"],
            "nested_deeper": {"level": 3, "value": None},
        },
    }
    storage.save_case_state("s-complex", state)
    recovered = storage.get_case_state("s-complex")
    assert recovered == state


def test_case_state_isolated_by_session(storage):
    """Case state is isolated per session_id."""
    storage.save_case_state("s-a", {"id": "a"})
    storage.save_case_state("s-b", {"id": "b"})
    assert storage.get_case_state("s-a") == {"id": "a"}
    assert storage.get_case_state("s-b") == {"id": "b"}
