"""Terminal-frame characterization across the four writers (Phase 2 Step 0).

Each turn feed closes with one terminal frame, and the poll path, the widget and
the detached backstop all read it. Four code sites write it today:

  chat.finalize            ops_chat._finalize (blocking + detached success)
  chat.write_terminal_error ops_chat._write_terminal_error (detached crash)
  resume.suspended         ops_resume._resume_suspended (tier-3 approval)
  resume.conversation      ops_resume._resume_conversation

This pins the stored frame each writer produces, normalised for the volatile
parts only (uuid turn ids, the test file's own line number in the crash
origin). Snapshots live in tests/golden/frames/. Step 1 unifies the writers; a
deliberate shape change is a snapshot update, never a silent one.

Regenerate with UPDATE_GOLDEN=1 and review the diff.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_DIR = ROOT / "tests" / "golden" / "frames"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"
UUID_RE = re.compile(r"\b[0-9a-f]{32}\b")
ORIGIN_RE = re.compile(r"\(at (test_turn_terminal_frames\.py):\d+\)")


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Result:
    transcript = []
    tags = {"intent": "triage"}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


class _TextResult(_Result):
    """A turn with a real transcript, so a trimmed vs kept copy is visible."""

    @property
    def transcript(self):
        from fsr_playbooks.llm.provider import TextEvent
        return [TextEvent(text="long finding " + "x" * 2000)]


class _ErrorResult(_Result):
    error = "provider returned 503"
    stop_reason = "error"


def _normalise(frame: dict) -> dict:
    out = {k: v for k, v in frame.items() if k != "cursor"}
    text = json.dumps(out, sort_keys=True, default=str)
    text = UUID_RE.sub("<uuid>", text)
    text = ORIGIN_RE.sub(r"(at \1:<line>)", text)
    return json.loads(text)


def _terminal_frames(store, session_id: str) -> list[dict]:
    frames = store.read_turn_progress(session_id, 0, 0)["frames"]
    return [f for f in frames if f.get("type") == "stream_end"]


def _check(name: str, actual) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    text = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
        return
    assert path.exists(), f"no golden frame {path}; run with UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="ascii"), (
        f"terminal frame drift for {name}; review and regenerate if intended")


@pytest.fixture
def env(operations, tmp_path, monkeypatch):
    import fsr_playbooks.llm.run_turn as run_turn
    ops_chat = sys.modules["_fsrpb_conn.ops_chat"]
    ops_resume = sys.modules["_fsrpb_conn.ops_resume"]
    storage_mod = sys.modules["_fsrpb_conn.storage"]
    from fsr_playbooks.llm.provider import UsageEvent

    store = storage_mod.Storage(tmp_path / "frames.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda *a, **k: False)
    monkeypatch.setattr(ops_chat, "_build_provider",
                        lambda config, gateway: object())
    monkeypatch.setattr(sys.modules["_fsrpb_conn.turn_sink"], "log_llm_activity",
                        lambda config, rec: 1)

    state = {"result": _Result(), "raise": None}

    async def _fake_run_agent_turn(**kw):
        if state["raise"] is not None:
            raise state["raise"]
        return state["result"]

    async def _fake_resume_agent_turn(**kw):
        return state["result"]

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_run_agent_turn)
    monkeypatch.setattr(run_turn, "resume_agent_turn", _fake_resume_agent_turn)

    class _Env:
        pass

    e = _Env()
    e.operations = operations
    e.ops_resume = ops_resume
    e.store = store
    e.state = state
    e.UsageEvent = UsageEvent
    return e


def _join_detached():
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(5.0)


def test_chat_finalize_terminal_frame(env):
    sid = "frames-chat-finalize"
    env.operations.chat_turn({}, {
        "session_id": sid, "intent": "triage",
        "messages": [{"role": "user", "content": "triage this"}]})
    ends = _terminal_frames(env.store, sid)
    assert len(ends) == 1, f"expected one terminal frame, got {len(ends)}"
    assert env.store.turn_is_terminal(sid, 1)
    _check("chat-finalize", _normalise(ends[0]))


def test_blocking_finalize_terminal_is_signal_only_but_return_has_transcript(env):
    """The trim: a blocking caller already holds the transcript from the
    return, so the feed's stream_end does not repeat it."""
    sid = "frames-blocking-trim"
    env.state["result"] = _TextResult()
    out = env.operations.chat_turn({}, {
        "session_id": sid, "intent": "triage",
        "messages": [{"role": "user", "content": "triage this"}]})
    assert any("long finding" in str(b) for b in out["transcript"])
    (end,) = _terminal_frames(env.store, sid)
    assert "transcript" not in end
    assert end["ok"] is True and end["stop_reason"] == "end_turn"
    assert end["turn_id"] == out["turn_id"] and end["tags"]["intent"] == "triage"


