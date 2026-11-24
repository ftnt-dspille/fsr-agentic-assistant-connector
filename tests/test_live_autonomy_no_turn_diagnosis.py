"""When the autonomy harness waits for an auto-triage turn and none comes, it
names which of the four cases it was instead of timing out silently: filter
skip, storm routing, already triaged, or the trigger playbook never calling
(or failing in) auto_triage. Fake box responses per case."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def la():
    spec = importlib.util.spec_from_file_location("live_autonomy", _SCRIPTS / "live_autonomy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(result, status="finished", step_status="finished", message=None):
    return {"id": "/wf/api/workflows/9/", "status": status,
            "steps": [{"name": "Start", "status": "finished", "result": {}},
                      {"name": "auto_triage", "status": step_status,
                       "result": result, "message": message}]}


def _facts(**kw):
    base = {"alert": {"severity": "High", "source": "Agentic Assistant Test"},
            "comments": [], "initiator": False, "runs": [], "errors": []}
    base.update(kw)
    return base


def test_filter_skip_read_from_the_trigger_run(la):
    case, ev = la._classify_no_turn(_facts(runs=[_run({"data": {"ok": True, "skipped": "severity"}})]))
    assert case == "filter_skip" and "skipped=severity" in ev


def test_filter_skip_from_the_alert_when_no_run_was_recorded(la):
    case, ev = la._classify_no_turn(_facts(alert={"severity": "Medium", "source": "Agentic Assistant Test"}))
    assert case == "filter_skip" and "severity='Medium'" in ev
    case, _ = la._classify_no_turn(_facts(alert={"severity": "High", "source": "Other SIEM"}))
    assert case == "filter_skip"


@pytest.mark.parametrize("skip", ["grouped", "storm_cap"])
def test_storm_routing_from_the_trigger_run(la, skip):
    case, ev = la._classify_no_turn(_facts(runs=[_run({"ok": True, "skipped": skip})]))
    assert case == "storm" and skip in ev


def test_storm_from_the_alert_comment(la):
    case, _ = la._classify_no_turn(_facts(comments=["Grouped with an earlier related alert: it shares"]))
    assert case == "storm"


def test_already_triaged_from_run_and_from_claim(la):
    case, _ = la._classify_no_turn(_facts(runs=[_run({"ok": True, "skipped": "already_triaged"})]))
    assert case == "already_triaged"
    case, _ = la._classify_no_turn(_facts(initiator=True))
    assert case == "already_triaged"


def test_connector_step_error_carries_the_message(la):
    case, ev = la._classify_no_turn(_facts(runs=[_run(None, status="failed", step_status="failed",
                                                      message="config 'x' not found")]))
    assert case == "trigger_step_error" and "config 'x' not found" in ev


def test_step_still_active_is_not_called_a_skip_or_error(la):
    case, ev = la._classify_no_turn(_facts(runs=[_run({}, status="active", step_status="active")]))
    assert case == "turn_in_progress" and "still active" in ev


def test_finished_run_with_dropped_data_defers_to_the_alert_facts(la):
    """FortiSOAR drops a finished run's step results, so the run proves the
    trigger fired and the alert's own facts say why there was no turn."""
    gone = _run({}, status="finished")
    gone["by"] = "time"
    case, _ = la._classify_no_turn(_facts(runs=[gone], alert={"severity": "Medium",
                                                              "source": "Agentic Assistant Test"}))
    assert case == "filter_skip"
    case, _ = la._classify_no_turn(_facts(runs=[gone], comments=["Triage deferred: too many"]))
    assert case == "storm"
    case, ev = la._classify_no_turn(_facts(runs=[gone]))
    assert case == "unknown" and "/wf/api/workflows/9/" in ev


def test_trigger_never_ran(la):
    case, ev = la._classify_no_turn(_facts())
    assert case == "trigger_not_run" and "never ran" in ev


def test_smoke_reason_names_the_case(la):
    ok, why = la._smoke_verdict({"turned": False, "no_turn": {"case": "storm", "evidence": "grouped"}})
    assert not ok and "storm: grouped" in why


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _Sess:
    """Serves the playbook-run list, a run detail per id, and nothing else."""
    def __init__(self, runs):
        self.runs = runs

    def get(self, url, params=None, **_):
        if url.endswith("/api/wf/api/workflows/"):
            return _Resp({"hydra:member": [{"@id": r["@id"], "name": r["name"]} for r in self.runs]})
        return _Resp(next(r for r in self.runs if url.endswith(r["@id"])))


class _Box:
    base = "https://box.example.com"
    verify = False

    def __init__(self, runs):
        self.s = _Sess(runs)


def test_trigger_runs_matches_a_finished_run_by_time(la):
    gone = {"@id": "/wf/api/workflows/4/", "name": "Agentic Assistant Auto-Triage -- alerts",
            "status": "finished", "created": "2026-01-01T00:00:10Z", "env": {},
            "steps": [{"name": "auto_triage", "status": "finished", "result": {}}]}
    early = {**gone, "@id": "/wf/api/workflows/5/", "created": "2025-12-31T23:59:00Z"}
    since = la._epoch("2026-01-01T00:00:00Z")
    runs = la._trigger_runs(_Box([gone, early]), UUID, since)
    assert [(r["id"], r["by"]) for r in runs] == [("/wf/api/workflows/4/", "time")]


def test_trigger_runs_keeps_only_runs_for_this_alert(la):
    mine = {"@id": "/wf/api/workflows/1/", "name": "Agentic Assistant Auto-Triage -- alerts",
            "status": "finished", "env": {"input": {"records": [{"uuid": UUID}]}},
            "steps": [{"name": "auto_triage", "status": "finished", "result": {"skipped": "source"}}]}
    other = {**mine, "@id": "/wf/api/workflows/2/", "env": {"input": {"records": [{"uuid": "other"}]}}}
    unrelated = {**mine, "@id": "/wf/api/workflows/3/", "name": "Purge Integration Logs"}
    runs = la._trigger_runs(_Box([mine, other, unrelated]), UUID)
    assert [r["id"] for r in runs] == ["/wf/api/workflows/1/"]
    assert la._classify_no_turn(_facts(runs=runs))[0] == "filter_skip"
