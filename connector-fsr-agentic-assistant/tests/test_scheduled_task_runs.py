"""list_scheduled_task_runs: the per-task run history the widget's history
drawer renders (scheduled-agent-tasks plan §8.4).

Each cron fire is a stored chat session whose id is ``sched:<name>:<uuid>``.
This op returns those runs newest-first with their outcome (stop_reason +
verdict) and a ``parked`` flag for HITL-gated runs the analyst can resume in
chat. It is a pure sqlite read -- no scheduler transport -- so the history
drawer works hermetically and on a box whose beat is stopped.

The cases here pin the contract the widget history drawer depends on:
  - runs are filtered to ONE task by the ``sched:<name>:`` prefix (no
    cross-task leakage, even with overlapping names like "Daily" / "Daily X")
  - newest-first ordering by last activity
  - stop_reason is the last turn's terminal; verdict is the last assistant text
  - parked is true exactly when the session is in suspended_sessions
  - a missing task def is not_found; a task with zero fires is an empty list
"""
import json
import sys
import time
import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def operations():
    """Load operations.py as a package submodule so its relative imports
    (``from .pydantic_models`` / ``from .storage``) resolve off-platform.
    Mirrors the test_enhancement_offer_accept pattern."""
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        sys.modules["connectors.core.connector"].ConnectorError = type(
            "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_runs")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_runs"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_runs.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_runs.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_runs.operations"] = mod
    spec.loader.exec_module(mod)
    # Force the storage submodule to load NOW (the op imports it lazily on its
    # first call), so the per-test `store` fixture can monkeypatch its singleton
    # before any op runs -- otherwise the first test reads a fresh default
    # Storage instead of the temp one.
    importlib.import_module("_fsrpb_runs.storage")
    return mod


import storage as S  # noqa: E402


@pytest.fixture()
def store(operations, tmp_path, monkeypatch):
    """A temp sqlite store the op resolves to via default_storage().

    The op loads storage as its own package submodule (``_fsrpb_runs.storage``),
    a distinct module object from the top-level ``storage`` this file imports,
    so the singleton must be patched on the module the op actually reads."""
    st = S.Storage(tmp_path / "runs.db")
    pkg_storage = sys.modules.get("_fsrpb_runs.storage") or S
    monkeypatch.setattr(pkg_storage, "_DEFAULT_STORAGE", st)
    return st


def _def(store, name, **kw):
    store.upsert_scheduled_task({
        "name": name, "cron": kw.get("cron", "0 9 * * 1"),
        "intent": kw.get("intent", "triage"),
        "prompt": kw.get("prompt", "review alerts"),
        "agent_config": "fsrpb-anthropic",
        "allow_mutating": False, "wf_iri": "/api/3/workflows/x",
        "timezone": "UTC", "enabled": True,
    })


def _run(store, name, uid, *, stop_reason="end_turn", verdict="no findings",
         user_text="run me", ts=None):
    """Seed one cron-fire chat session with a user turn + an assistant verdict
    + a chat_turn row carrying the terminal stop_reason."""
    sid = f"sched:{name}:{uid}"
    t = ts if ts is not None else time.time()
    with store._write() as c:
        c.execute(
            "INSERT INTO session_transcript "
            "(session_id, role, payload_json, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (sid, json.dumps({"text": user_text}), t),
        )
        c.execute(
            "INSERT INTO session_transcript "
            "(session_id, role, payload_json, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (sid, json.dumps({"transcript": [{"type": "text", "text": verdict}]}),
             t + 1),
        )
        c.execute(
            "INSERT OR REPLACE INTO chat_turns "
            "(session_id, turn, model, input_tokens, output_tokens, "
            " cache_read, cache_write, stop_reason, tags_json, "
            " tool_calls_json, created_at) "
            "VALUES (?, 1, 'claude', 1, 2, 0, 0, ?, '{}', '[]', ?)",
            (sid, stop_reason, t + 1),
        )
    return sid


def _park(store, session_id):
    """Mark a session as HITL-parked (resumable) by inserting a suspended row."""
    with store._write() as c:
        c.execute(
            "INSERT OR REPLACE INTO suspended_sessions "
            "(approval_id, session_id, payload, created_at) VALUES (?, ?, ?, ?)",
            ("apr:" + session_id, session_id, b"\x80\x04N.", time.time()),
        )


def test_returns_runs_newest_first_with_outcome(operations, store):
    _def(store, "Weekly sweep")
    _run(store, "Weekly sweep", "u1", verdict="all clear", ts=time.time() - 1000)
    _run(store, "Weekly sweep", "u2", verdict="IOC found: 1.2.3.4",
         stop_reason="end_turn", ts=time.time() - 10)

    out = operations.list_scheduled_task_runs({}, {"name": "Weekly sweep"})

    assert out["ok"] is True
    runs = out["runs"]
    assert [r["session_id"] for r in runs] == ["sched:Weekly sweep:u2",
                                              "sched:Weekly sweep:u1"]
    newest = runs[0]
    assert newest["verdict"] == "IOC found: 1.2.3.4"
    assert newest["stop_reason"] == "end_turn"
    assert newest["title"] == "run me"
    assert newest["entries"] == 2
    assert newest["parked"] is False
    # The summary shape list_sessions already provides must still be present.
    assert "started_at" in newest and "last_activity" in newest


def test_parked_run_flagged_resumable(operations, store):
    _def(store, "Bandwidth watch")
    parked_sid = _run(store, "Bandwidth watch", "p1",
                      stop_reason="awaiting_action_card", verdict="gate open")
    _park(store, parked_sid)
    _run(store, "Bandwidth watch", "p2", verdict="done", ts=time.time() - 5000)

    runs = operations.list_scheduled_task_runs({}, {"name": "Bandwidth watch"})["runs"]
    by_id = {r["session_id"]: r for r in runs}
    assert by_id[parked_sid]["parked"] is True
    assert by_id[parked_sid]["stop_reason"] == "awaiting_action_card"
    assert by_id["sched:Bandwidth watch:p2"]["parked"] is False


def test_no_cross_task_leakage_with_overlapping_names(operations, store):
    """A task named "Daily" must not pick up runs of "Daily alert review" --
    the prefix match is on the full ``sched:<name>:`` boundary."""
    _def(store, "Daily")
    _def(store, "Daily alert review")
    _run(store, "Daily", "a", verdict="daily ok")
    _run(store, "Daily alert review", "b", verdict="review ok")

    daily = operations.list_scheduled_task_runs({}, {"name": "Daily"})["runs"]
    review = operations.list_scheduled_task_runs(
        {}, {"name": "Daily alert review"})["runs"]
    assert [r["session_id"] for r in daily] == ["sched:Daily:a"]
    assert [r["session_id"] for r in review] == ["sched:Daily alert review:b"]


def test_not_found_when_no_task_def(operations, store):
    out = operations.list_scheduled_task_runs({}, {"name": "ghost"})
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"


def test_empty_runs_for_task_with_zero_fires(operations, store):
    _def(store, "Never ran")
    out = operations.list_scheduled_task_runs({}, {"name": "Never ran"})
    assert out["ok"] is True
    assert out["runs"] == []


def test_limit_caps_the_run_list(operations, store):
    _def(store, "Chatty")
    for i in range(5):
        _run(store, "Chatty", f"u{i}", ts=time.time() + i)
    out = operations.list_scheduled_task_runs({}, {"name": "Chatty", "limit": 2})
    assert len(out["runs"]) == 2
