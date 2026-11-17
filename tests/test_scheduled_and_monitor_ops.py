"""Unit tests for the scheduled-tasks + monitor ops.

Stubs the crudhub transport (`operations._make_request`) with a recording
fake so the /api/wf/api/scheduled/ + /api/3/llm_activity_logs + push_playbook
calls run hermetically — no box contact. The sqlite store (scheduled_tasks,
agent_usage) is exercised against a tmp db.
"""
from __future__ import annotations

import copy

import pytest
import ops_autonomy
import ops_common
import ops_health
import ops_playbooks
import ops_resume
import ops_scheduling


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh sqlite so tests don't share ~/.connector_fsr_agentic_assistant.db."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


class _FakeMR:
    """Recording crudhub stand-in: (url, method, body?, params?) → response.

    `routes` maps a url-substring or (method, url-substring) to either a
    constant response or a callable(body) → response. Unmatched GET → hydra
    collection, unmatched POST → a created-record stub.
    """
    def __init__(self):
        self.calls = []
        self.routes = {}
        # the runner playbook push returns a workflow_collection record
        self.workflow_uuid = "wf-uuid-1"
        self.collection_uuid = "coll-uuid-1"
        # the next schedule row id (Fernet token) to hand out
        self._sched_id_seq = 0

    def add(self, key, response):
        self.routes[key] = response

    def __call__(self, url, method, body=None, **kw):
        method = (method or "GET").upper()
        self.calls.append((method, url, copy.deepcopy(body), kw.get("params")))
        # push_playbook: POST /api/3/workflow_collections → created record
        if method == "POST" and "/api/3/workflow_collections" in url:
            return {
                "uuid": self.collection_uuid,
                "workflows": [{"uuid": self.workflow_uuid, "name": "Scheduled Agent Runner"}],
            }
        # schedules: GET list → hydra:member
        if method == "GET" and "/api/wf/api/scheduled" in url:
            return {"hydra:member": self.routes.get(("GET", "/scheduled"), [])}
        # schedules: POST create → created row
        if method == "POST" and "/api/wf/api/scheduled" in url and "trigger" not in url:
            self._sched_id_seq += 1
            row = {"id": f"fernet-{self._sched_id_seq}",
                   "name": (body or {}).get("name"),
                   "enabled": (body or {}).get("enabled", True),
                   "last_run_at": None, "total_run_count": 0,
                   "crontab": (body or {}).get("crontab", {}),
                   "kwargs": (body or {}).get("kwargs", {})}
            self.routes.setdefault(("GET", "/scheduled"), []).append(row)
            return row
        # schedules: POST trigger-now → ack
        if method == "POST" and "trigger-now" in url:
            return {"message": "The associated workflow is successfully triggered"}
        # schedules: PUT (update) → the body back
        if method == "PUT" and "/api/wf/api/scheduled" in url:
            return body
        # schedules: DELETE → None
        if method == "DELETE" and "/api/wf/api/scheduled" in url:
            return None
        # workflow_collections DELETE (hard-purge runner) → None
        if method == "DELETE" and "/api/3/workflow_collections" in url:
            return None
        # llm_activity_logs POST → created record
        if method == "POST" and "/api/3/llm_activity_logs" in url:
            return {"@id": f"/api/3/llm_activity_logs/log-{self._sched_id_seq}",
                    "@type": "LLMActivityLog", **(body or {})}
        # explicit route override
        for key, resp in self.routes.items():
            if key == (method, url) or (isinstance(key, tuple) and key[1] in url):
                return resp(body) if callable(resp) else resp
        return {}


@pytest.fixture
def fake_transport(monkeypatch):
    """Stub operations._make_request to return a recording _FakeMR."""
    mr = _FakeMR()
    monkeypatch.setattr(ops_common, "_make_request", lambda: mr)
    monkeypatch.setattr(ops_health, "_make_request", lambda: mr)
    return mr


# --- scheduled-tasks: storage layer ---------------------------------------