def test_detached_finalize_terminal_keeps_the_transcript(env):
    """A detached turn has no blocking return: the terminal frame IS the
    delivery, so it must still carry the transcript."""
    sid = "frames-detached-keeps"
    env.state["result"] = _TextResult()
    ack = env.operations.chat_turn({}, {
        "session_id": sid, "intent": "triage", "detached": True,
        "messages": [{"role": "user", "content": "triage this"}]})
    assert ack.get("accepted") is True
    _join_detached()
    (end,) = _terminal_frames(env.store, sid)
    assert any("long finding" in str(b) for b in end["transcript"])


def test_blocking_error_terminal_keeps_the_transcript(env):
    sid = "frames-blocking-error-keeps"
    env.state["result"] = _ErrorResult()
    env.operations.chat_turn({}, {
        "session_id": sid, "intent": "triage",
        "messages": [{"role": "user", "content": "triage this"}]})
    (end,) = _terminal_frames(env.store, sid)
    assert "transcript" in end and end["ok"] is False


def test_chat_finalize_error_terminal_frame(env):
    sid = "frames-chat-finalize-error"
    env.state["result"] = _ErrorResult()
    env.operations.chat_turn({}, {
        "session_id": sid, "intent": "triage",
        "messages": [{"role": "user", "content": "triage this"}]})
    ends = _terminal_frames(env.store, sid)
    assert len(ends) == 1
    _check("chat-finalize-error", _normalise(ends[0]))


def test_chat_write_terminal_error_frame(env):
    sid = "frames-chat-error"
    env.state["raise"] = RuntimeError("boom in the loop")
    ack = env.operations.chat_turn({}, {
        "session_id": sid, "intent": "triage", "detached": True,
        "messages": [{"role": "user", "content": "triage this"}]})
    assert ack.get("accepted") is True
    _join_detached()
    ends = _terminal_frames(env.store, sid)
    assert len(ends) == 1, f"expected one terminal error frame, got {len(ends)}"
    assert ends[0]["ok"] is False
    assert ends[0]["error"].startswith("RuntimeError: boom in the loop (at test_turn_terminal_frames.py:")
    _check("chat-write-terminal-error", _normalise(ends[0]))


def test_resume_suspended_terminal_frame(env):
    sid = "frames-suspended"

    class _Suspended:
        session_id = sid

    env.store.set_session_intent(sid, "triage")
    env.store.save_conversation(sid, [{"role": "user", "content": "hi"}])
    out = env.ops_resume._resume_suspended(
        object(), _Suspended(), "approve", env.store, session_id=sid)
    assert out["ok"] is True
    ends = _terminal_frames(env.store, sid)
    assert len(ends) == 1
    _check("resume-suspended", _normalise(ends[0]))


def test_resume_conversation_terminal_frame(env):
    sid = "frames-conversation"
    env.store.set_session_intent(sid, "triage")
    env.store.save_conversation(sid, [{"role": "user", "content": "hi"}])
    out = env.operations._resume_conversation(
        {}, {"session_id": sid, "card_id": "c1"}, "approve", env.store,
        provider=object(), user_msg="continue")
    assert out["ok"] is True
    ends = _terminal_frames(env.store, sid)
    assert len(ends) == 1
    _check("resume-conversation", _normalise(ends[0]))
