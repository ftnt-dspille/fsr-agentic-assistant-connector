"""A failed accounting write must not fail the turn it accounts for (Phase 2 Step 2b).

chat_turn's event sink used to call storage.record_chat_turn unguarded, inside
the agent loop's event dispatch. A transient SQLite lock there could kill a live
turn over an accounting row. The resume path already swallowed the same error
silently. The factory now treats both the same way: it logs the failure at
error level, the turn completes, and the other rows (llm-activity audit row,
tool-call rows) still land.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL = "claude-haiku-4-5-20251001"


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn_wf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_wf"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_wf.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_wf.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_wf.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Result:
    transcript = []
    tags = {}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


def _usage():
    from fsr_playbooks.llm.provider import UsageEvent
    return UsageEvent(
        session_id="w", turn=1, model=MODEL, input_tokens=5, output_tokens=7,
        cache_read=0, cache_write=0, history_chars=0, stop_reason="end_turn",
        tool_calls=[])


def _locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def env(operations, tmp_path, monkeypatch):
    import fsr_playbooks.llm.run_turn as run_turn
    from fsr_playbooks.llm.provider import TextEvent
    ops_chat = sys.modules["_fsrpb_conn_wf.ops_chat"]
    turn_sink = sys.modules["_fsrpb_conn_wf.turn_sink"]
    storage_mod = sys.modules["_fsrpb_conn_wf.storage"]

    store = storage_mod.Storage(tmp_path / "write-failure.db")
    monkeypatch.setattr(store, "record_chat_turn", _locked)
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda *a, **k: False)
    monkeypatch.setattr(ops_chat, "_build_provider",
                        lambda config, gateway: object())

    audit: list[dict] = []
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(turn_sink, "log_llm_activity",
                        lambda config, rec: audit.append(rec) or 1)
    monkeypatch.setattr(turn_sink, "_log_usage_jsonl", lambda ev, path: None)
    monkeypatch.setattr(turn_sink, "_log",
                        lambda msg, level="info": lines.append((msg, level)))

    async def _one_round(**kw):
        kw["on_event"](TextEvent(text="Done."))
        kw["on_event"](_usage())
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _one_round)
    monkeypatch.setattr(run_turn, "resume_agent_turn", _one_round)

    class _Env:
        pass

    e = _Env()
    e.operations = operations
    e.store = store
    e.audit = audit
    e.lines = lines
    e.turn_sink = turn_sink
    return e


def _failures(lines):
    return [msg for msg, level in lines
            if level == "error" and "record_chat_turn FAILED" in msg]


def test_a_failed_usage_write_does_not_fail_the_chat_turn(env):
    out = env.operations.chat_turn({}, {
        "session_id": "w-chat", "intent": "build",
        "messages": [{"role": "user", "content": "hi"}]})
    assert out["ok"] is True, out
    assert out["stop_reason"] == "end_turn"


def test_a_failed_usage_write_is_logged_at_error_level(env):
    env.operations.chat_turn({}, {
        "session_id": "w-chat-log", "intent": "build",
        "messages": [{"role": "user", "content": "hi"}]})
    failures = _failures(env.lines)
    assert len(failures) == 1, env.lines
    assert "database is locked" in failures[0]
    assert "session=w-chat-log" in failures[0]


def test_the_other_rows_still_land_when_the_usage_write_fails(env):
    env.operations.chat_turn({}, {
        "session_id": "w-chat-rows", "intent": "build",
        "messages": [{"role": "user", "content": "hi"}]})
    assert len(env.audit) == 1, "the llm-activity audit row was dropped"
    assert env.audit[0]["response"] == "Done."


def test_a_failed_usage_write_does_not_fail_the_resume_turn(env):
    env.store.set_session_intent("w-resume", "triage")
    env.store.save_conversation("w-resume", [{"role": "user", "content": "hi"}])
    out = env.operations._resume_conversation(
        {}, {"session_id": "w-resume", "card_id": "c1"}, "approve", env.store,
        provider=object(), user_msg="continue")
    assert out["ok"] is True, out
    assert len(_failures(env.lines)) == 1
    assert len(env.audit) == 1


def test_propagate_policy_still_fails_the_round(env):
    sink = env.turn_sink.make_event_sink(
        env.store, {}, "w-prop", 1, provider_name="anthropic",
        audit_intent="build", audit_prompt="", actor_resolver=lambda: None,
        usage_error_policy="propagate")
    with pytest.raises(sqlite3.OperationalError):
        sink(_usage())


def test_an_unknown_policy_is_refused(env):
    with pytest.raises(ValueError):
        env.turn_sink.make_event_sink(
            env.store, {}, "w-bad", 1, provider_name="anthropic",
            audit_intent="build", audit_prompt="", actor_resolver=lambda: None,
            usage_error_policy="swallow")
