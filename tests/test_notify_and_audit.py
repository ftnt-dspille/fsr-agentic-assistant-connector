"""Outbound notifications and audit export (X6), box-free.

The store is the real `Storage` on a temp file. The notification channel is a
recording stand-in for `ops_scheduling._send_notification` (the one place that
talks to the box's email/Slack/Teams connector); the turn is a stub that
answers with a chosen transcript. Every send, stored row and exported cell is
asserted by value.
"""
from __future__ import annotations

import csv
import io
import json
import time

import pytest

import ops_audit
import ops_hunts
import ops_notify
import ops_scheduling
import ops_shift
from tests.test_scheduled_hunts import (  # noqa: F401
    box, pushes, save, tmp_db, turn, verdict,
)
from tests.test_auto_triage import ALERT, box as triage_box  # noqa: F401

ON = {"notify_unattended": True, "notification_recipient": "soc@example.com"}


class Channel:
    def __init__(self):
        self.sent = []
        self.result = {"ok": True, "channel": "smtp", "channel_label": "Email (SMTP)",
                       "subject": "s"}

    def __call__(self, subject, body, name, session_id, target):
        self.sent.append({"subject": subject, "body": body, "target": target,
                          "name": name, "session_id": session_id})
        return self.result


@pytest.fixture
def channel(monkeypatch):
    ch = Channel()
    monkeypatch.setattr(ops_scheduling, "_send_notification", ch)
    import ops_autonomy
    monkeypatch.setattr(ops_autonomy, "_send_notification", ch)
    return ch


def tmp_rows(_box):
    from connector_fsr_agentic_assistant.storage import default_storage
    return default_storage().list_notifications()


def run_hunt_with(turn, transcript, config=ON, stop="done"):
    turn.transcript, turn.stop_reason = transcript, stop
    return ops_hunts.run_hunt(config, {"name": "c2-sweep"})


# --- notifications: when -----------------------------------------------------

def test_off_by_default_nothing_is_sent_or_recorded(tmp_db, box, pushes, turn, channel):
    save()
    out = run_hunt_with(turn, [verdict("true_positive")], config={})
    assert out["outcome"] == "finding" and out["notified"] is None
    assert channel.sent == [] and tmp_db.list_notifications() == []


@pytest.mark.parametrize("disposition", ["true_positive", "suspicious"])
def test_a_threat_verdict_from_a_hunt_sends_one_notification(
        tmp_db, box, pushes, turn, channel, disposition):
    save()
    out = run_hunt_with(turn, [verdict(disposition, summary="h1 beaconed")])
    assert out["notified"] == "sent"
    (m,) = channel.sent
    assert m["target"] == "soc@example.com" and "c2-sweep" in m["subject"]
    assert "found a " + disposition.replace("_", " ") + " verdict" in m["subject"]
    assert "h1 beaconed" in m["body"] and "Severity: high" in m["body"]
    assert "Confidence: 90%" in m["body"]
    (row,) = tmp_db.list_notifications()
    assert (row["kind"], row["status"], row["channel"], row["trigger_code"]) == (
        "hunt", "sent", "smtp", "verdict")


def test_a_benign_verdict_sends_nothing(tmp_db, box, pushes, turn, channel):
    save()
    out = run_hunt_with(turn, [verdict("benign")])
    assert out["notified"] is None and channel.sent == []


def test_the_trigger_is_the_structured_disposition_never_the_words(
        tmp_db, box, pushes, turn, channel):
    # A benign verdict whose prose is full of alarming words does not notify.
    save()
    run_hunt_with(turn, [verdict("benign", summary=(
        "malicious suspicious critical true positive escalate immediately"))])
    assert channel.sent == []
    # And a threat verdict with bland prose does.
    run_hunt_with(turn, [verdict("true_positive", summary="all quiet")])
    assert len(channel.sent) == 1


