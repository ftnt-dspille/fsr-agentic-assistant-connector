"""Scheduled hunts (X6), box-free.

The sqlite store is the real `Storage` on a temp file. The SOAR transport is a
recording fake that serves the scheduler endpoints, record reads and the case
writes; `chat_turn` is a stub that answers with a chosen transcript. Every
outcome, case write and stored row is asserted by value.
"""
from __future__ import annotations

import copy

import pytest

import ops_common
import ops_health
import ops_hunts

ALERT_IRI = "/api/3/alerts/case-1"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "test.db"))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield storage.default_storage()
    storage._DEFAULT_STORAGE = None


class _Box:
    """A recording FortiSOAR stand-in: scheduler rows, records, comments."""

    def __init__(self):
        self.calls = []
        self.schedules = []
        self.records = {}          # iri -> record
        self.created = 0
        self._seq = 0

    def writes(self, method=None, needle=""):
        return [c for c in self.calls
                if c[0] != "GET" and (method is None or c[0] == method) and needle in c[1]]

    def __call__(self, url, method, body=None, **kw):
        method = method.upper()
        self.calls.append((method, url, copy.deepcopy(body)))
        if "/api/wf/api/scheduled" in url:
            if method == "GET":
                return {"hydra:member": [dict(r) for r in self.schedules]}
            if method == "POST" and "trigger-now" in url:
                return {"message": "triggered"}
            if method == "POST":
                self._seq += 1
                row = {"id": f"tok-{self._seq}", **copy.deepcopy(body)}
                self.schedules.append(row)
                return row
            if method == "PUT":
                for i, r in enumerate(self.schedules):
                    if url.rstrip("/").endswith(r["id"]):
                        self.schedules[i] = copy.deepcopy(body)
                return body
            if method == "DELETE":
                self.schedules = [r for r in self.schedules
                                  if not url.rstrip("/").endswith(r["id"])]
                return None
        if method == "GET" and "/api/integration/connectors" in url:
            return {"data": [{"name": ops_hunts._connector_name(), "version": "1",
                              "configuration": [
                                  {"config_id": "c1", "name": "other", "default": False},
                                  {"config_id": "c2", "name": "main-config", "default": True}]}]}
        if method == "GET" and "picklist_names" in url:
            return {"hydra:member": [{"name": "Severity", "picklists": [
                {"itemValue": "High", "@id": "/api/3/picklists/sev-high"},
                {"itemValue": "Critical", "@id": "/api/3/picklists/sev-crit"}]}]}
        if method == "POST" and url in ("/api/3/alerts", "/api/3/incidents"):
            self.created += 1
            module = url.rsplit("/", 1)[-1]
            rec = {"@id": f"{url}/new-{self.created}", "uuid": f"new-{self.created}",
                   "status": {"itemValue": "Open"}, **copy.deepcopy(body)}
            self.records[rec["@id"]] = rec
            assert module
            return rec
        if method == "GET":
            return dict(self.records.get(url, {}))
        return {}


@pytest.fixture
def box(monkeypatch):
    b = _Box()
    monkeypatch.setattr(ops_common, "_make_request", lambda: b)
    monkeypatch.setattr(ops_health, "_make_request", lambda: b)
    return b


@pytest.fixture
def pushes(monkeypatch):
    seen = []

    def _push(config, params):
        seen.append(params)
        n = len(seen)
        return {"ok": True, "collection_uuid": f"coll-{n}", "workflow_uuid": f"wf-{n}",
                "workflows": [{"uuid": f"wf-{n}"}], "raw": {}}
    monkeypatch.setattr(ops_hunts, "push_playbook", _push)
    monkeypatch.setattr(ops_hunts, "_compile_hunt_runner",
                        lambda name, cfg: {"ok": True, "workflow_json": {"name": name,
                                                                         "config": cfg},
                                           "errors": []})
    return seen


QUESTION = "any host talking to known C2 in the last hour"


def save(**kw):
    p = {"name": "c2-sweep", "question": QUESTION, "cron": "0 * * * *"}
    p.update(kw)
    return ops_hunts.save_hunt({}, p)


def verdict(disposition, *, severity="high", confidence=0.9, summary="s",
            claims=("Host h1 contacted 198.51.100.7",)):
    return {"type": "verdict_card", "disposition": disposition, "severity": severity,
            "confidence": confidence, "summary": summary,
            "findings": [{"claim": c, "evidence": ["t1"]} for c in claims]}


