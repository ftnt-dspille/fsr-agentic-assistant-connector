"""A background turn whose owner dies must surface as failed, not run forever.

Each streaming turn is owned by the worker process that reserved it
(`turn_runs`: owner pid + heartbeat). The poll path fails a turn that has no
terminal frame when its owner is gone, its heartbeat has stopped, or it has
gone silent past the producer ceiling; the worker that starts next sweeps
turns left running by a dead pid. Cases pinned here:

  - the owner process dies mid-turn (a real separate process), poll -> failed
  - the owner is alive but its heartbeat is stale, poll -> failed
  - a turn that is alive and beating is not touched
  - a completed turn is unaffected, and a turn finishes exactly once
  - the sweep fails only turns whose owner is gone
  - the heartbeat beats while a turn runs and stops at its terminal frame
"""
from __future__ import annotations

import subprocess
import sys
import time
import importlib
import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import storage as S  # noqa: E402



@pytest.fixture(scope="module")
def ops():
    """ops_chat loaded as a package submodule (relative imports resolve
    off-platform), mirroring test_scheduled_task_runs."""
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        sys.modules["connectors.core.connector"].ConnectorError = type(
            "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_liveness")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_liveness"] = pkg
    for k in [k for k in sys.modules if k.startswith("_fsrpb_liveness.")]:
        del sys.modules[k]
    importlib.import_module("_fsrpb_liveness.storage")
    return importlib.import_module("_fsrpb_liveness.ops_chat")


@pytest.fixture()
def store(ops, tmp_path, monkeypatch):
    """The ops package's Storage: it is the module ops_common installs the
    terminal-frame builder on, so a turn failed through it is wired as in
    production."""
    pkg_storage = sys.modules["_fsrpb_liveness.storage"]
    st = pkg_storage.Storage(tmp_path / "turns.db")
    monkeypatch.setattr(pkg_storage, "_DEFAULT_STORAGE", st)
    return st


def _hb(st):
    """The heartbeat registry of the module that built `st` (the store fixture
    comes from the ops package, a distinct module object from `storage`)."""
    return sys.modules[type(st).__module__]._HEARTBEAT


def _poll(ops, sid, cursor=0, since_turn=0):
    return ops.chat_poll({}, {"session_id": sid, "cursor": cursor,
                              "since_turn": since_turn})


def _age_heartbeat(st, sid, turn, seconds):
    with st._write() as c:
        c.execute("UPDATE turn_runs SET heartbeat_at=? WHERE session_id=? "
                  "AND turn=?", (time.time() - seconds, sid, turn))


_DYING_OWNER = """
import os, sys
sys.path.insert(0, sys.argv[1])
import storage
st = storage.Storage(sys.argv[2])
turn = st.reserve_next_turn(sys.argv[3])
st.append_turn_progress(sys.argv[3], turn, {"type": "text", "text": "partial"})
os._exit(0)
"""