def test_an_escalation_notifies_even_without_a_threat_verdict(
        tmp_db, box, pushes, turn, channel):
    save()
    out = run_hunt_with(turn, [verdict("needs_more_info", confidence=0.3)])
    assert out["notified"] == "sent"
    assert channel.sent[0]["subject"].endswith("needs a person")
    assert tmp_db.list_notifications()[0]["trigger_code"] == "escalation"


def test_the_same_run_is_notified_once(tmp_db, box, pushes, turn, channel):
    outcome = {"needs_human": False, "why": [],
               "verdict": {"disposition": "true_positive"}}
    first = ops_notify.notify_turn(ON, kind="hunt", name="h", session_id="s1",
                                   outcome=outcome)
    again = ops_notify.notify_turn(ON, kind="hunt", name="h", session_id="s1",
                                   outcome=outcome)
    assert first["status"] == "sent" and again["status"] == "duplicate"
    assert len(channel.sent) == 1 and len(tmp_db.list_notifications()) == 1


@pytest.mark.parametrize("result,status,detail", [
    ({"ok": True, "skipped": "no_channels", "message": "x"}, "skipped", "no_channels"),
    ({"ok": True, "skipped": "no_target", "channel": "smtp"}, "skipped", "no_target"),
    ({"ok": False, "error": {"code": "send_failed", "message": "x"}}, "failed", "send_failed"),
])
def test_a_skip_or_a_failure_is_recorded_and_never_fails_the_run(
        tmp_db, box, pushes, turn, channel, result, status, detail):
    channel.result = result
    save()
    out = run_hunt_with(turn, [verdict("true_positive")])
    assert out["ok"] and out["outcome"] == "finding" and out["notified"] == status
    (row,) = tmp_db.list_notifications()
    assert (row["status"], row["detail"]) == (status, detail)


# --- notifications: auto-triage and shift ------------------------------------

def test_auto_triage_notifies_through_the_configured_channel(triage_box, channel):
    from connector_fsr_agentic_assistant import operations as ops
    out = ops.auto_triage(ON, {"record": ALERT["@id"]})
    assert out["notified"] == "smtp" and len(channel.sent) == 1
    assert channel.sent[0]["target"] == "soc@example.com"
    assert "true positive verdict" in channel.sent[0]["subject"]
    assert len(tmp_rows(triage_box)) == 1


def test_auto_triage_off_by_default_and_a_playbook_target_is_not_told_twice(
        triage_box, channel):
    from connector_fsr_agentic_assistant import operations as ops
    # Off: the older per-playbook path is all that runs, and records nothing.
    ops.auto_triage({}, {"record": ALERT["@id"]})
    assert tmp_rows(triage_box) == []
    triage_box.record = dict(ALERT, uuid="u2", **{"@id": "/api/3/alerts/u2"})
    ops.auto_triage(ON, {"record": "/api/3/alerts/u2", "notify_target": "a@example.com"})
    assert [m["target"] for m in channel.sent[-1:]] == ["a@example.com"]
    # One legacy send per call (2 in all) and none from the configured path.
    assert len(channel.sent) == 2 and tmp_rows(triage_box) == []


def test_shift_summary_is_sent_only_when_asked_and_enabled(tmp_db, channel):
    off = ops_shift.shift_summary({}, {"notify": True})
    assert off["notified"] == "off" and channel.sent == []
    plain = ops_shift.shift_summary(ON, {})
    assert "notified" not in plain and channel.sent == []
    sent = ops_shift.shift_summary(ON, {"notify": True, "hours": 4})
    assert sent["notified"] == "sent"
    (m,) = channel.sent
    assert m["subject"] == "[Agentic Assistant] " + sent["subject"]
    assert m["body"] == sent["text"] and m["target"] == "soc@example.com"
    assert tmp_db.list_notifications()[0]["kind"] == "shift"


# --- audit export ------------------------------------------------------------