@pytest.fixture
def turn(monkeypatch):
    """chat_turn stub: answers with `turn.transcript`, records its params."""
    class _Turn:
        transcript = [verdict("true_positive", summary="h1 beaconed to a known C2")]
        stop_reason = "done"
        error = None
        calls: list = []

    t = _Turn()
    t.calls = []

    def _chat(config, params):
        t.calls.append(params)
        return {"ok": t.error is None, "session_id": params["session_id"],
                "stop_reason": t.stop_reason, "transcript": list(t.transcript),
                "error": t.error}
    monkeypatch.setattr(ops_hunts, "chat_turn", _chat)
    return t


# --- schedule words and the cron guard --------------------------------------

@pytest.mark.parametrize("cron,tz,label", [
    ("0 * * * *", "UTC", "Every hour"),
    ("0 */4 * * *", "UTC", "Every 4 hours"),
    ("30 6 * * *", "UTC", "Every day at 06:30 UTC"),
    ("0 8 * * 1-5", "Europe/Paris", "Weekdays at 08:00 Europe/Paris"),
    ("*/30 * * * *", "UTC", "Every 30 minutes"),
    ("0 8 1 * *", "UTC", "Custom schedule"),
])
def test_schedule_label_by_value(cron, tz, label):
    assert ops_hunts.schedule_label(cron, tz) == label


@pytest.mark.parametrize("cron", ["* * * * *", "*/5 * * * *", "0 9 *"])
def test_a_too_frequent_or_malformed_schedule_is_refused_before_any_write(
        tmp_db, box, pushes, cron):
    with pytest.raises(Exception):
        save(cron=cron)
    assert box.calls == [] and pushes == [] and tmp_db.list_hunts() == []


# --- save, list, pause, edit, delete ----------------------------------------

def test_save_hunt_writes_definition_schedule_and_runner(tmp_db, box, pushes):
    out = save()
    assert out["ok"] and out["created"] is True and out["enabled"] is True
    assert out["schedule_label"] == "Every hour"
    h = tmp_db.get_hunt("c2-sweep")
    assert (h["question"], h["cron"], h["module"], h["enabled"]) == (
        QUESTION, "0 * * * *", "alerts", True)
    # The runner is pushed once, against the connector's default configuration.
    assert len(pushes) == 1 and pushes[0]["workflow_json"]["config"] == "main-config"
    assert h["agent_config"] == "main-config" and h["wf_iri"] == "/api/3/workflows/wf-1"
    # One scheduler row, named for the hunt, firing that playbook.
    (row,) = box.schedules
    assert row["name"] == "agent-hunt:c2-sweep" and row["enabled"] is True
    assert row["crontab"]["hour"] == "*" and row["crontab"]["minute"] == "0"
    assert row["kwargs"]["wf_iri"] == "/api/3/workflows/wf-1"


def test_the_real_runner_playbook_compiles_and_calls_run_hunt():
    compiled = ops_hunts._compile_hunt_runner("c2-sweep", "main-config")
    assert compiled["ok"], compiled["errors"]
    assert "run_hunt" in str(compiled["workflow_json"])


def test_pause_edit_and_resume_keep_one_schedule_and_do_not_republish(tmp_db, box, pushes):
    save()
    save(enabled=False)
    assert tmp_db.get_hunt("c2-sweep")["enabled"] is False
    (row,) = box.schedules
    assert row["enabled"] is False
    # An edit of the question and cadence: same scheduler row, no new runner.
    save(question="new question", cron="0 */2 * * *", enabled=True)
    (row,) = box.schedules
    assert row["enabled"] is True and row["crontab"]["hour"] == "*/2"
    h = tmp_db.get_hunt("c2-sweep")
    assert h["question"] == "new question" and h["cron"] == "0 */2 * * *"
    assert len(pushes) == 1
    # An omitted field keeps its value.
    ops_hunts.save_hunt({}, {"name": "c2-sweep", "enabled": False})
    assert tmp_db.get_hunt("c2-sweep")["question"] == "new question"