def test_scheduled_task_storage_roundtrip(tmp_db):
    from connector_fsr_agentic_assistant.storage import default_storage
    s = default_storage()
    s.upsert_scheduled_task({
        "name": "weekly-sweep", "cron": "0 9 * * 1", "intent": "triage",
        "prompt": "Review escalated alerts.", "agent_config": "fsrpb-anthropic",
        "wf_iri": "/api/3/workflows/abc",
    })
    t = s.get_scheduled_task("weekly-sweep")
    assert t and t["prompt"] == "Review escalated alerts."
    assert t["enabled"] is True and t["allow_mutating"] is False
    assert len(s.list_scheduled_tasks()) == 1
    # upsert (update) keeps the row, replaces fields
    s.upsert_scheduled_task({
        "name": "weekly-sweep", "cron": "0 10 * * 1", "intent": "triage",
        "prompt": "New prompt", "agent_config": "fsrpb-anthropic",
        "wf_iri": "/api/3/workflows/abc",
    })
    assert s.get_scheduled_task("weekly-sweep")["prompt"] == "New prompt"
    assert len(s.list_scheduled_tasks()) == 1
    assert s.delete_scheduled_task("weekly-sweep") is True
    assert s.get_scheduled_task("weekly-sweep") is None


# --- scheduled-tasks: cron parsing ----------------------------------------

def test_parse_cron_5_fields():
    from connector_fsr_agentic_assistant.operations import _parse_cron
    assert _parse_cron("7 2 * * *") == {
        "minute": "7", "hour": "2", "day_of_month": "*",
        "month_of_year": "*", "day_of_week": "*"}


def test_parse_cron_rejects_non_5_fields():
    from connector_fsr_agentic_assistant import operations as ops
    for bad in ("7 2 *", "7 2 * * * *", "", "1 2 3 4 5 6"):
        with pytest.raises(Exception, match="5 fields"):
            ops._parse_cron(bad)


# --- scheduled-tasks: ops (transport stubbed) -----------------------------

def test_create_scheduled_task_requires_transport(tmp_db):
    """Off-platform with no transport → crudhub_unavailable envelope."""
    from connector_fsr_agentic_assistant import operations as ops
    import connector_fsr_agentic_assistant.operations as ops_mod
    # Force _make_request to return None (no transport)
    orig = ops_mod._make_request
    ops_common._make_request = ops_health._make_request = lambda: None
    try:
        out = ops.create_scheduled_task({}, {"mode": "live",
            "name": "t", "cron": "7 2 * * *", "intent": "triage",
            "prompt": "x", "agent_config": "fsrpb-anthropic"})
    finally:
        ops_common._make_request = ops_health._make_request = orig
    assert out.get("ok") is False and out.get("error", {}).get("code") == "crudhub_unavailable"


def test_create_scheduled_task_end_to_end(tmp_db, fake_transport, monkeypatch):
    """create → list → get → run-now → delete, all against the stub."""
    from connector_fsr_agentic_assistant import operations as ops

    # Stub the runner-playbook compiler so we don't need fsr_playbooks importable.
    monkeypatch.setattr(ops_scheduling, "_compile_runner_playbook",
        lambda name, intent, prompt, cfg, allow_mutating=True: {"ok": True,
            "workflow_json": {"name": name, "workflows": []},
            "errors": []})
    # push_playbook is a live op; stub it to return a created record.
    monkeypatch.setattr(ops_autonomy, "push_playbook",
        lambda c, p: {"ok": True, "collection_uuid": "coll-uuid-1",
                      "workflow_uuid": "wf-uuid-1",
                      "workflows": [{"uuid": "wf-uuid-1"}], "raw": {}})
    monkeypatch.setattr(ops, "push_playbook",
        lambda c, p: {"ok": True, "collection_uuid": "coll-uuid-1",
                      "workflow_uuid": "wf-uuid-1",
                      "workflows": [{"uuid": "wf-uuid-1"}], "raw": {}})
    monkeypatch.setattr(ops_playbooks, "push_playbook",
        lambda c, p: {"ok": True, "collection_uuid": "coll-uuid-1",
                      "workflow_uuid": "wf-uuid-1",
                      "workflows": [{"uuid": "wf-uuid-1"}], "raw": {}})
    monkeypatch.setattr(ops_resume, "push_playbook",
        lambda c, p: {"ok": True, "collection_uuid": "coll-uuid-1",
                      "workflow_uuid": "wf-uuid-1",
                      "workflows": [{"uuid": "wf-uuid-1"}], "raw": {}})
    monkeypatch.setattr(ops_scheduling, "push_playbook",
        lambda c, p: {"ok": True, "collection_uuid": "coll-uuid-1",
                      "workflow_uuid": "wf-uuid-1",
                      "workflows": [{"uuid": "wf-uuid-1"}], "raw": {}})

    cfg = {"anthropic_api_key": "sk-test", "model": "claude"}
    base = {"name": "weekly-sweep", "cron": "0 9 * * 1", "intent": "triage",
            "prompt": "Review escalated alerts.", "agent_config": "fsrpb-anthropic"}

    # create (live mode — not mock)
    out = ops.create_scheduled_task(cfg, {**base, "mode": "live"})
    assert out["ok"] is True and out["name"] == "weekly-sweep"
    assert out["wf_iri"] == "/api/3/workflows/wf-uuid-1"
    assert out["schedule_name"] == "agent-sched:weekly-sweep"

    # def persisted
    from connector_fsr_agentic_assistant.storage import default_storage
    t = default_storage().get_scheduled_task("weekly-sweep")
    assert t and t["prompt"] == "Review escalated alerts."

    # list joins live state
    listing = ops.list_scheduled_tasks({}, {"mode": "live"})
    assert len(listing["tasks"]) == 1
    assert listing["tasks"][0]["schedule_enabled"] is True

    # get one
    got = ops.get_scheduled_task({}, {"name": "weekly-sweep", "mode": "live"})
    assert got["task"]["name"] == "weekly-sweep"

    # run now triggers
    trig = ops.run_scheduled_task_now({}, {"name": "weekly-sweep", "mode": "live"})
    assert trig["ok"] is True
    assert "triggered" in trig["trigger"]["message"]

    # delete removes schedule + def
    dele = ops.delete_scheduled_task({}, {"name": "weekly-sweep", "mode": "live"})
    assert dele["ok"] is True and dele["deleted"] is True
    assert default_storage().get_scheduled_task("weekly-sweep") is None