def _seed_audit(store):
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))  # noqa: E731
    sid = "auto:alerts:a1"
    store.upsert_chat_session(sid, "a1", "alerts", "triage")
    uid = store.record_agent_usage({
        "ts": iso(now - 600), "session_id": sid, "user_name": "=evil()",
        "intent": "triage", "model": "m1", "turn": 1, "input_tokens": 10,
        "output_tokens": 5, "cost_usd": 0.01, "stop_reason": "done",
        "prompt": "Triage host 203.0.113.9 now", "response": "=cmd|' /C calc'!A0 done"})
    store.record_agent_tool_calls(uid, 1, [{
        "tool_name": "search_logs", "params": {"src": "203.0.113.9"},
        "result_status": "success", "result_summary": "3 hits for 203.0.113.9",
        "latency_ms": 12}])
    store.record_agent_usage({"ts": iso(now - 40 * 3600), "session_id": "old",
                              "intent": "triage", "prompt": "old row"})
    store.save_card(sid, {"type": "action_card", "id": "c1", "tool": "run_op"})
    assert store.claim_card_decision(sid, "c1", "approved", "analyst1") is None
    store.record_card_execution(sid, "c1", connector="fortigate", operation="block_ip",
                                config=None, args={"ip": "203.0.113.9"}, ok=True)
    nid = store.claim_notification("auto_triage", sid, "verdict", "subj")
    store.finish_notification(nid, "sent", "smtp", "Email (SMTP)")
    return sid


def test_the_export_carries_every_part_of_the_audit_for_the_window(tmp_db):
    sid = _seed_audit(tmp_db)
    out = ops_audit.export_audit({}, {"hours": 2})
    assert out["ok"] and out["redaction"] == "off"
    assert out["counts"] == {"sessions": 1, "turns": 1, "tool_calls": 1,
                             "decisions": 1, "notifications": 1}
    data = json.loads(out["json"])
    (turn_,) = data["turns"]
    assert turn_["session_id"] == sid and turn_["prompt"] == "Triage host 203.0.113.9 now"
    (call,) = data["tool_calls"]
    assert call["tool"] == "search_logs" and call["params"] == {"src": "203.0.113.9"}
    (dec,) = data["decisions"]
    assert (dec["decision"], dec["actor"], dec["connector"], dec["operation"],
            dec["args"], dec["ok"], dec["executed"]) == (
        "approved", "analyst1", "fortigate", "block_ip", {"ip": "203.0.113.9"}, True, True)
    assert data["notifications"][0]["status"] == "sent"
    rows = list(csv.DictReader(io.StringIO(out["csv"])))
    assert [r["kind"] for r in rows].count("tool_call") == 1
    assert {r["kind"] for r in rows} == {"turn", "tool_call", "decision", "notification"}
    assert list(rows[0]) == list(ops_audit.CSV_COLUMNS)


def test_the_window_excludes_older_rows(tmp_db):
    _seed_audit(tmp_db)
    wide = json.loads(ops_audit.export_audit({}, {"hours": 72, "format": "json"})["json"])
    assert len(wide["turns"]) == 2
    narrow = json.loads(ops_audit.export_audit({}, {"hours": 2, "format": "json"})["json"])
    assert [t["session_id"] for t in narrow["turns"]] == ["auto:alerts:a1"]


def test_a_formula_in_a_cell_is_exported_as_text(tmp_db):
    _seed_audit(tmp_db)
    out = ops_audit.export_audit({}, {"hours": 2, "format": "csv"})
    assert "json" not in out
    turn_row = next(r for r in csv.DictReader(io.StringIO(out["csv"])) if r["kind"] == "turn")
    assert turn_row["actor"] == "'=evil()"
    assert ops_audit._cell("=1+1") == "'=1+1" and ops_audit._cell("plain") == "plain"


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_redaction_masks_the_exported_content(tmp_db, mode):
    _seed_audit(tmp_db)
    out = ops_audit.export_audit({"redaction_mode": mode}, {"hours": 2})
    assert out["redaction"] == mode
    assert "203.0.113.9" not in out["json"] and "203.0.113.9" not in out["csv"]
    data = json.loads(out["json"])
    assert "203.0.113.9" not in json.dumps(data)
    assert data["turns"][0]["prompt"].startswith("Triage host ")
    assert data["turns"][0]["prompt"] != "Triage host 203.0.113.9 now"
    # The same value carries one placeholder across the parts of one session.
    ph = data["tool_calls"][0]["params"]["src"]
    assert ph in data["turns"][0]["prompt"] and ph in data["decisions"][0]["args"]["ip"]


