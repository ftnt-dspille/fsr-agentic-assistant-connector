"""C6: Stop ends the server turn.

chat_cancel marks the session's running turn; the loop-side check
(`_turn_stop_requested`, passed to the framework as cancel_check) reads that
mark. A stopped turn ends as `stopped_by_analyst`; a cancel with no running turn
is a no-op.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        sys.modules["connectors.core.connector"].ConnectorError = type(
            "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_cancel")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_cancel"] = pkg
    for k in [k for k in sys.modules if k.startswith("_fsrpb_cancel.")]:
        del sys.modules[k]
    importlib.import_module("_fsrpb_cancel.storage")
    return importlib.import_module("_fsrpb_cancel.ops_chat")


@pytest.fixture()
def store(ops, tmp_path, monkeypatch):
    pkg_storage = sys.modules["_fsrpb_cancel.storage"]
    st = pkg_storage.Storage(tmp_path / "turns.db")
    monkeypatch.setattr(pkg_storage, "_DEFAULT_STORAGE", st)
    return st


def _start_turn(st, sid):
    turn = st.reserve_next_turn(sid)
    st.append_turn_progress(sid, turn, {"type": "turn_start", "turn": turn})
    return turn


def test_cancel_marks_the_running_turn(ops, store):
    sid = "sess-cancel"
    turn = _start_turn(store, sid)
    assert store.turn_cancel_requested(sid, turn) is False
    resp = ops.chat_cancel({}, {"session_id": sid})
    assert resp["ok"] is True
    assert resp["turn"] == turn
    assert resp["cancelled"] is True
    assert store.turn_cancel_requested(sid, turn) is True


def test_cancel_with_no_running_turn_is_a_no_op(ops, store):
    sid = "sess-idle"
    turn = _start_turn(store, sid)
    store.append_turn_progress(sid, turn, {"type": "stream_end", "turn": turn,
                                           "stop_reason": "end_turn"},
                               is_terminal=True) if False else None
    store.fail_turn(sid, turn, "owner_gone")
    resp = ops.chat_cancel({}, {"session_id": sid})
    assert resp["cancelled"] is False
    assert store.turn_cancel_requested(sid, turn) is False


def test_cancel_is_idempotent(ops, store):
    sid = "sess-twice"
    turn = _start_turn(store, sid)
    ops.chat_cancel({}, {"session_id": sid})
    ops.chat_cancel({}, {"session_id": sid})
    assert store.turn_cancel_requested(sid, turn) is True


def test_stop_probe_reads_the_mark_and_survives_a_broken_store(ops, store):
    sid = "sess-probe"
    turn = _start_turn(store, sid)
    assert ops._turn_stop_requested(store, sid, turn) is False
    store.request_turn_cancel(sid, turn)
    assert ops._turn_stop_requested(store, sid, turn) is True

    class Broken:
        def turn_cancel_requested(self, *_a):
            raise RuntimeError("db gone")

    assert ops._turn_stop_requested(Broken(), sid, turn) is False