def test_list_hunts_reads_the_schedule_in_words_and_the_last_run(tmp_db, box, pushes, turn):
    save()
    save(name="quiet-one", question="q2", cron="30 6 * * *")
    assert [h["name"] for h in ops_hunts.list_hunts({}, {})["hunts"]] == [
        "c2-sweep", "quiet-one"]
    h0 = ops_hunts.list_hunts({}, {})["hunts"][0]
    assert (h0["schedule_label"], h0["last_run_at"], h0["last_outcome"], h0["runs"]) == (
        "Every hour", None, None, [])
    ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    h0 = ops_hunts.list_hunts({}, {})["hunts"][0]
    assert h0["last_outcome"] == "finding" and h0["last_outcome_label"] == "Found something"
    assert h0["case_iri"] == "/api/3/alerts/new-1"
    assert len(h0["runs"]) == 1 and h0["runs"][0]["verdict"] == "true positive"


def test_delete_removes_schedule_playbook_definition_and_history(tmp_db, box, pushes, turn):
    save()
    ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert tmp_db.list_hunt_runs("c2-sweep")
    out = ops_hunts.delete_hunt({}, {"name": "c2-sweep"})
    assert out == {"ok": True, "name": "c2-sweep", "deleted": True}
    assert box.schedules == []
    assert box.writes("DELETE", "workflow_collections/coll-1")
    assert tmp_db.get_hunt("c2-sweep") is None and tmp_db.list_hunt_runs("c2-sweep") == []
    assert ops_hunts.delete_hunt({}, {"name": "c2-sweep"})["deleted"] is False


def test_a_hunt_name_cannot_break_the_session_or_schedule_key(tmp_db, box, pushes):
    for bad in ("a:b", "a/b", " "):
        with pytest.raises(Exception):
            save(name=bad)


# --- runs -------------------------------------------------------------------

def test_a_finding_opens_a_case_with_the_cited_verdict_and_records_the_run(
        tmp_db, box, pushes, turn):
    save()
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert (out["outcome"], out["outcome_label"], out["case_action"]) == (
        "finding", "Found something", "opened")
    (post,) = box.writes("POST", "/api/3/alerts")
    body = post[2]
    assert body["name"] == "Hunt finding: c2-sweep"
    assert body["severity"] == "/api/3/picklists/sev-high"
    for text in ("h1 beaconed to a known C2", "Host h1 contacted 198.51.100.7", QUESTION):
        assert text in body["description"]
    (run,) = tmp_db.list_hunt_runs("c2-sweep")
    assert (run["outcome"], run["disposition"], run["severity"], run["confidence"],
            run["case_iri"], run["case_action"]) == (
        "finding", "true_positive", "high", 0.9, "/api/3/alerts/new-1", "opened")
    assert tmp_db.get_hunt("c2-sweep")["case_iri"] == "/api/3/alerts/new-1"
    # The turn ran as the system, in a hunt session, with the question in it.
    (p,) = turn.calls
    assert p["session_id"].startswith("hunt:c2-sweep:")
    assert p["actor"]["system"] is True and QUESTION in p["messages"][0]["content"]


def test_a_second_finding_comments_on_the_open_case_instead_of_opening_another(
        tmp_db, box, pushes, turn):
    save()
    ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    turn.transcript = [verdict("suspicious", summary="a second host did it too")]
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert out["case_action"] == "updated" and out["case_iri"] == "/api/3/alerts/new-1"
    assert len(box.writes("POST", "/api/3/alerts")) == 1
    (comment,) = box.writes("POST", "/api/3/comments")
    assert "a second host did it too" in comment[2]["content"]
    assert comment[2]["alerts"] == ["/api/3/alerts/new-1"]
    assert [r["case_action"] for r in tmp_db.list_hunt_runs("c2-sweep")] == [
        "updated", "opened"]


def test_a_finding_after_the_case_was_closed_opens_a_new_one(tmp_db, box, pushes, turn):
    save()
    ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    box.records["/api/3/alerts/new-1"]["status"] = {"itemValue": "Closed"}
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert out["case_action"] == "opened" and out["case_iri"] == "/api/3/alerts/new-2"
    assert tmp_db.get_hunt("c2-sweep")["case_iri"] == "/api/3/alerts/new-2"


def test_an_incident_hunt_opens_its_case_in_incidents(tmp_db, box, pushes, turn):
    save(module="incidents")
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert out["case_iri"] == "/api/3/incidents/new-1"
    assert box.writes("POST", "/api/3/incidents") and not box.writes("POST", "/api/3/alerts")