def test_a_bad_format_or_window_is_refused(tmp_db):
    assert ops_audit.export_audit({}, {"format": "xml"})["error"]["code"] == "bad_format"
    assert ops_audit.export_audit({}, {"hours": 10 ** 6})["error"]["code"] == "bad_window"


def test_the_new_ops_and_fields_are_registered():
    from pathlib import Path
    from connector_fsr_agentic_assistant import operations as ops
    info = json.loads((Path(ops.__file__).with_name("info.json")).read_text())
    assert "export_audit" in {o["operation"] for o in info["operations"]}
    assert "export_audit" in ops._LIVE_OPERATIONS
    fields = {f["name"]: f for f in info["configuration"]["fields"]}
    assert fields["notify_unattended"]["value"] is False
    assert fields["notification_recipient"]["visible"] is True
    shift = next(o for o in info["operations"] if o["operation"] == "shift_summary")
    assert "notify" in {p["name"] for p in shift["parameters"]}


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_redaction_masks_a_known_host_embedded_in_names_and_labels(tmp_db, mode):
    """A host known from a typed field is masked inside longer free-form strings
    too (a record name, a prompt), in every row, whatever the row order."""
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))  # noqa: E731
    sid = "auto:alerts:a2"
    name = "Agent Lab - C2 callback from dev-laptop-13"
    tmp_db.upsert_chat_session(sid, "a2", "alerts", "triage")
    uid = tmp_db.record_agent_usage({
        "ts": iso(now - 600), "session_id": sid, "intent": "triage", "model": "m1",
        "turn": 1, "prompt": "Look at " + name, "response": "done"})
    tmp_db.record_agent_tool_calls(uid, 1, [
        {"tool_name": "get_alert", "params": {"id": "a2"},
         "result_status": "success", "latency_ms": 1,
         "result_summary": json.dumps({"name": name, "sourceHost": "dev-laptop-13"})},
        {"tool_name": "find_alerts", "params": {"filter": name},
         "result_status": "success", "latency_ms": 1,
         "result_summary": json.dumps({"items": [{"name": name}], "hostname": "dev-laptop-13"})}])
    out = ops_audit.export_audit({"redaction_mode": mode}, {"hours": 2})
    assert "dev-laptop-13" not in out["json"] and "dev-laptop-13" not in out["csv"]
    data = json.loads(out["json"])
    assert data["turns"][0]["prompt"].startswith("Look at Agent Lab - C2 callback from <HOST_")


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_a_host_known_in_one_session_is_masked_in_the_others_of_the_export(tmp_db, mode):
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))  # noqa: E731
    for sid, extra in (("auto:alerts:s1", {"sourceHost": "dev-laptop-13"}),
                       ("auto:alerts:s2", {})):
        tmp_db.upsert_chat_session(sid, sid[-2:], "alerts", "triage")
        uid = tmp_db.record_agent_usage({
            "ts": iso(now - 600), "session_id": sid, "intent": "triage", "model": "m1",
            "turn": 1, "prompt": "p", "response": "The alert is named Agent Lab - C2 "
                                                    "callback from dev-laptop-13."})
        tmp_db.record_agent_tool_calls(uid, 1, [{
            "tool_name": "get_alert", "params": {}, "result_status": "success",
            "latency_ms": 1, "result_summary": json.dumps(
                {"name": "C2 callback from dev-laptop-13", **extra})}])
    out = ops_audit.export_audit({"redaction_mode": mode}, {"hours": 2})
    assert "dev-laptop-13" not in out["json"] and "dev-laptop-13" not in out["csv"]