def test_create_scheduled_task_idempotent_update(tmp_db, fake_transport, monkeypatch):
    """Re-creating an existing task updates it (get_or_create the schedule)."""
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_scheduling, "_compile_runner_playbook",
        lambda name, intent, prompt, cfg, allow_mutating=True: {"ok": True,
            "workflow_json": {}, "errors": []})
    monkeypatch.setattr(ops_autonomy, "push_playbook",
        lambda c, p: {"ok": True, "workflow_uuid": "wf-uuid-2",
                      "collection_uuid": "coll-2", "workflows": [], "raw": {}})
    monkeypatch.setattr(ops, "push_playbook",
        lambda c, p: {"ok": True, "workflow_uuid": "wf-uuid-2",
                      "collection_uuid": "coll-2", "workflows": [], "raw": {}})
    monkeypatch.setattr(ops_playbooks, "push_playbook",
        lambda c, p: {"ok": True, "workflow_uuid": "wf-uuid-2",
                      "collection_uuid": "coll-2", "workflows": [], "raw": {}})
    monkeypatch.setattr(ops_resume, "push_playbook",
        lambda c, p: {"ok": True, "workflow_uuid": "wf-uuid-2",
                      "collection_uuid": "coll-2", "workflows": [], "raw": {}})
    monkeypatch.setattr(ops_scheduling, "push_playbook",
        lambda c, p: {"ok": True, "workflow_uuid": "wf-uuid-2",
                      "collection_uuid": "coll-2", "workflows": [], "raw": {}})
    cfg = {"anthropic_api_key": "sk-test", "model": "claude"}
    base = {"name": "daily-check", "cron": "7 2 * * *", "intent": "triage",
            "prompt": "p1", "agent_config": "fsrpb-anthropic", "mode": "live"}
    ops.create_scheduled_task(cfg, dict(base))
    # second create with a new prompt → update path (PUT, not a second POST)
    ops.create_scheduled_task(cfg, {**base, "prompt": "p2"})
    # exactly one schedule row was created (the second call PUT-updated it)
    posts = [c for c in fake_transport.calls
             if c[0] == "POST" and "/api/wf/api/scheduled" in c[1] and "trigger" not in c[1]]
    puts = [c for c in fake_transport.calls if c[0] == "PUT" and "/api/wf/api/scheduled" in c[1]]
    assert len(posts) == 1 and len(puts) == 1
    from connector_fsr_agentic_assistant.storage import default_storage
    assert default_storage().get_scheduled_task("daily-check")["prompt"] == "p2"


# --- monitor: agent_usage telemetry ---------------------------------------