@pytest.mark.parametrize("disposition,outcome", [
    ("benign", "quiet"), ("false_positive", "quiet"),
    ("needs_more_info", "inconclusive")])
def test_a_quiet_or_inconclusive_run_is_recorded_and_opens_nothing(
        tmp_db, box, pushes, turn, disposition, outcome):
    save()
    turn.transcript = [verdict(disposition, summary="searched, nothing")]
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert out["outcome"] == outcome and out["case_iri"] is None
    assert box.writes("POST", "/api/3/alerts") == [] and box.writes("POST", "/api/3/comments") == []
    (run,) = tmp_db.list_hunt_runs("c2-sweep")
    assert (run["outcome"], run["case_action"], run["case_iri"]) == (outcome, "none", None)


def test_no_verdict_is_inconclusive_and_a_failed_turn_is_an_error(tmp_db, box, pushes, turn):
    save()
    turn.transcript = [{"type": "text", "text": "I looked."}]
    assert ops_hunts.run_hunt({}, {"name": "c2-sweep"})["outcome"] == "inconclusive"
    turn.error = "provider down"
    turn.transcript = [verdict("true_positive")]
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert (out["outcome"], out["outcome_label"]) == ("error", "Failed")
    assert box.writes("POST", "/api/3/alerts") == []
    assert tmp_db.list_hunt_runs("c2-sweep")[0]["error"] == "provider down"


def test_a_hunt_never_acts_on_containment_it_proposes(tmp_db, box, pushes, turn):
    save()
    turn.transcript = [verdict("true_positive"),
                       {"type": "action_card", "id": "c1", "tool": "run_op",
                        "title": "Block 198.51.100.7 on the firewall"}]
    turn.stop_reason = "awaiting_action_card"
    before = len(box.calls)
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert out["outcome"] == "finding" and out["waiting"] == 1
    # The only write is the case itself: no policy execution, no record update.
    writes = [c for c in box.calls[before:] if c[0] != "GET"]
    assert [(c[0], c[1]) for c in writes] == [("POST", "/api/3/alerts")]
    assert tmp_db.list_hunt_runs("c2-sweep")[0]["waiting"] == 1


def test_a_case_write_failure_keeps_the_finding_and_the_run(tmp_db, box, pushes, turn,
                                                            monkeypatch):
    save()
    orig = box.__call__

    def broken(url, method, body=None, **kw):
        if method.upper() == "POST" and url == "/api/3/alerts":
            raise RuntimeError("write refused")
        return orig(url, method, body, **kw)
    monkeypatch.setattr(ops_common, "_make_request", lambda: broken)
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert (out["outcome"], out["case_action"], out["case_iri"]) == ("finding", "failed", None)
    assert tmp_db.list_hunt_runs("c2-sweep")[0]["outcome"] == "finding"


@pytest.mark.parametrize("disposition,outcome", [
    ("true_positive", "finding"), ("suspicious", "finding"), ("benign", "quiet"),
    ("false_positive", "quiet"), ("needs_more_info", "inconclusive")])
def test_each_verdict_disposition_maps_to_exactly_one_outcome(disposition, outcome):
    got = ops_hunts.classify_run({"verdict": {"disposition": disposition}})
    assert got == outcome
    assert ops_hunts.classify_run({"verdict": {"disposition": disposition}}, "boom") == "error"
    assert ops_hunts.classify_run({"verdict": None}) == "inconclusive"


def test_run_now_asks_the_scheduler_and_an_unknown_hunt_is_not_found(tmp_db, box, pushes, turn):
    save()
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep", "trigger": True})
    assert out == {"ok": True, "name": "c2-sweep", "triggered": True}
    assert box.writes("POST", "trigger-now") and turn.calls == []
    assert ops_hunts.run_hunt({}, {"name": "nope"})["error"]["code"] == "not_found"


def test_the_ops_are_registered_with_info_json_and_the_dispatch_table():
    import json
    from pathlib import Path
    from connector_fsr_agentic_assistant import operations as ops
    info = json.loads((Path(ops.__file__).with_name("info.json")).read_text())
    names = {o["operation"] for o in info["operations"]}
    for op in ("list_hunts", "save_hunt", "delete_hunt", "run_hunt", "shift_summary"):
        assert op in names and op in ops._LIVE_OPERATIONS
