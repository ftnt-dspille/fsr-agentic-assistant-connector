"""What the tier-3 approval resume writes (Phase 2 Step 0, flipped in Step 2b).

Before Step 2b, `_resume_suspended` ran the post-approval agent loop with a
hook that appended wire frames to the poll feed and nothing else: no
`chat_turns` row, no llm-activity audit row, no JSONL usage line, no per-tool
call rows. Every post-approval LLM round was absent from the usage and audit
trail. Step 0 pinned that. Step 2b gives this path the same sink as the other
turn paths, so the test below now asserts the rows exist. The flip is the
deliberate change; the feed assertions were unchanged.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    tags = {}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


class _Suspended:
    session_id = "sess-suspended"


def _drive(operations, tmp_path, monkeypatch):
    """Run _resume_suspended with one approved round that emits tool use, a
    tool result, text and a usage frame. Returns (storage, spies)."""
    import fsr_playbooks.llm.run_turn as run_turn
    from fsr_playbooks.llm.provider import (
        TextEvent, ToolResultEvent, ToolUseEvent, UsageEvent)
    ops_resume = sys.modules["_fsrpb_conn.ops_resume"]
    storage_mod = sys.modules["_fsrpb_conn.storage"]

    store = storage_mod.Storage(tmp_path / "suspended.db")
    store.set_session_intent("sess-suspended", "triage")
    store.save_conversation("sess-suspended", [{"role": "user", "content": "hi"}])

    spies = {"record_chat_turn": [], "record_agent_tool_calls": [],
             "log_llm_activity": [], "log_usage_jsonl": []}

    real_record = store.record_chat_turn
    real_tool_calls = store.record_agent_tool_calls

    def _spy_record(record):
        spies["record_chat_turn"].append(record)
        return real_record(record)

    def _spy_tool_calls(*a, **k):
        spies["record_agent_tool_calls"].append((a, k))
        return real_tool_calls(*a, **k)

    monkeypatch.setattr(store, "record_chat_turn", _spy_record)
    monkeypatch.setattr(store, "record_agent_tool_calls", _spy_tool_calls)
    turn_sink = sys.modules["_fsrpb_conn.turn_sink"]
    monkeypatch.setattr(turn_sink, "log_llm_activity",
                        lambda config, rec: spies["log_llm_activity"].append(rec) or 1)
    monkeypatch.setattr(turn_sink, "_log_usage_jsonl",
                        lambda ev, path: spies["log_usage_jsonl"].append(ev))

    usage = UsageEvent(
        session_id="sess-suspended", turn=1, model="claude-haiku-4-5-20251001",
        input_tokens=40, output_tokens=60, cache_read=5, cache_write=7,
        history_chars=0, stop_reason="end_turn", tool_calls=[])

    async def _fake_resume(**kw):
        hook = kw["on_event"]
        hook(ToolUseEvent(name="run_playbook", arguments={"name": "x"}, call_id="c1"))
        hook(ToolResultEvent(call_id="c1", result={"ok": True}, duration_ms=9))
        hook(TextEvent(text="Ran it."))
        hook(usage)
        return _Result()

    monkeypatch.setattr(run_turn, "resume_agent_turn", _fake_resume)
    out = ops_resume._resume_suspended(
        object(), _Suspended(), "approve", store, session_id="sess-suspended")
    assert out["ok"] is True
    return store, spies


def test_suspended_resume_writes_usage_and_audit_rows(
        operations, tmp_path, monkeypatch):
    """Every usage/audit sink is written for a tier-3 round, as on the other
    turn paths: the chat_turns row, the JSONL line, the llm-activity audit row
    and the per-tool-call rows. The rows are keyed on the feed turn."""
    _store, spies = _drive(operations, tmp_path, monkeypatch)
    assert len(spies["record_chat_turn"]) == 1
    row = spies["record_chat_turn"][0]
    # The feed turn is 1 (a fresh session); the history turn is 0. Usage rides
    # the feed, the turn the round's frames are on.
    assert row["turn"] == 1, "usage must ride the feed turn, not the history turn"
    assert len(spies["log_usage_jsonl"]) == 1
    assert len(spies["log_llm_activity"]) == 1
    audit = spies["log_llm_activity"][0]
    assert audit["provider"] == "anthropic"  # the object() stand-in has no name
    assert audit["intent"] == "triage"       # the stored session intent
    assert audit["prompt"] == ""             # an approval carries no analyst text
    assert audit["response"] == "Ran it."
    assert audit["tool_calls"] == []         # the usage frame here lists no calls
    (args, _kwargs), = spies["record_agent_tool_calls"]
    assert args[1] == row["turn"]
    assert [c["tool_name"] for c in args[2]] == ["run_playbook"]


def test_suspended_resume_still_streams_the_round_to_the_feed(
        operations, tmp_path, monkeypatch):
    """The wire side is intact today: the feed carries the round's frames and
    the usage frame, closed by one terminal frame."""
    store, _spies = _drive(operations, tmp_path, monkeypatch)
    frames = store.read_turn_progress("sess-suspended", 0, 0)["frames"]
    kinds = [f.get("type") for f in frames]
    assert "turn_start" in kinds
    assert "tool_use" in kinds and "tool_result" in kinds
    assert "text" in kinds
    assert "usage" in kinds
    assert kinds.count("stream_end") == 1