def _dying_owner(db, sid):
    """A worker that reserves a turn, streams one frame, then dies with the
    turn still running -- no terminal frame, as a SIGKILL or restart would.

    A fresh interpreter, not a fork: a fork of the test process inherits any
    lock a live thread (the heartbeat, a warm) holds at that instant, and under
    some test orders the child hung in sqlite3.connect."""
    proc = subprocess.run(
        [sys.executable, "-c", _DYING_OWNER, str(ROOT), str(db), sid],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def _terminal_frames(frames):
    return [f for f in frames if f.get("type") == "stream_end"]


def test_owner_dies_mid_turn_poll_reports_failed(ops, store, tmp_path):
    sid = "live-sess-dies"
    _dying_owner(store.db_path, sid)

    turn = store.read_turn_progress(sid)["turn"]
    assert store.turn_run(sid, turn)["status"] == "running"

    resp = _poll(ops, sid)
    msg = S.TURN_FAILURE_MESSAGES["owner_gone"]
    assert resp["done"] is True
    assert resp["stop_reason"] == "error"
    assert resp["error"] == msg
    terminal = _terminal_frames(resp["frames"])
    assert len(terminal) == 1
    assert terminal[0]["ok"] is False
    assert terminal[0]["stop_reason"] == "error"
    assert terminal[0]["transcript"] == [{"type": "error", "message": msg}]
    assert store.turn_run(sid, turn)["status"] == "failed"

    # Once failed, a later poll reports the same terminal and no new error.
    again = _poll(ops, sid)
    assert again["done"] is True
    assert "error" not in again
    assert len(_terminal_frames(again["frames"])) == 1


def test_stale_heartbeat_with_live_owner_reports_failed(ops, store):
    sid = "live-sess-stale"
    turn = store.reserve_next_turn(sid)
    # The owner is this live process, but its heartbeat has stopped: stop the
    # registry from beating this turn, then age the last beat past the limit.
    _hb(store).discard(store.db_path, sid, turn)
    _age_heartbeat(store, sid, turn, S.TURN_STALE_SECS + 30)

    resp = _poll(ops, sid)
    assert resp["done"] is True
    assert resp["error"] == S.TURN_FAILURE_MESSAGES["stale_heartbeat"]
    assert store.turn_run(sid, turn)["status"] == "failed"


def test_live_beating_turn_is_left_alone(ops, store):
    sid = "live-sess-alive"
    turn = store.reserve_next_turn(sid)
    store.append_turn_progress(sid, turn, {"type": "text", "text": "working"})

    resp = _poll(ops, sid)
    assert resp["done"] is False
    assert "error" not in resp
    assert store.turn_run(sid, turn)["status"] == "running"
    _hb(store).discard(store.db_path, sid, turn)


def test_silent_producer_past_ceiling_is_failed_even_when_beating(ops, store):
    """Backstop: the heartbeat proves the owner is alive, not that the producer
    is making progress. A turn silent past the producer ceiling still fails."""
    sid = "live-sess-hung"
    turn = store.reserve_next_turn(sid)
    store.append_turn_progress(sid, turn, {"type": "text", "text": "x"})
    with store._write() as c:
        c.execute("UPDATE turn_progress SET created_at=? WHERE session_id=?",
                  (time.time() - ops._STREAM_STALE_SECS - 60, sid))
    resp = _poll(ops, sid)
    assert resp["done"] is True
    assert resp["error"] == S.TURN_FAILURE_MESSAGES["silent"]
    _hb(store).discard(store.db_path, sid, turn)


def test_completed_turn_is_unaffected_and_finishes_once(ops, store):
    sid = "done-sess"
    turn = store.reserve_next_turn(sid)
    store.append_turn_progress(sid, turn, {"type": "text", "text": "hi"})
    assert store.append_turn_progress(
        sid, turn, {"type": "stream_end", "stop_reason": "end_turn"},
        terminal=True, stop_reason="end_turn") is True
    # Its heartbeat is long stale and its owner may be gone: a finished turn
    # must not be re-judged.
    _age_heartbeat(store, sid, turn, S.TURN_STALE_SECS * 10)

    resp = _poll(ops, sid)
    assert resp["done"] is True
    assert resp["stop_reason"] == "end_turn"
    assert "error" not in resp
    assert store.turn_run(sid, turn)["status"] == "done"

    # A second terminal (a racing watchdog) is refused, not appended.
    assert store.append_turn_progress(
        sid, turn, {"type": "stream_end"}, terminal=True,
        stop_reason="error") is False
    assert store.read_turn_progress(sid)["stop_reason"] == "end_turn"
    assert store.fail_turn(sid, turn, "owner_gone") is None


def test_sweep_fails_only_turns_whose_owner_is_gone(store):
    dead_sid, live_sid = "sweep-dead", "sweep-live"
    _dying_owner(store.db_path, dead_sid)
    live_turn = store.reserve_next_turn(live_sid)

    assert store.sweep_orphaned_turns() == 1
    dead_turn = store.read_turn_progress(dead_sid)["turn"]
    assert store.turn_run(dead_sid, dead_turn)["status"] == "failed"
    assert store.turn_run(live_sid, live_turn)["status"] == "running"
    assert store.read_turn_progress(dead_sid)["done"] is True
    assert store.sweep_orphaned_turns() == 0
    _hb(store).discard(store.db_path, live_sid, live_turn)


def test_heartbeat_beats_while_running_and_stops_at_terminal(tmp_path, monkeypatch):
    """A registry beats a running turn on its interval and stops beating it once
    the turn is no longer running. A dedicated registry is used: the process
    one may already be sleeping on the production interval, which the patch
    cannot shorten."""
    monkeypatch.setattr(S, "TURN_HEARTBEAT_SECS", 0.05)
    st = S.Storage(tmp_path / "hb.db")
    sid = "hb-sess"
    turn = st.reserve_next_turn(sid)
    registry = S._TurnHeartbeat()
    registry.add(st.db_path, sid, turn)
    first = st.turn_run(sid, turn)["heartbeat_at"]
    time.sleep(0.5)
    assert st.turn_run(sid, turn)["heartbeat_at"] > first

    st.append_turn_progress(sid, turn, {"type": "stream_end"}, terminal=True,
                            stop_reason="end_turn")
    time.sleep(0.2)  # let the registry notice the turn is no longer running
    settled = st.turn_run(sid, turn)["heartbeat_at"]
    time.sleep(0.3)
    assert st.turn_run(sid, turn)["heartbeat_at"] == settled
    assert (st.db_path, sid, turn) not in registry._turns


def _store_errors(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(S, "_log_store_error",
                        lambda what, exc: seen.append(f"{what}: {exc}"))
    return seen


def test_heartbeat_stops_beating_a_turn_whose_database_is_gone(tmp_path, monkeypatch):
    """A turn whose store was removed (a deleted session DB, a test's temp dir)
    was beaten, and its failure logged, every interval for three hours."""
    monkeypatch.setattr(S, "TURN_HEARTBEAT_SECS", 0.05)
    seen = _store_errors(monkeypatch)
    d = tmp_path / "gone"
    d.mkdir()
    st = S.Storage(d / "hb.db")
    sid = "hb-gone"
    turn = st.reserve_next_turn(sid)
    _hb(st).discard(st.db_path, sid, turn)  # only the registry under test beats
    registry = S._TurnHeartbeat()
    registry.add(st.db_path, sid, turn)
    for f in d.iterdir():
        f.unlink()
    d.rmdir()
    time.sleep(0.6)
    assert (str(st.db_path), sid, turn) not in registry._turns
    mine = [m for m in seen if f"{sid}/{turn}" in m]
    assert len(mine) == 1, seen
    assert "database gone" in mine[0]


def test_heartbeat_logs_a_failing_turn_once_per_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "TURN_HEARTBEAT_SECS", 0.05)
    seen = _store_errors(monkeypatch)
    st = S.Storage(tmp_path / "hb.db")
    sid = "hb-locked"
    turn = st.reserve_next_turn(sid)
    _hb(st).discard(st.db_path, sid, turn)  # only the registry under test beats
    registry = S._TurnHeartbeat()
    real = S._closing
    broken = {"on": True}

    def _flaky(path):
        if broken["on"] and str(path) == str(st.db_path):
            raise S.sqlite3.OperationalError("database is locked")
        return real(path)

    monkeypatch.setattr(S, "_closing", _flaky)
    registry.add(st.db_path, sid, turn)
    time.sleep(0.5)
    # Still tracked (a locked DB is transient), logged once, not every beat.
    assert (str(st.db_path), sid, turn) in registry._turns
    assert [m for m in seen if f"{sid}/{turn}" in m] == [
        f"turn heartbeat {sid}/{turn}: database is locked"], seen
    broken["on"] = False
    time.sleep(0.3)
    assert (str(st.db_path), sid, turn) not in registry._failing
    registry.discard(st.db_path, sid, turn)
