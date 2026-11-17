"""Phase 0.5 — session-lifecycle integration + the audit's untested corners.

The connector's session/state code is defensively solid (atomic turn
reservation, real-sid threading, fail-closed gates), but the audit found the
FULL lifecycle is never exercised as one flow, and three corners have no test:
concurrent turn minting under real contention, cross-worker recovery, and
corrupt/diverged persisted state. This file closes that gap.

Network-free — pure Storage against a temp DB, no LLM.
"""
from __future__ import annotations

import threading

import pytest


def _storage(tmp_path, name="s.db"):
    from connector_fsr_agentic_assistant.storage import Storage
    return Storage(db_path=tmp_path / name)


@pytest.fixture
def storage(tmp_path):
    return _storage(tmp_path)


@pytest.fixture
def suspended():
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    s = SuspendedSession(
        approval_id="appr-life-1", session_id="sess-life", tool="run_op",
        tool_use_id="tu-1", args={"connector": "x", "op": "y"},
        tier=3, history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys", tags={},
    )
    bind(s)
    return s


# ---------------------------------------------------------------------------
# 1. Full lifecycle as ONE flow, verified cross-instance (cold worker)
# ---------------------------------------------------------------------------
def test_full_lifecycle_open_turn_card_resume_next(tmp_path, suspended):
    """open → reserve turn → save conversation → emit a gating card → suspend
    for approval → a DIFFERENT Storage instance (cold worker) resumes: pops the
    suspended session once, reads the card + conversation, reserves the next
    turn without colliding."""
    sid = "sess-life"
    hot = _storage(tmp_path)
    from connector_fsr_agentic_assistant.storage import PersistedApprovalGateway

    # -- turn 1 on the "hot" worker --
    hot.write_active_session(sid)
    t1 = hot.reserve_next_turn(sid)
    assert t1 == 1
    hot.save_conversation(sid, [{"role": "user", "content": "block the C2 IP"}])
    hot.set_session_intent(sid, "triage")
    hot.set_session_module(sid, "alerts")
    card = {"id": "card-1", "type": "action_card", "tier": 3,
            "action": "block_ip", "args": {"ip": "1.2.3.4"}}
    hot.save_card(sid, card)
    PersistedApprovalGateway(hot).stash(suspended)

    # -- a cold worker (fresh Storage, same file) resumes --
    cold = _storage(tmp_path)
    gw = PersistedApprovalGateway(cold)
    popped = gw.pop("appr-life-1")
    assert popped is not None and popped.session_id == sid
    assert gw.pop("appr-life-1") is None, "suspended session must be single-use"

    assert cold.get_card(sid, "card-1")["action"] == "block_ip"
    assert cold.load_conversation(sid)[0]["content"] == "block the C2 IP"
    assert cold.get_session_intent(sid) == "triage"
    assert cold.get_session_module(sid) == "alerts"

    # -- next turn reserves 2, not a duplicate 1 --
    t2 = cold.reserve_next_turn(sid)
    assert t2 == 2


# ---------------------------------------------------------------------------
# 2. Concurrent turn minting — the BEGIN IMMEDIATE atomicity, under real
#    thread contention across SEPARATE Storage instances (no shared lock).
# ---------------------------------------------------------------------------
def test_concurrent_reserve_never_mints_duplicate_turns(tmp_path):
    sid = "sess-concurrent"
    _storage(tmp_path).write_active_session(sid)  # ensure schema + row

    n_threads, per_thread = 6, 15
    results: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker():
        st = _storage(tmp_path)  # separate instance → only SQLite's lock serializes
        try:
            barrier.wait()
            for _ in range(per_thread):
                idx = st.reserve_next_turn(sid)
                with lock:
                    results.append(idx)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"reserve raised under contention: {errors[:2]}"
    total = n_threads * per_thread
    assert len(results) == total
    assert len(set(results)) == total, \
        f"duplicate turn indices minted: {sorted(results)}"
    # dense 1..total, no gaps
    assert sorted(results) == list(range(1, total + 1))


# ---------------------------------------------------------------------------
# 3. Corrupt / diverged persisted state degrades gracefully (never crashes a
#    resume). load_conversation must match get_case_state's fail-soft guard.
# ---------------------------------------------------------------------------
def test_corrupt_conversation_degrades_to_empty(storage):
    sid = "sess-corrupt"
    storage.save_conversation(sid, [{"role": "user", "content": "hi"}])
    # simulate a truncated / half-written row (a wedge would otherwise crash resume)
    with storage._conn() as c:
        c.execute("UPDATE session_conversation SET messages_json=? "
                  "WHERE session_id=?", ("{ not valid json", sid))
    # must NOT raise — a corrupt row reads as "no prior conversation"
    assert storage.load_conversation(sid) == []


def test_corrupt_card_degrades_to_none(storage):
    sid = "sess-corrupt2"
    storage.save_card(sid, {"id": "c1", "type": "action_card"})
    with storage._conn() as c:
        c.execute("UPDATE session_cards SET card_json=? "
                  "WHERE session_id=? AND card_id=?", ("}{bad", sid, "c1"))
    assert storage.get_card(sid, "c1") is None


# ---------------------------------------------------------------------------
# 4. A card with no key is not persisted.
# ---------------------------------------------------------------------------
def test_card_with_no_id_is_not_persisted(storage):
    """A card carrying neither id nor approval_id is silently a no-op today —
    pin that so a future gating card that forgets its key can't masquerade as
    persisted (it simply won't be recoverable)."""
    sid = "sess-noid"
    storage.save_card(sid, {"type": "action_card", "action": "x"})  # no id/approval_id
    # nothing to fetch — the absence is total
    with storage._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM session_cards WHERE session_id=?",
                      (sid,)).fetchone()[0]
    assert n == 0
