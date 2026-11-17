"""X4: playbook_health reads run health for the playbooks the assistant bottled.

The framework's list_playbook_runs and why_did_playbook_fail are replaced with
fakes shaped like their real returns (the run rows carry status, modified,
error_message; the diagnosis carries step_diagnostics). The bottled-playbook
table is the real sqlite Storage.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def health(monkeypatch):
    return importlib.import_module("connector_fsr_agentic_assistant.ops_playbook_health")


def _run(task, status, modified, error=None, name="Block Malicious IP"):
    return {"task_id": task, "name": name, "status": status,
            "modified": modified, "error_message": error, "pk": None}


def _fake_tools(runs_by_uuid, diag=None, diag_raises=False):
    calls = {"list": [], "why": []}

    def list_runs(playbook_uuid=None, limit=20, include_finished=False, **_):
        calls["list"].append({"playbook_uuid": playbook_uuid, "limit": limit,
                              "include_finished": include_finished})
        if isinstance(runs_by_uuid, Exception):
            raise runs_by_uuid
        return {"playbook_uuid": playbook_uuid, "count": 0,
                "runs": runs_by_uuid.get(playbook_uuid, [])}

    def why(playbook_or_id, yaml_text=None):
        calls["why"].append(playbook_or_id)
        if diag_raises:
            raise RuntimeError("env unavailable")
        return diag if diag is not None else {"ok": True, "step_diagnostics": []}

    return list_runs, why, calls


def _row(wf):
    return {"workflow_uuid": wf, "session_id": "sess-1", "saved_at": 1.0}


def test_failures_since_last_success_and_last_failure_reason(health, monkeypatch):
    runs = {"wf-1": [
        _run("t-new", "failed", "2026-11-12T10:00:00", error="FortiGate 401"),
        _run("t-ok", "finished", "2026-11-11T10:00:00"),
        _run("t-old", "failed", "2026-11-10T10:00:00", error="older"),
    ]}
    diag = {"ok": False, "step_diagnostics": [
        {"step": "Block IP on FortiGate", "ok": False, "code": "render_error"},
        {"step": "Set Inputs", "ok": True, "code": "ok"},
    ]}
    list_runs, why, calls = _fake_tools(runs, diag=diag)
    monkeypatch.setattr(health, "_framework_tools", lambda: (list_runs, why))
    monkeypatch.setattr("connector_fsr_agentic_assistant.storage.Storage.list_bottled_playbooks",
                        lambda self, limit=50: [_row("wf-1")])

    out = health.playbook_health({}, {})
    pb = out["playbooks"][0]
    assert out["count"] == 1
    assert pb["runs"] == 3 and pb["failures"] == 2
    assert pb["failures_since_last_ok"] == 1          # only the newer failure broke after the last success
    assert pb["health"] == "failing"
    assert pb["name"] == "Block Malicious IP"
    assert pb["last_failure"]["task_id"] == "t-new"
    assert pb["last_failure"]["error_message"] == "FortiGate 401"
    assert pb["last_failure"]["failing_steps"] == ["Block IP on FortiGate"]
    assert pb["last_failure"]["diagnosis_ok"] is True
    assert why and calls["why"] == ["t-new"]          # diagnosis asked for the most recent failure only
    assert calls["list"][0]["include_finished"] is True


def test_all_green_is_ok_with_no_failure_detail(health, monkeypatch):
    runs = {"wf-2": [_run("a", "finished", "2026-11-12T10:00:00")]}
    list_runs, why, calls = _fake_tools(runs)
    monkeypatch.setattr(health, "_framework_tools", lambda: (list_runs, why))
    monkeypatch.setattr("connector_fsr_agentic_assistant.storage.Storage.list_bottled_playbooks",
                        lambda self, limit=50: [_row("wf-2")])
    pb = health.playbook_health({}, {})["playbooks"][0]
    assert pb["health"] == "ok"
    assert pb["failures"] == 0 and pb["failures_since_last_ok"] == 0
    assert pb["last_failure"] is None
    assert calls["why"] == []                          # nothing failed, nothing diagnosed


def test_no_runs_and_failure_with_failed_diagnosis(health, monkeypatch):
    runs = {"wf-3": [], "wf-4": [_run("f", "failed", "2026-11-12T10:00:00", error="boom")]}
    list_runs, why, _ = _fake_tools(runs, diag_raises=True)
    monkeypatch.setattr(health, "_framework_tools", lambda: (list_runs, why))
    monkeypatch.setattr("connector_fsr_agentic_assistant.storage.Storage.list_bottled_playbooks",
                        lambda self, limit=50: [_row("wf-3"), _row("wf-4")])
    out = health.playbook_health({}, {})
    by = {p["workflow_uuid"]: p for p in out["playbooks"]}
    assert by["wf-3"]["health"] == "no_runs" and by["wf-3"]["runs"] == 0
    assert by["wf-4"]["last_failure"]["diagnosis_ok"] is False
    assert "env unavailable" in by["wf-4"]["last_failure"]["diagnosis_error"]
    assert by["wf-4"]["last_failure"]["error_message"] == "boom"


def test_run_listing_error_is_reported_per_playbook_not_raised(health, monkeypatch):
    list_runs, why, _ = _fake_tools(RuntimeError("box unreachable"))
    monkeypatch.setattr(health, "_framework_tools", lambda: (list_runs, why))
    monkeypatch.setattr("connector_fsr_agentic_assistant.storage.Storage.list_bottled_playbooks",
                        lambda self, limit=50: [_row("wf-5")])
    pb = health.playbook_health({}, {})["playbooks"][0]
    assert pb["health"] == "unknown"
    assert "box unreachable" in pb["error"]


def test_limit_is_clamped_and_reaches_storage(health, monkeypatch):
    seen = {}
    list_runs, why, _ = _fake_tools({})
    monkeypatch.setattr(health, "_framework_tools", lambda: (list_runs, why))

    def fake_list(self, limit=50):
        seen["limit"] = limit
        return []
    monkeypatch.setattr("connector_fsr_agentic_assistant.storage.Storage.list_bottled_playbooks",
                        fake_list)
    health.playbook_health({}, {"limit": 999})
    assert seen["limit"] == 50
    with pytest.raises(Exception, match="invalid PlaybookHealthParams"):
        health.playbook_health({}, {"limit": "nope"})   # a bad limit is refused, not guessed


def test_saved_playbooks_are_recorded_and_listed_newest_first(tmp_path):
    from connector_fsr_agentic_assistant.storage import Storage
    st = Storage(tmp_path / "s.db")
    st.set_session_saved_playbook("sess-a", "wf-a", "playbooks: []")
    st.set_session_saved_playbook("sess-b", "wf-b", "playbooks: []")
    st.set_session_saved_playbook("sess-a", "wf-a2", "playbooks: []")  # same session, new save
    rows = st.list_bottled_playbooks(10)
    assert [r["workflow_uuid"] for r in rows] == ["wf-a2", "wf-b", "wf-a"]
    assert st.list_bottled_playbooks(1)[0]["workflow_uuid"] == "wf-a2"