def test_list_usage_and_summary(tmp_db):
    from connector_fsr_agentic_assistant.storage import default_storage
    s = default_storage()
    s.record_agent_usage({"ts": "2026-07-27T10:00:00Z", "session_id": "s1",
                          "model": "claude", "turn": 1, "input_tokens": 100,
                          "output_tokens": 20, "cost_usd": 0.01, "intent": "triage",
                          "status": "success", "tool_calls": [{"name": "x"}],
                          "tags": {"k": "v"}})
    s.record_agent_usage({"ts": "2026-07-27T11:00:00Z", "session_id": "s1",
                          "model": "claude", "turn": 2, "input_tokens": 200,
                          "output_tokens": 40, "cost_usd": 0.02, "intent": "triage",
                          "status": "success"})
    from connector_fsr_agentic_assistant import operations as ops
    listing = ops.list_usage({}, {"limit": 10, "mode": "live"})
    assert len(listing["usage"]) == 2
    assert listing["usage"][0]["input_tokens"] == 200  # newest-first
    summary = ops.get_usage_summary({}, {"bucket": "day", "metric": "tokens",
                                            "mode": "live"})
    assert summary["summary"][0]["value"] == 360  # 120 + 240


def test_list_usage_filters_by_intent(tmp_db):
    from connector_fsr_agentic_assistant.storage import default_storage
    s = default_storage()
    s.record_agent_usage({"ts": "2026-07-27T10:00:00Z", "session_id": "s1",
                          "model": "claude", "input_tokens": 100,
                          "output_tokens": 0, "intent": "triage", "status": "success"})
    s.record_agent_usage({"ts": "2026-07-27T11:00:00Z", "session_id": "s2",
                          "model": "claude", "input_tokens": 50,
                          "output_tokens": 0, "intent": "build", "status": "success"})
    from connector_fsr_agentic_assistant import operations as ops
    triage = ops.list_usage({}, {"intent": "triage", "mode": "live"})
    assert len(triage["usage"]) == 1 and triage["usage"][0]["intent"] == "triage"


# --- monitor: llm_activity_log dual-write ---------------------------------

def test_llm_activity_log_payload_camelcase():
    from connector_fsr_agentic_assistant.operations import _llm_activity_log_payload
    p = _llm_activity_log_payload({
        "model": "claude-sonnet-4-5", "input_tokens": 100,
        "output_tokens": 20, "cost_usd": 0.01, "latency_ms": 1500,
        "status": "success", "session_id": "s1", "prompt": "[]", "response": "r",
        "provider": "anthropic",
    })
    assert p["inputTokens"] == 100 and p["outputTokens"] == 20
    assert p["totalTokens"] == 120 and p["costUSD"] == 0.01
    assert p["latencyMs"] == 1500 and p["modelName"] == "claude-sonnet-4-5"
    assert p["correlationID"] == "s1"


def test_log_llm_activity_dual_writes(tmp_db, fake_transport):
    """log_llm_activity POSTs to /api/3/llm_activity_logs AND sqlite."""
    from connector_fsr_agentic_assistant import operations as ops
    ops.log_llm_activity({}, {
        "ts": "2026-07-27T10:00:00Z", "session_id": "s1", "model": "claude",
        "turn": 1, "input_tokens": 100, "output_tokens": 20,
        "cost_usd": 0.01, "intent": "triage", "status": "success",
        "prompt": "[]", "response": "r", "provider": "anthropic",
        "tool_calls": [{"name": "x"}], "tags": {"k": "v"},
    })
    # platform POST happened
    posts = [c for c in fake_transport.calls
             if c[0] == "POST" and "/api/3/llm_activity_logs" in c[1]]
    assert len(posts) == 1
    assert posts[0][2]["inputTokens"] == 100
    # sqlite row written with the activity_log_iri
    from connector_fsr_agentic_assistant.storage import default_storage
    rows = default_storage().list_agent_usage(limit=5)
    assert len(rows) == 1
    assert rows[0]["activity_log_iri"].startswith("/api/3/llm_activity_logs/")


def test_log_llm_activity_survives_no_transport(tmp_db, monkeypatch):
    """Best-effort: even with no transport, the sqlite write still lands."""
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_common, "_make_request", lambda: None)
    monkeypatch.setattr(ops_health, "_make_request", lambda: None)
    ops.log_llm_activity({}, {
        "ts": "2026-07-27T10:00:00Z", "session_id": "s1", "model": "claude",
        "input_tokens": 100, "output_tokens": 20, "intent": "triage",
        "status": "success",
    })
    from connector_fsr_agentic_assistant.storage import default_storage
    rows = default_storage().list_agent_usage(limit=5)
    assert len(rows) == 1 and rows[0]["activity_log_iri"] is None


# --- monitor: pending (HITL-parked) ----------------------------------------

def test_list_pending_empty_when_no_suspended(tmp_db):
    from connector_fsr_agentic_assistant import operations as ops
    out = ops.list_pending({}, {"mode": "live"})
    assert out["pending"] == []
