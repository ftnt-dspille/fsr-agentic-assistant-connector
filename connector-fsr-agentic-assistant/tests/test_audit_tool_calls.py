"""Per-tool-call audit detail (agentic-assistant-monitor §10.2 -- Phase 1).

Two layers:
  1. Storage -- `agent_tool_calls` rows linked to `agent_usage` via the row id
     returned by `record_agent_usage`; `list_agent_usage` nests them.
  2. The pure status/summary helpers that turn a ToolResultEvent into the
     audit-row fields (`_tool_result_status`, `_summarize_result`).

Storage is importable standalone; the helpers live in `operations.py`, which
needs the FortiSOAR `connectors` platform module at import time. We stub that
module so the helper tests run anywhere (not just on-box) -- mirroring the
synthetic-package pattern in `test_warmup_instance_rewarm.py`.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import types
from pathlib import Path

import pytest

# Storage layer: importable directly.
from storage import Storage  # noqa: E402


CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)


def _import_ops_helpers():
    """Import operations' pure helpers. The FortiSOAR platform `connectors`
    stub that makes this importable off-box comes from `tests/conftest.py`.

    This used to build its own stub and `sys.modules.update(...)` it in --
    REPLACING the shared one rather than adding to it. Because sys.modules is
    process-global and this module happens to import early, every test that ran
    afterwards inherited a stub carrying only `ConnectorError`, with
    `get_logger` and `Connector` silently gone. Order-dependent global
    mutation: harmless here, invisible everywhere else. Use the shared stub."""
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    return importlib.import_module("fsrpb_connector.operations")


# ── Storage layer ────────────────────────────────────────────────────────

@pytest.fixture
def store():
    return Storage(tempfile.mktemp(suffix=".db"))


def test_record_agent_usage_returns_row_id(store):
    uid = store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                                    "session_id": "s1", "model": "m", "turn": 1})
    assert uid is not None and isinstance(uid, int)
    uid2 = store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                                     "session_id": "s1", "model": "m", "turn": 2})
    assert uid2 == uid + 1


def test_tool_calls_linked_to_their_usage_row(store):
    uid = store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                                    "session_id": "s1", "model": "m", "turn": 1})
    store.record_agent_tool_calls(uid, 1, [
        {"tool_name": "get_record", "params": {"module": "alerts", "id": "1"},
         "result_status": "success", "result_summary": "ok", "latency_ms": 120},
        {"tool_name": "run_op", "params": None,
         "result_status": "error", "result_summary": "bad args", "latency_ms": 50},
    ])
    rows = store.list_agent_usage()
    assert len(rows) == 1
    detail = rows[0]["tool_call_detail"]
    assert len(detail) == 2
    assert detail[0]["tool_name"] == "get_record"
    assert detail[0]["params"] == {"module": "alerts", "id": "1"}
    assert detail[0]["result_status"] == "success"
    assert detail[0]["latency_ms"] == 120
    assert detail[1]["tool_name"] == "run_op"
    assert detail[1]["result_status"] == "error"
    assert detail[1]["params"] is None


def test_tool_calls_scoped_to_their_own_round_trip(store):
    # Two round-trips in one session, each with its own tool calls. The detail
    # must NOT bleed across usage rows.
    uid_a = store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                                      "session_id": "s1", "model": "m", "turn": 1})
    store.record_agent_tool_calls(uid_a, 1, [
        {"tool_name": "search", "result_status": "success", "latency_ms": 30}])
    uid_b = store.record_agent_usage({"ts": "2026-01-01T00:01:00Z",
                                      "session_id": "s1", "model": "m", "turn": 1})
    store.record_agent_tool_calls(uid_b, 1, [
        {"tool_name": "get_record", "result_status": "error", "latency_ms": 80},
        {"tool_name": "enrich", "result_status": "success", "latency_ms": 12}])
    rows = store.list_agent_usage()
    # newest-first → round-trip b is first
    assert rows[0]["tool_call_detail"][0]["tool_name"] == "get_record"
    assert len(rows[0]["tool_call_detail"]) == 2
    assert len(rows[1]["tool_call_detail"]) == 1
    assert rows[1]["tool_call_detail"][0]["tool_name"] == "search"


def test_no_tool_calls_yields_empty_detail(store):
    store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                              "session_id": "s1", "model": "m", "turn": 1})
    rows = store.list_agent_usage()
    assert rows[0]["tool_call_detail"] == []


def test_record_tool_calls_noop_without_usage_id(store):
    # Parent insert failed (uid None) -- must not raise, must not write orphans.
    store.record_agent_tool_calls(None, 1, [{"tool_name": "x"}])
    assert store.list_agent_usage() == []
    # also a no-op on an empty list
    store.record_agent_tool_calls(1, 1, [])
    assert store.list_agent_usage() == []


def test_tool_calls_cascade_delete_with_usage_row(store):
    uid = store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                                    "session_id": "s1", "model": "m", "turn": 1})
    store.record_agent_tool_calls(uid, 1, [{"tool_name": "x", "latency_ms": 1}])
    with store._conn() as c:
        c.execute("DELETE FROM agent_usage WHERE id=?", (uid,))
    # FK ON DELETE CASCADE -- the detail rows are gone with the parent.
    rows = store.list_agent_usage()
    assert rows == []


def test_tool_call_index_preserves_order(store):
    uid = store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                                    "session_id": "s1", "model": "m", "turn": 1})
    calls = [{"tool_name": f"tool_{i}", "latency_ms": i} for i in range(5)]
    store.record_agent_tool_calls(uid, 1, calls)
    detail = store.list_agent_usage()[0]["tool_call_detail"]
    assert [d["tool_name"] for d in detail] == [f"tool_{i}" for i in range(5)]
    assert [d["index"] for d in detail] == [0, 1, 2, 3, 4]


# ── Status / summary helpers (operations.py) ─────────────────────────────

@pytest.fixture(scope="module")
def ops():
    return _import_ops_helpers()


def test_result_status_error_on_error_key(ops):
    assert ops._tool_result_status({"error": "boom"}) == "error"


def test_result_status_error_on_ok_false(ops):
    assert ops._tool_result_status({"ok": False, "code": "superseded_by_approval"}) == "error"


def test_result_status_success_on_ok_true_with_null_error(ops):
    # A successful tool may legitimately return {"ok": True, "error": None}.
    assert ops._tool_result_status({"ok": True, "error": None}) == "success"


def test_result_status_success_on_plain_dict(ops):
    assert ops._tool_result_status({"rows": [1, 2, 3]}) == "success"


def test_result_status_success_on_non_dict(ops):
    assert ops._tool_result_status("a string result") == "success"
    assert ops._tool_result_status(None) == "success"
    assert ops._tool_result_status([1, 2]) == "success"


def test_summarize_result_truncates_long(ops):
    s = ops._summarize_result("x" * 900)
    assert len(s) == ops._AUDIT_RESULT_CAP + 1  # cap + ellipsis char
    assert s.endswith("…")


def test_summarize_result_keeps_short(ops):
    assert ops._summarize_result("short") == "short"
    assert ops._summarize_result({"a": "b"}) == '{"a": "b"}'
    assert ops._summarize_result(None) == ""


# -- Enriched audit fields (§10.3: response, agent_thinking, approvals_count) --

def test_response_and_thinking_roundtrip(store):
    store.record_agent_usage({
        "ts": "2026-01-01T00:00:00Z", "session_id": "s1", "model": "m", "turn": 1,
        "response": "The alert is benign.",
        "agent_thinking": "Analyzing indicators...",
        "approvals_count": 2,
    })
    row = store.list_agent_usage()[0]
    assert row["response"] == "The alert is benign."
    assert row["agent_thinking"] == "Analyzing indicators..."
    assert row["approvals_count"] == 2


def test_response_defaults_to_none_when_absent(store):
    store.record_agent_usage({"ts": "2026-01-01T00:00:00Z",
                              "session_id": "s1", "model": "m", "turn": 1})
    row = store.list_agent_usage()[0]
    assert row["response"] is None
    assert row["agent_thinking"] is None
    assert row["approvals_count"] == 0


def test_migration_adds_columns_to_existing_table(tmp_path):
    """An agent_usage table created BEFORE the §10.3 columns existed must
    get ALTERed on the next Storage open (additive migration)."""
    import sqlite3
    db = tmp_path / "migrate.db"
    # Create an old-schema table (no agent_thinking / approvals_count)
    with sqlite3.connect(db) as c:
        c.executescript(
            "CREATE TABLE agent_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT NOT NULL, session_id TEXT NOT NULL, user_iri TEXT, "
            "user_name TEXT, intent TEXT, model TEXT, turn INTEGER, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, "
            "cache_write INTEGER, cost_usd REAL, latency_ms REAL, "
            "stop_reason TEXT, status TEXT, error TEXT, tool_calls_json TEXT, "
            "tags_json TEXT, prompt TEXT, response TEXT, "
            "activity_log_iri TEXT, created_at REAL NOT NULL);")
        c.execute("INSERT INTO agent_usage (ts,session_id,model,turn,created_at) "
                   "VALUES ('2026-01-01','s0','m',0,1)")
        c.commit()
    # Open with Storage -> should ALTER
    s = Storage(db)
    # Old row should still be there with nulls for the new columns
    old = s.list_agent_usage()[0]
    assert old["session_id"] == "s0"
    assert old["agent_thinking"] is None
    assert old["approvals_count"] == 0
    # New writes should store the enriched fields
    s.record_agent_usage({"ts": "2026-01-02", "session_id": "s1", "model": "m",
                          "turn": 1, "response": "ok", "approvals_count": 1})
    new = s.list_agent_usage()[0]
    assert new["response"] == "ok"
    assert new["approvals_count"] == 1
