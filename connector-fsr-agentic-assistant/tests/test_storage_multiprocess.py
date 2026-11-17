"""Multi-process safety of the connector's sqlite store.

The connector runs in several uWSGI worker processes that share one sqlite
file. Single-process tests cannot see a lost update or a double claim between
processes, so these fork real worker processes that hammer the read-modify-
write paths and assert the invariants that matter: one winner per claim, no
lost update, no sqlite error escaping to the caller, and an intact file.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import time

import pytest

from storage import Storage

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"),
                                reason="multi-process tests need fork()")
# The test harness forks from a process that has a live heartbeat thread;
# the child only opens its own sqlite connections, so the warning is noise here.
pytestmark = [pytestmark, pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"),
    # Above the 120s queue wait in _run, so a hung worker fails with the
    # test's own message instead of the suite-wide per-test timeout.
    pytest.mark.timeout(300)]

N_PROCS = 6
N_OPS = 30
# forkserver, not fork: the test process is multi-threaded (pytest-xdist's
# execnet threads, the heartbeat thread), and a plain fork() copies any lock
# another thread held at that instant into a child that can never release it.
# A child deadlocked that way never reported, so q.get raised Empty after 120s
# under load, and its orphaned workers once stalled the whole gate. The fork
# server is a clean single-threaded process, so the children it forks start
# with no inherited locks. Workers still use the real fork-based process model.
_CTX = mp.get_context("forkserver") if hasattr(os, "fork") else None


def _run(target, db, *args, n=N_PROCS):
    """Fork `n` workers running `target(db, idx, q, *args)`; return their
    reports. Every worker must exit cleanly."""
    q = _CTX.Queue()
    procs = [_CTX.Process(target=target, args=(str(db), i, q, *args))
             for i in range(n)]
    for p in procs:
        p.start()
    try:
        reports = [q.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(60)
    finally:
        # A worker that is still alive here is hung: kill it rather than leave
        # an orphan holding the sqlite file and the pytest worker's pipes.
        for p in procs:
            if p.is_alive():
                p.kill()
                p.join(5)
    assert [p.exitcode for p in procs] == [0] * n
    return reports


def _claim_worker(db, idx, q, card_ids):
    st = Storage(db)
    wins, errors = [], []
    for cid in card_ids:
        try:
            if st.claim_card_decision("sess", cid, "approve", f"p{idx}") is None:
                wins.append(cid)
        except sqlite3.Error as e:  # a lock error must never escape
            errors.append(repr(e))
    q.put({"wins": wins, "errors": errors})


def _undo_worker(db, idx, q, card_ids):
    st = Storage(db)
    wins, errors = [], []
    for cid in card_ids:
        try:
            if st.claim_card_undo("sess", cid, f"p{idx}") is None:
                wins.append(cid)
        except sqlite3.Error as e:
            errors.append(repr(e))
    q.put({"wins": wins, "errors": errors})


def _usage_worker(db, idx, q, _unused):
    st = Storage(db)
    ids = []
    for k in range(N_OPS):
        ids.append(st.record_agent_usage({
            "ts": "2000-01-01T00:00:00Z", "session_id": f"s{idx}",
            "model": "m", "input_tokens": 1, "output_tokens": 1,
            "turn": k, "status": "success"}))
    q.put({"ids": ids, "errors": []})


def _mock_worker(db, idx, q, _unused):
    st = Storage(db)
    errors = []
    for k in range(N_OPS):
        try:
            st.mock_mark("sess", "scn", idx * N_OPS + k)
        except sqlite3.Error as e:
            errors.append(repr(e))
    q.put({"ids": [], "errors": errors})


def _turn_worker(db, idx, q, _unused):
    st = Storage(db)
    turns, errors = [], []
    for _ in range(N_OPS):
        try:
            turns.append(st.reserve_next_turn("turns-sess"))
        except sqlite3.Error as e:
            errors.append(repr(e))
    q.put({"ids": turns, "errors": errors})


def _open_worker(db, idx, q, barrier):
    barrier.wait()  # every worker opens the fresh DB at the same instant
    errors = []
    try:
        Storage(db)
    except sqlite3.Error as e:
        errors.append(repr(e))
    q.put({"ids": [], "errors": errors})


def _assert_intact(db):
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def _no_errors(reports):
    errors = [e for r in reports for e in r["errors"]]
    assert errors == [], f"sqlite errors escaped to callers: {errors[:3]}"


def test_claim_card_decision_one_winner_per_card(tmp_path):
    db = tmp_path / "claims.db"
    Storage(db)
    cards = [f"card-{i}" for i in range(N_OPS)]
    reports = _run(_claim_worker, db, cards)
    _no_errors(reports)
    winners = [cid for r in reports for cid in r["wins"]]
    # Every card claimed exactly once across all processes, never twice.
    assert sorted(winners) == sorted(cards)
    _assert_intact(db)


def test_claim_card_undo_one_winner_per_card(tmp_path):
    db = tmp_path / "undo.db"
    st = Storage(db)
    cards = [f"card-{i}" for i in range(N_OPS)]
    for cid in cards:
        st.record_card_execution("sess", cid, connector="c", operation="op",
                                 config=None, args={}, ok=True)
    reports = _run(_undo_worker, db, cards)
    _no_errors(reports)
    winners = [cid for r in reports for cid in r["wins"]]
    assert sorted(winners) == sorted(cards)
    _assert_intact(db)


def test_usage_rows_not_lost_across_processes(tmp_path):
    db = tmp_path / "usage.db"
    Storage(db)
    reports = _run(_usage_worker, db, None)
    _no_errors(reports)
    # record_agent_usage swallows errors and returns None; a None here is a
    # lost telemetry row.
    assert all(i is not None for r in reports for i in r["ids"])
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM agent_usage").fetchone()[0] \
            == N_PROCS * N_OPS
    _assert_intact(db)


def test_mock_cursor_has_no_lost_update(tmp_path):
    db = tmp_path / "mock.db"
    Storage(db)
    reports = _run(_mock_worker, db, None)
    _no_errors(reports)
    consumed = Storage(db).mock_consumed("sess", "scn")
    # A read-modify-write that is not one write transaction drops indexes
    # when two processes interleave between the SELECT and the UPDATE.
    assert sorted(consumed) == list(range(N_PROCS * N_OPS))
    _assert_intact(db)


def test_reserve_next_turn_unique_across_processes(tmp_path):
    db = tmp_path / "turns.db"
    Storage(db)
    reports = _run(_turn_worker, db, None)
    _no_errors(reports)
    turns = sorted(t for r in reports for t in r["ids"])
    assert turns == list(range(1, N_PROCS * N_OPS + 1))
    _assert_intact(db)


def test_concurrent_first_open_migrates_legacy_schema(tmp_path):
    """A DB created before the agent_usage / scheduled_tasks column additions
    is opened by several workers at once. Each sees the column missing; only
    one ALTER may run, and no worker may see "duplicate column name"."""
    db = tmp_path / "legacy.db"
    Storage(db)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE agent_usage DROP COLUMN agent_thinking")
        c.execute("ALTER TABLE agent_usage DROP COLUMN approvals_count")
        c.execute("ALTER TABLE scheduled_tasks DROP COLUMN notify_target")
    barrier = _CTX.Barrier(N_PROCS, timeout=60)
    reports = _run(_open_worker, db, barrier)
    _no_errors(reports)
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(agent_usage)")}
    assert {"agent_thinking", "approvals_count"} <= cols
    _assert_intact(db)


def _progress_writer(db, idx, q, turn):
    st = Storage(db)
    for i in range(N_OPS * 10):
        st.append_turn_progress("snap-sess", turn, {"type": "text", "i": i})
    st.append_turn_progress("snap-sess", turn, {"type": "stream_end"},
                            terminal=True, stop_reason="end_turn")
    q.put({"ids": [], "errors": []})


def test_poll_never_reports_done_before_its_frames(tmp_path):
    """A poller that sees `done` must already hold every frame of the turn.
    The poll reads its pieces in separate statements; a terminal written
    between them makes `done` true while the last frames were never read,
    and a widget that stops polling on `done` drops them."""
    db = tmp_path / "snap.db"
    st = Storage(db)
    turn = st.reserve_next_turn("snap-sess")
    q = _CTX.Queue()
    proc = _CTX.Process(target=_progress_writer, args=(str(db), 0, q, turn))
    proc.start()
    cursor, seen, done = 0, [], False
    deadline = time.time() + 60
    while not done and time.time() < deadline:
        prog = st.read_turn_progress("snap-sess", cursor, 0)
        seen.extend(f for f in prog["frames"] if f.get("type") != "turn_start")
        cursor = prog["cursor"]
        done = prog["done"]
    q.get(timeout=60)
    proc.join(60)
    assert done
    texts = [f["i"] for f in seen if f.get("type") == "text"]
    assert texts == list(range(N_OPS * 10))
    assert any(f.get("type") == "stream_end" for f in seen)
    _assert_intact(db)
