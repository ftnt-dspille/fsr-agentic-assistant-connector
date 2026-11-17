"""chat_poll can hold a request open until a frame lands (long-poll).

Why. The widget used to re-poll every 700 ms, so a frame that landed just
after a poll waited for the next one: about 1.1 s of lag per frame, and a poll
per 0.7 s for the life of the turn. With `long_poll_ms` the poll waits on the
feed and returns the moment a frame (or the terminal) arrives. Without the
param the answer is immediate, as before.

Cases pinned here:
  - no wait requested: answers at once, even with no frame (back-compat)
  - a wait with nothing new: holds for the window, then returns empty
  - a frame landing mid-wait: returns early with that frame
  - the window is capped server-side
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def ops():
    """ops_chat loaded as a package submodule (relative imports resolve
    off-platform), the same way test_turn_liveness.py does it."""
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        sys.modules["connectors.core.connector"].ConnectorError = type(
            "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_longpoll")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_longpoll"] = pkg
    for k in [k for k in sys.modules if k.startswith("_fsrpb_longpoll.")]:
        del sys.modules[k]
    importlib.import_module("_fsrpb_longpoll.storage")
    return importlib.import_module("_fsrpb_longpoll.ops_chat")


@pytest.fixture()
def live_turn(ops, tmp_path, monkeypatch):
    """A running turn (turn_start written, no terminal) in a fresh store."""
    pkg_storage = sys.modules["_fsrpb_longpoll.storage"]
    st = pkg_storage.Storage(tmp_path / "turns.db")
    monkeypatch.setattr(pkg_storage, "_DEFAULT_STORAGE", st)
    sid = "sess-longpoll"
    turn = st.reserve_next_turn(sid)
    st.append_turn_progress(sid, turn, {"type": "turn_start", "turn": turn})
    return st, sid, turn


def _poll(ops, sid, cursor, long_poll_ms=None):
    params = {"session_id": sid, "cursor": cursor, "since_turn": 0}
    if long_poll_ms is not None:
        params["long_poll_ms"] = long_poll_ms
    return ops.chat_poll({}, params)


def test_no_wait_requested_answers_at_once(ops, live_turn):
    _st, sid, _turn = live_turn
    first = _poll(ops, sid, 0)
    cursor = first["cursor"]
    t0 = time.monotonic()
    resp = _poll(ops, sid, cursor)
    assert time.monotonic() - t0 < 0.2
    assert resp["frames"] == [] and resp["done"] is False


def test_wait_with_nothing_new_holds_for_the_window(ops, live_turn):
    _st, sid, _turn = live_turn
    cursor = _poll(ops, sid, 0)["cursor"]
    t0 = time.monotonic()
    resp = _poll(ops, sid, cursor, long_poll_ms=1200)
    elapsed = time.monotonic() - t0
    assert 1.0 <= elapsed < 2.5, f"expected to hold about 1.2 s, held {elapsed:.2f} s"
    assert resp["frames"] == [] and resp["done"] is False


def test_frame_landing_mid_wait_returns_early(ops, live_turn):
    st, sid, turn = live_turn
    cursor = _poll(ops, sid, 0)["cursor"]

    def late_frame():
        time.sleep(0.4)
        st.append_turn_progress(sid, turn, {"type": "text", "text": "hello"})

    writer = threading.Thread(target=late_frame)
    writer.start()
    t0 = time.monotonic()
    resp = _poll(ops, sid, cursor, long_poll_ms=6000)
    elapsed = time.monotonic() - t0
    writer.join()
    assert elapsed < 2.0, f"a frame at 0.4 s should return early, took {elapsed:.2f} s"
    assert [f.get("type") for f in resp["frames"]] == ["text"]


def test_window_is_capped_server_side(ops, live_turn, monkeypatch):
    _st, sid, _turn = live_turn
    monkeypatch.setattr(ops, "_LONG_POLL_MAX_MS", 300)
    cursor = _poll(ops, sid, 0)["cursor"]
    t0 = time.monotonic()
    _poll(ops, sid, cursor, long_poll_ms=60000)
    assert time.monotonic() - t0 < 1.5


def test_held_poll_says_so_and_plain_poll_does_not(ops, live_turn):
    """The widget re-polls at once only when the connector says it held the
    poll; a plain poll must not carry the flag."""
    _st, sid, _turn = live_turn
    cursor = _poll(ops, sid, 0)["cursor"]
    assert "long_poll" not in _poll(ops, sid, cursor)
    assert _poll(ops, sid, cursor, long_poll_ms=300).get("long_poll") is True
