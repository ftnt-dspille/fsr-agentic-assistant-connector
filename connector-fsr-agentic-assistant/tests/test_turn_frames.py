"""turn_frames: the one writer for a turn's terminal frames (Phase 2 Step 1).

A turn feed closes with one terminal stream_end frame. chat_turn's finalize,
the detached error writer and both resume paths used to assemble it by hand.
turn_frames.py is the one writer now. These tests pin its contract, and that
the two connector modules that close turns can no longer hand-build one.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def tf():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn_tf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_tf"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_tf.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_tf.turn_frames", ROOT / "turn_frames.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_tf.turn_frames"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Store:
    def __init__(self, fail: BaseException | None = None):
        self.calls: list[dict] = []
        self.fail = fail

    def append_turn_progress(self, session_id, turn, frame, terminal=False,
                             stop_reason=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append({"session_id": session_id, "turn": turn,
                           "frame": frame, "terminal": terminal,
                           "stop_reason": stop_reason})
        return True


@pytest.fixture
def logged(tf, monkeypatch):
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(tf, "_log", lambda msg, level="info": lines.append((msg, level)))
    return lines


def _end(store, **overrides):
    kwargs = dict(source="finalize", turn_id="t-1", ok=True,
                  stop_reason="end_turn", transcript=[], error=None,
                  last_assistant_yaml=None, tags={"intent": "triage"})
    kwargs.update(overrides)
    return kwargs


def test_stream_end_is_terminal_and_keeps_explicit_nulls(tf, logged):
    store = _Store()
    assert tf.write_stream_end(store, "s1", 3, **_end(store)) is True
    (call,) = store.calls
    assert call["session_id"] == "s1" and call["turn"] == 3
    assert call["terminal"] is True and call["stop_reason"] == "end_turn"
    frame = call["frame"]
    assert frame["type"] == "stream_end"
    assert frame["turn_id"] == "t-1"
    assert frame["contract_version"]
    # Explicit nulls survive: the chat path's shape, now shared by resume.
    assert "error" in frame and frame["error"] is None
    assert "last_assistant_yaml" in frame and frame["last_assistant_yaml"] is None
    assert frame["tags"] == {"intent": "triage"}
    assert logged == []


def test_failed_stream_end_is_logged_loudly_and_reported(tf, logged):
    store = _Store(fail=RuntimeError("database is locked"))
    assert tf.write_stream_end(store, "s2", 4,
                               **_end(store, source="resume")) is False
    (msg, level), = logged
    assert level == "error"
    assert msg == ("resume: terminal stream_end append FAILED session=s2 turn=4: "
                   "RuntimeError: database is locked")


def test_terminal_error_names_the_exception_and_its_line(tf, logged):
    def _raise_here():
        raise ValueError("bad state")

    try:
        _raise_here()
    except ValueError as exc:
        err = exc
    store = _Store()
    assert tf.write_terminal_error(store, "s3", 2, turn_id="t-3", exc=err) is True
    (call,) = store.calls
    frame = call["frame"]
    assert call["terminal"] is True and call["stop_reason"] == "error"
    assert frame["ok"] is False and frame["stop_reason"] == "error"
    assert re.fullmatch(
        r"ValueError: bad state \(at test_turn_frames\.py:\d+\)", frame["error"]), (
        frame["error"])
    assert frame["transcript"][0]["type"] == "error"
    assert frame["transcript"][0]["message"] == frame["error"]
    assert logged == []


def test_terminal_error_without_a_traceback_has_no_origin(tf, logged):
    store = _Store()
    tf.write_terminal_error(store, "s4", 1, turn_id="t-4",
                            exc=RuntimeError("never raised"))
    assert store.calls[0]["frame"]["error"] == "RuntimeError: never raised"


def test_failed_terminal_error_is_logged_and_reported(tf, logged):
    store = _Store(fail=OSError("disk gone"))
    assert tf.write_terminal_error(store, "s5", 6, turn_id="t-5",
                                   exc=RuntimeError("x")) is False
    (msg, level), = logged
    assert level == "error"
    assert msg.startswith("detached: terminal ERROR frame append FAILED "
                          "session=s5 turn=6: OSError: disk gone")


def test_turn_closers_do_not_hand_build_a_terminal_frame():
    """The guard: ops_chat and ops_resume close turns only through turn_frames.
    A hand-built stream_end in either module is the drift this module removed."""
    for name in ("ops_chat.py", "ops_resume.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert 'type="stream_end"' not in src, f"{name} hand-builds a stream_end"
        assert "StreamEndFrame(" not in src, f"{name} constructs StreamEndFrame"
        assert "terminal=True" not in src, f"{name} appends a terminal frame directly"
