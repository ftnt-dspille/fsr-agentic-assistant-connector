"""Auto-triage (autonomous tier-1, phase A), box-free.

A new record fires `auto_triage`, which runs one unattended triage turn on it.
What must hold without a box: only records inside the filter are triaged, each
record once, the turn is the same triage turn an analyst would get (the record
mounted, triage intent) started by the system rather than a person, nothing is
approved on the way, and the result says plainly whether a human is needed.
"""
from __future__ import annotations

import pytest
import ops_approvals
import ops_autonomy
import ops_cards
import ops_chat
import ops_common
import ops_health
import ops_playbooks
import ops_playbooks_persist
import ops_policy
import ops_scheduling
import ops_warmup

ALERT = {
    "@id": "/api/3/alerts/a1b2c3d4-0000-4000-8000-000000000001",
    "@type": "Alert",
    "uuid": "a1b2c3d4-0000-4000-8000-000000000001",
    "id": 41,
    "name": "Outbound beacon to 198.51.100.7",
    "severity": {"@id": "/api/3/picklists/sev-high", "itemValue": "High"},
    "source": "FortiSIEM",
    "sourceIp": "198.51.100.7",
    "description": "",
    "assignedTo": None,
}

VERDICT = {"type": "verdict_card", "disposition": "true_positive",
           "severity": "high", "confidence": 0.92,
           "summary": "Known C2 address", "findings": []}
ACTION = {"type": "action_card", "id": "card-1", "tool": "run_op",
          "title": "Block 198.51.100.7 on FortiGate"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "test.db"))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield
    storage._DEFAULT_STORAGE = None


@pytest.fixture
def box(monkeypatch, tmp_db):
    """A transport that serves ALERT, and a chat_turn that records its params
    and answers with `box.transcript`."""
    from connector_fsr_agentic_assistant import operations as ops

    class _Box:
        record = dict(ALERT)
        transcript = [{"type": "text", "text": "Looked it up."}, VERDICT, ACTION]
        stop_reason = "awaiting_approval"
        turns: list = []

        def __call__(self, url, method, body=None, **kw):
            if method != "GET":
                # Only an enforced policy writes (a comment, an undo).
                self.writes.append((method, url, body))
                if method == "PUT":
                    self.record = {**self.record, **(body or {})}
                return {}
            return dict(self.record)

    b = _Box()
    b.turns = []
    b.writes = []
    monkeypatch.setattr(ops_common, "_make_request", lambda: b)
    monkeypatch.setattr(ops_health, "_make_request", lambda: b)

    def _chat_turn(config, params):
        b.turns.append(params)
        err = getattr(b, "error", None)
        return {"ok": err is None, "session_id": params["session_id"],
                "stop_reason": b.stop_reason, "transcript": list(b.transcript),
                "error": err}

    monkeypatch.setattr(ops_autonomy, "chat_turn", _chat_turn)
    monkeypatch.setattr(ops, "chat_turn", _chat_turn)
    # These tests triage look-alike alerts one by one; grouping has its own
    # tests (test_auto_triage_storm.py).
    monkeypatch.setitem(ops_autonomy._STORM_DEFAULTS, "group", False)
    b.sent = []

    def _send(subject, body, name, session_id, target):
        b.sent.append({"subject": subject, "body": body, "target": target})
        return b.send_result

    b.send_result = {"ok": True, "channel": "smtp", "subject": "s"}
    monkeypatch.setattr(ops_autonomy, "_send_notification", _send)
    monkeypatch.setattr(ops_scheduling, "_send_notification", _send)
    return b


def _run(**params):
    from connector_fsr_agentic_assistant import operations as ops
    return ops.auto_triage({}, {"record": ALERT["@id"], **params})


def test_a_new_high_alert_gets_the_analysts_triage_turn_started_by_the_system(box):
    out = _run()
    assert len(box.turns) == 1
    turn = box.turns[0]
    assert turn["intent"] == "triage"
    assert turn["actor"]["iri"] == "system:auto-triage"
    assert turn["session_id"] == f"auto:alerts:{ALERT['uuid']}" == out["session_id"]
    entity = turn["entity"]
    assert entity["module"] == "alerts" and entity["iri"] == ALERT["@id"]
    # Picklists reach the model as their text; empties are dropped, as the
    # drawer drops them.
    assert entity["fields"]["severity"] == "High"
    assert entity["fields"]["sourceIp"] == "198.51.100.7"
    assert "description" not in entity["fields"] and "assignedTo" not in entity["fields"]
    assert not turn.get("read_only"), "proposed actions must still card"


def test_the_result_names_the_verdict_and_what_waits_on_a_human(box):
    out = _run()
    assert out["ok"] is True
    assert out["verdict"] == {"disposition": "true_positive", "severity": "high",
                              "confidence": 0.92, "summary": "Known C2 address"}
    assert out["waiting"] == 1
    assert out["needs_human"] is True and out["why"] == ["awaiting_approval"]


def test_a_turn_without_a_verdict_is_flagged(box):
    box.transcript = [{"type": "text", "text": "I could not reach FortiSIEM."}]
    box.stop_reason = "end_turn"
    out = _run()
    assert out["verdict"] is None
    assert out["needs_human"] is True and out["why"] == ["no_verdict"]


def test_a_clear_verdict_with_nothing_waiting_needs_no_one(box):
    box.transcript = [{**VERDICT, "disposition": "false_positive"}]
    box.stop_reason = "end_turn"
    out = _run()
    assert out["needs_human"] is False and out["why"] == []


def test_an_unsure_verdict_asks_for_review(box):
    box.transcript = [{**VERDICT, "disposition": "suspicious", "confidence": 0.5}]
    box.stop_reason = "end_turn"
    assert _run()["why"] == ["needs_review"]


def test_a_turn_parked_on_a_question_is_flagged(box):
    """Live: the turn ended on a capability-gap card asking what to do next
    (stop `awaiting_choice`). Unattended, nobody answers it."""
    box.transcript = [{**VERDICT, "disposition": "true_positive"},
                      {"type": "capability_gap", "missing": "ip containment"}]
    box.stop_reason = "awaiting_choice"
    out = _run()
    assert out["needs_human"] is True and out["why"] == ["awaiting_answer"]


def test_a_record_is_triaged_once(box):
    _run()
    again = _run()
    assert again == {"ok": True, "skipped": "already_triaged", "record": ALERT["@id"],
                     "session_id": f"auto:alerts:{ALERT['uuid']}"}
    assert len(box.turns) == 1


@pytest.mark.parametrize("record, params, why", [
    ({"severity": {"itemValue": "Low"}}, {}, "severity"),
    ({}, {"severities": ["Critical"]}, "severity"),
    ({}, {"sources": ["FortiAnalyzer"]}, "source"),
])
def test_records_outside_the_filter_are_skipped_without_a_turn(box, record, params, why):
    box.record = {**ALERT, **record}
    out = _run(**params)
    assert out["skipped"] == why and box.turns == []
    # Skipping claims nothing: the record is still triaged if the filter widens.
    box.record = dict(ALERT)
    assert "skipped" not in _run(severities=[])


def test_the_filter_matches_text_not_case(box):
    box.record = {**ALERT, "severity": {"itemValue": "critical"}, "source": "fortisiem"}
    assert "skipped" not in _run(severities=["Critical"], sources=["FortiSIEM"])


def test_an_empty_severity_list_means_any(box):
    box.record = {**ALERT, "severity": {"itemValue": "Low"}}
    assert "skipped" not in _run(severities=[])


def test_a_bare_id_is_refused_before_anything_is_read(box):
    from connector_fsr_agentic_assistant import operations as ops
    out = ops.auto_triage({}, {"record": ALERT["uuid"]})
    assert out["ok"] is False and out["error"]["code"] == "bad_iri"
    assert box.turns == []


# --- the trigger playbook --------------------------------------------------

@pytest.fixture
def emitted(monkeypatch):
    """The YAML the trigger compiles, without the compiler."""
    seen = {}

    class _Result:
        ok, fsr_json, errors = True, {"stub": True}, []

    class _Compiler:
        @staticmethod
        def compile_yaml(text, _db):
            seen["yaml"] = text
            return _Result()

    monkeypatch.setattr(ops_autonomy, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_chat, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_common, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_health, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_playbooks, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_playbooks_persist, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_scheduling, "_import_fsr_playbooks", lambda: _Compiler())
    monkeypatch.setattr(ops_approvals, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_autonomy, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_cards, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_chat, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_health, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_playbooks, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_playbooks_persist, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_scheduling, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_warmup, "_reference_db_path", lambda: ":memory:")
    return seen


def test_the_trigger_fires_on_create_and_hands_auto_triage_the_new_record(emitted):
    import yaml
    from connector_fsr_agentic_assistant import operations as ops
    assert ops._compile_auto_triage_playbook("alerts", "cfg-1", ["High"], [], True)["ok"]
    pb = yaml.safe_load(emitted["yaml"])["playbooks"][0]
    trigger, step = pb["steps"]
    assert pb["is_active"] is True
    assert trigger["type"] == "start_on_create" and trigger["module"] == "alerts"
    assert trigger["next"] == step["name"]
    assert step["connector"] == ops._connector_name()
    assert step["operation"] == "auto_triage" and step["config"] == "cfg-1"
    assert step["params"] == {"record": "{{ vars.input.records[0]['@id'] }}",
                              "severities": ["High"], "sources": [],
                              "notify_target": ""}


def test_disabling_keeps_the_playbook_but_deactivates_it(emitted):
    import yaml
    from connector_fsr_agentic_assistant import operations as ops
    ops._compile_auto_triage_playbook("alerts", "cfg-1", [], [], False)
    assert yaml.safe_load(emitted["yaml"])["playbooks"][0]["is_active"] is False


def test_the_trigger_playbook_compiles():
    """The real compiler accepts it (step types, trigger shape, Jinja)."""
    pytest.importorskip("fsr_playbooks")
    from connector_fsr_agentic_assistant import operations as ops
    out = ops._compile_auto_triage_playbook("alerts", "cfg-1", ["High", "Critical"],
                                            ["FortiSIEM"], True)
    assert out["ok"], out["errors"]


@pytest.fixture
def seed(emitted, monkeypatch):
    """`set_auto_triage` with the compiler and transport stubbed, capturing the
    policy it seeds. `state['existing']` is what the Key Store already holds."""
    state = {"existing": None, "written": None}
    monkeypatch.setattr(ops_autonomy, "_require_transport",
                        lambda *a, **k: (lambda *x, **y: {}, None))
    monkeypatch.setattr(ops_autonomy, "push_playbook",
                        lambda c, p: {"ok": True, "iri": "/api/3/workflows/wf-1"})
    monkeypatch.setattr(ops_autonomy, "_wf_iri_from_push", lambda p: p.get("iri"))
    monkeypatch.setattr(ops_autonomy, "_load_autonomy_policy",
                        lambda force=False: (state["existing"], None))

    def _write(policy):
        state["written"] = policy
        return None

    monkeypatch.setattr(ops_policy, "write_autonomy_policy", _write)
    return state


def _set(**params):
    from connector_fsr_agentic_assistant import operations as ops
    return ops.set_auto_triage({}, {"agent_config": "cfg-1", "module": "alerts",
                                    **params})


def test_turning_on_auto_triage_seeds_the_default_close_rule_in_shadow(seed):
    out = _set(enabled=True)
    assert out["ok"] and out["policy_seeded"] is True
    pol = seed["written"]
    # Seeded in shadow (nothing acts until a deliberate promotion), with the
    # one close rule, validated against the framework by set_auto_triage.
    assert pol["mode"] == "shadow"
    assert [r["id"] for r in pol["rules"]] == ["close-false-positive"]
    assert pol["rules"][0]["action"]["module"] == "alerts"


def test_an_existing_policy_is_never_overwritten_by_enabling_auto_triage(seed):
    seed["existing"] = {"version": 1, "mode": "enforce", "rules": []}
    out = _set(enabled=True)
    assert out["ok"] and out["policy_seeded"] is False
    assert seed["written"] is None


def test_disabling_auto_triage_seeds_no_policy(seed):
    out = _set(enabled=False)
    assert out["ok"] and out["policy_seeded"] is False
    assert seed["written"] is None


# --- phase B: escalation ----------------------------------------------------

def _queue():
    from connector_fsr_agentic_assistant import operations as ops
    return ops.list_pending({}, {})


def _resolve(**params):
    from connector_fsr_agentic_assistant import operations as ops
    return ops.resolve_escalation({}, params)


def test_a_record_that_needs_a_human_is_queued_and_the_reviewer_told(box):
    out = _run(notify_target="soc-leads@example.com")
    assert out["escalation_id"] and out["priority"] == "P2"
    assert out["notified"] == "smtp"
    (sent,) = box.sent
    assert sent["target"] == "soc-leads@example.com"
    assert "P2" in sent["subject"] and ALERT["name"] in sent["subject"]
    assert "1 action waiting for approval." in sent["body"]
    (esc,) = _queue()["escalations"]
    assert esc["card_id"] == out["escalation_id"]
    assert esc["record"]["name"] == ALERT["name"] and esc["record"]["iri"] == ALERT["@id"]
    assert esc["verdict"]["disposition"] == "true_positive"
    assert esc["reasons"] == ["1 action waiting for approval."]


def test_a_record_finished_alone_is_neither_queued_nor_announced(box):
    box.transcript = [{**VERDICT, "disposition": "false_positive"}]
    box.stop_reason = "end_turn"
    out = _run()
    assert "escalation_id" not in out and box.sent == []
    assert _queue()["escalations"] == []


def test_reasons_read_as_plain_sentences(box):
    box.transcript = [{**VERDICT, "disposition": "needs_more_info", "confidence": 0.5}]
    box.stop_reason = "awaiting_choice"
    (esc,) = (_run(), _queue()["escalations"])[1]
    assert esc["reasons"] == [
        "The verdict needs review: needs more information, 50% confident.",
        "The assistant stopped on a question nobody has answered."]


@pytest.mark.parametrize("record_sev, verdict_sev, priority", [
    ("High", "critical", "P1"),
    ("Critical", "low", "P1"),
    ("Medium", "medium", "P3"),
    ("Low", "low", "P4"),
])
def test_priority_is_the_higher_of_record_and_verdict_severity(box, record_sev, verdict_sev, priority):
    box.record = {**ALERT, "severity": {"itemValue": record_sev}}
    box.transcript = [{**VERDICT, "severity": verdict_sev}, ACTION]
    assert _run(severities=[])["priority"] == priority


def test_the_queue_puts_the_most_urgent_first_then_the_oldest(box, monkeypatch):
    import time as _t
    clock = [_t.time() - 600]
    monkeypatch.setattr(_t, "time", lambda: clock[0])
    for uuid, sev in (("u-old-p2", "High"), ("u-p1", "Critical"), ("u-new-p2", "High")):
        box.record = {**ALERT, "@id": f"/api/3/alerts/{uuid}", "uuid": uuid,
                      "severity": {"itemValue": sev}}
        _run(severities=[])
        clock[0] += 60
    order = [e["card_id"] for e in _queue()["escalations"]]
    assert order == ["esc_u-p1", "esc_u-old-p2", "esc_u-new-p2"]


def test_resolving_takes_it_off_the_queue_once(box):
    out = _run()
    sid, cid = out["session_id"], out["escalation_id"]
    ok = _resolve(session_id=sid, card_id=cid, outcome="dismissed",
                  actor={"name": "Lead A"})
    assert ok == {"ok": True, "session_id": sid, "card_id": cid, "outcome": "dismissed"}
    assert _queue()["escalations"] == []
    again = _resolve(session_id=sid, card_id=cid, actor={"name": "Lead B"})
    assert again["ok"] is False and again["error"]["code"] == "already_decided"
    assert "dismissed by Lead A" in again["error"]["message"]


def test_resolve_only_answers_escalations(box):
    out = _run()
    bad = _resolve(session_id=out["session_id"], card_id=out["escalation_id"],
                   outcome="approve")
    assert bad["error"]["code"] == "bad_outcome"
    missing = _resolve(session_id=out["session_id"], card_id="card-1")
    assert missing["error"]["code"] == "not_found"


def test_a_failed_notification_does_not_fail_the_triage(box):
    box.send_result = {"ok": False, "error": {"code": "send_failed"}}
    out = _run()
    assert out["ok"] is True and out["notified"] == "send_failed"
    assert _queue()["escalations"], "the escalation is queued whether or not anyone was told"


def test_a_skipped_notification_is_not_reported_as_sent(box):
    """Live: with no recipient the SMTP channel skips, and its skip still names
    the channel. The result said `notified: smtp` for an email never sent."""
    box.send_result = {"ok": True, "skipped": "no_target", "channel": "smtp",
                       "message": "no notify_target set for channel smtp"}
    assert _run()["notified"] == "no_target"


def test_a_confirmed_threat_nothing_acts_on_is_queued(box):
    """Live on .159: a C2 true positive, nothing staged, reached no queue."""
    box.transcript = [VERDICT]
    box.stop_reason = "end_turn"
    out = _run()
    assert out["needs_human"] is True and out["why"] == ["confirmed_threat"]
    assert out["escalation_id"]
    assert box.sent and "confirmed a threat and nothing has acted on it" in box.sent[0]["body"]


def test_a_staged_action_is_the_reason_not_the_confirmation(box):
    out = _run()   # verdict + action card, parked on approval
    assert out["why"] == ["awaiting_approval"]


def test_a_turn_that_errored_is_queued_with_its_error(box):
    box.transcript = [{**VERDICT, "disposition": "false_positive"}]
    box.stop_reason = "end_turn"
    box.error = "provider stream timed out after 600 s"
    out = _run()
    assert out["ok"] is False and out["needs_human"] is True
    assert "turn_error" in out["why"]
    assert out["error"] == {"code": "turn_error",
                            "message": "provider stream timed out after 600 s"}
    assert "stopped on an error: provider stream timed out" in box.sent[0]["body"]


# ---- phase E: the policy acts ----------------------------------------------

POL = {"rule": "block-external-ip", "outcome": "would_act", "failed": [],
       "mode": "enforce_unavailable", "targets": ["198.51.100.7"]}
CARD = {"type": "action_card", "id": "card-blk", "connector": "fortigate-firewall",
        "operation": "block_ip_new", "summary": "Block 198.51.100.7",
        "args": {"method": "Quarantine Based", "ip_addresses": "198.51.100.7",
                 "time_to_live": "1 Hour"}, "policy": POL}
LIVE = {"enabled": True, "mode": "enforce", "rules": [{"id": "block-external-ip"}]}


@pytest.fixture
def enforce(box, monkeypatch):
    """A live policy, and a FortiGate that records blocks and reads them back."""
    import fsr_playbooks.llm.tools as tools
    state = {"policy": dict(LIVE), "blocked": set(), "calls": [], "fail_op": False}
    monkeypatch.setattr(ops_approvals, "_load_autonomy_policy", lambda: (state["policy"], None))
    monkeypatch.setattr(ops_autonomy, "_load_autonomy_policy", lambda: (state["policy"], None))
    monkeypatch.setattr(ops_policy, "_load_autonomy_policy", lambda: (state["policy"], None))

    def _dispatch(name, args, **kw):
        state["calls"].append((args.get("op"), args))
        if args.get("op") == "block_ip_new":
            if state["fail_op"]:
                return {"ok": False, "message": "FortiGate unreachable"}
            state["blocked"].add(args["params"]["ip_addresses"])
            return {"ok": True}
        if args.get("op") == "get_blocked_ip":
            return {"ok": True, "data": sorted(state["blocked"])}
        return {"ok": True}

    monkeypatch.setattr(tools, "dispatch", _dispatch)
    box.transcript = [VERDICT, dict(CARD, policy=dict(POL))]
    box.stop_reason = "awaiting_action_card"
    return state


def test_an_enforced_rule_runs_reads_back_and_needs_no_one(box, enforce):
    out = _run()
    assert [c[0] for c in enforce["calls"]] == ["block_ip_new", "get_blocked_ip"]
    assert enforce["calls"][0][1]["_approved"] is True
    assert out["acted"] == [{"card_id": "card-blk", "connector": "fortigate-firewall",
                             "operation": "block_ip_new", "targets": ["198.51.100.7"],
                             "rule": "block-external-ip", "ok": True, "verified": True}]
    assert out["needs_human"] is False and out["waiting"] == 0
    from connector_fsr_agentic_assistant.storage import default_storage
    st = default_storage()
    sid = out["session_id"]
    # Decided once, by the policy; recorded so Undo can reverse it.
    assert st.claim_card_decision(sid, "card-blk", "approve", "x")["actor"] == "system:autonomy"
    assert st.get_card_execution(sid, "card-blk")["ok"] is True
    assert st.get_card(sid, "card-blk")["policy"]["mode"] == "enforced"


@pytest.mark.parametrize("live", [
    {**LIVE, "mode": "shadow"},
    {**LIVE, "enabled": False},                                    # kill switch
    {**LIVE, "rules": [{"id": "some-other-rule"}]},
    None,                                                          # no policy
])
def test_nothing_runs_unless_the_live_policy_enforces_this_rule(box, enforce, live):
    enforce["policy"] = live
    out = _run()
    assert enforce["calls"] == [] and not out.get("acted")
    assert out["why"] == ["awaiting_approval"]


def test_a_would_not_act_card_still_waits(box, enforce):
    box.transcript = [VERDICT, dict(CARD, policy={**POL, "outcome": "would_not_act",
                                                    "failed": ["protected"]})]
    out = _run()
    assert enforce["calls"] == [] and out["why"] == ["awaiting_approval"]


def test_a_block_that_does_not_read_back_goes_to_a_human(box, enforce, monkeypatch):
    import fsr_playbooks.llm.tools as tools
    real = tools.dispatch
    monkeypatch.setattr(tools, "dispatch", lambda n, a, **k: (
        {"ok": True, "data": []} if a.get("op") == "get_blocked_ip" else real(n, a, **k)))
    out = _run()
    assert out["acted"][0]["ok"] is True and out["acted"][0]["verified"] is False
    assert out["needs_human"] is True and out["why"] == ["action_unverified"]


def test_a_failed_action_goes_to_a_human(box, enforce):
    enforce["fail_op"] = True
    out = _run()
    assert [c[0] for c in enforce["calls"]] == ["block_ip_new"]
    assert out["acted"][0]["ok"] is False and out["why"] == ["action_unverified"]


def test_the_shadow_table_shows_who_acted(box, enforce):
    out = _run()
    from connector_fsr_agentic_assistant import operations as ops
    (row,) = [d for d in ops.list_shadow_decisions({}, {})["decisions"]
              if d["session_id"] == out["session_id"]]
    assert row["human"] == "approve" and row["actor"] == "system:autonomy"
    assert row["executed"] is True and row["undo"]["op"] == "unblock_ip"
    assert row["enforced"] == {"by": "system:autonomy", "rule": "block-external-ip",
                               "ok": True, "verified": True}


# ---- phase F: the policy closes a false positive ----------------------------

FP_VERDICT = {"type": "verdict_card", "disposition": "false_positive",
              "severity": "low", "confidence": 0.97,
              "summary": "Known scanner, no hits anywhere", "findings": []}
CLOSE_POL = {"rule": "close-false-positive", "outcome": "would_act", "failed": [],
             "mode": "enforce_unavailable", "targets": [ALERT["uuid"]]}
CLOSE = {"type": "approval_request", "approval_id": "ap-close", "tool": "update_record",
         "tier": 3, "summary": "Close the alert as a false positive",
         "preview": {"tool": "update_record",
                     "args": {"module": "alerts", "uuid": ALERT["uuid"],
                              "fields": {"status": "/api/3/picklists/closed",
                                         "closureReason": "False Positive"}}},
         "policy": CLOSE_POL}
LIVE_CLOSE = {"enabled": True, "mode": "enforce", "rules": [{"id": "close-false-positive"}]}


def _wire(approval):
    """The close as the turn's transcript actually carries it: chat_turn turns
    a record-write approval into an action_card (ops_cards). The tests fed the
    raw approval_request, a shape no live turn delivers, so they passed while
    no policy close could ever run."""
    import ops_cards
    card = ops_cards._approval_to_action_card(approval, approval["preview"]["args"])
    assert card is not None and card["type"] == "action_card"
    return dict(card)


@pytest.fixture
def close(box, monkeypatch):
    """A live close policy, and a resume that applies the suspended update to
    the box's record the way an approved resume would."""
    from connector_fsr_agentic_assistant import operations as ops
    state = {"policy": dict(LIVE_CLOSE), "resumes": [], "apply": True}
    monkeypatch.setattr(ops_approvals, "_load_autonomy_policy", lambda: (state["policy"], None))
    monkeypatch.setattr(ops_autonomy, "_load_autonomy_policy", lambda: (state["policy"], None))
    monkeypatch.setattr(ops_policy, "_load_autonomy_policy", lambda: (state["policy"], None))

    def _resume(config, params, *, _policy_decider=None):
        # The real resume validates its params first; a fake that skipped it
        # passed while every live policy close died there.
        from connector_fsr_agentic_assistant.ops_common import _validate_model
        from connector_fsr_agentic_assistant.pydantic_models import ChatResumeParams
        _validate_model(ChatResumeParams, params)
        state["resumes"].append((params, _policy_decider))
        # The real resume claims the decision before it runs the update.
        from connector_fsr_agentic_assistant.storage import default_storage
        default_storage().claim_card_decision(params["session_id"], params["approval_id"],
                                              "approve", _policy_decider)
        if state["apply"]:
            box.record = {**box.record,
                          "status": {"@id": "/api/3/picklists/closed", "itemValue": "Closed"},
                          "closureReason": "False Positive"}
        return {"ok": True, "stop_reason": "end_turn"}

    monkeypatch.setattr(ops_autonomy, "chat_resume", _resume)
    monkeypatch.setattr(ops, "chat_resume", _resume)
    box.transcript = [FP_VERDICT, _wire(dict(CLOSE, policy=dict(CLOSE_POL)))]
    box.stop_reason = "pending_approval"
    yield state
    box.record = dict(ALERT)


def test_an_enforced_close_is_approved_by_the_policy_read_back_and_needs_no_one(box, close):
    out = _run()
    ((params, decider),) = close["resumes"]
    assert params == {"session_id": out["session_id"], "approval_id": "ap-close",
                      "decision": "approve"}
    assert decider == "system:autonomy"
    assert out["acted"] == [{"card_id": "ap-close", "tool": "update_record",
                             "targets": [ALERT["uuid"]], "rule": "close-false-positive",
                             "ok": True, "verified": True}]
    assert out["needs_human"] is False and out["waiting"] == 0


def test_a_raw_approval_request_close_is_enforced_too(box, close):
    box.transcript = [FP_VERDICT, dict(CLOSE, policy=dict(CLOSE_POL))]
    out = _run()
    assert [a["card_id"] for a in out["acted"]] == ["ap-close"]
    assert out["acted"][0]["verified"] is True


def test_a_would_not_act_close_still_waits(box, close):
    box.transcript = [FP_VERDICT, _wire(dict(CLOSE, policy={**CLOSE_POL, "outcome": "would_not_act",
                                                             "failed": ["the verdict cites no successful lookup"]}))]
    out = _run()
    assert close["resumes"] == [] and "awaiting_approval" in out["why"]


def test_only_the_tools_the_policy_may_approve_are_approved(box, close):
    box.transcript = [FP_VERDICT, _wire(dict(CLOSE, tool="delete_record", policy=dict(CLOSE_POL)))]
    out = _run()
    assert close["resumes"] == [] and not out.get("acted")


def test_a_close_that_does_not_read_back_goes_to_a_human(box, close):
    close["apply"] = False
    out = _run()
    assert out["acted"][0]["ok"] is True and out["acted"][0]["verified"] is False
    assert out["needs_human"] is True and "action_unverified" in out["why"]


def test_the_policy_decider_cannot_be_sent_as_a_param():
    """The reviewer check is skipped only for the keyword argument; a widget
    param of the same name is just an unknown key."""
    import inspect
    from connector_fsr_agentic_assistant import operations as ops
    sig = inspect.signature(ops.chat_resume)
    assert sig.parameters["_policy_decider"].kind is inspect.Parameter.KEYWORD_ONLY


def _comments(box):
    return [body for method, url, body in box.writes
            if method == "POST" and url == "/api/3/comments"]


def test_a_triage_that_enforces_nothing_writes_nothing(box):
    _run()
    assert box.writes == []


def test_an_enforced_close_says_on_the_alert_why_and_how_to_reopen(box, close):
    out = _run()
    assert out["acted"][0]["verified"] is True
    ((body,),) = [_comments(box)]
    assert body["alerts"] == [ALERT["@id"]]
    text = body["content"]
    for part in ("Closed automatically by the Agentic Assistant as a false positive",
                 "97% confidence", "Known scanner, no hits anywhere", "Undo"):
        assert part in text, text
    # Analyst-facing: no rule id, tool name or field name.
    for leak in ("close-false-positive", "update_record", "closureReason"):
        assert leak not in text


def test_a_close_that_does_not_read_back_leaves_no_comment(box, close):
    close["apply"] = False
    _run()
    assert _comments(box) == []


def test_the_monitor_sees_the_policy_close_and_offers_undo(box, close):
    from connector_fsr_agentic_assistant import operations as ops
    out = _run()
    rows = ops.list_shadow_decisions({}, {})["decisions"]
    (row,) = [r for r in rows if r["card_id"] == "ap-close"]
    assert row["session_id"] == out["session_id"]
    assert row["human"] == "approve" and row["actor"] == "system:autonomy"
    assert row["executed"] is True and row["undo"]
    assert row["enforced"]["verified"] is True and row["enforced"]["commented"] is True


def test_undo_reopens_the_alert_with_its_prior_values(box, close):
    from connector_fsr_agentic_assistant import operations as ops
    out = _run()
    assert box.record["status"]["itemValue"] == "Closed"
    box.writes.clear()
    res = ops.undo_action({}, {"session_id": out["session_id"], "card_id": "ap-close",
                               "actor": {"name": "jdoe"}})
    assert res["ok"] is True, res
    puts = [(url, body) for method, url, body in box.writes if method == "PUT"]
    assert puts == [(ALERT["@id"], {"status": None, "closureReason": None})]
    assert box.record["status"] is None and box.record["closureReason"] is None
    (body,) = _comments(box)
    assert "Reopened by jdoe" in body["content"]
    again = ops.undo_action({}, {"session_id": out["session_id"], "card_id": "ap-close"})
    assert again["ok"] is False and again["error"]["code"] == "already_undone"


def test_undo_restores_a_picklist_by_its_iri(box, close):
    from connector_fsr_agentic_assistant import operations as ops
    box.record = {**ALERT, "status": {"@id": "/api/3/picklists/open", "itemValue": "Open"}}
    out = _run()
    box.writes.clear()
    assert ops.undo_action({}, {"session_id": out["session_id"],
                                "card_id": "ap-close"})["ok"] is True
    ((url, body),) = [(u, b) for m, u, b in box.writes if m == "PUT"]
    assert body["status"] == "/api/3/picklists/open"


def test_an_undo_the_record_does_not_take_is_reported_and_can_be_retried(box, close, monkeypatch):
    from connector_fsr_agentic_assistant import operations as ops
    out = _run()
    real = type(box).__call__

    def _ignores_put(self, url, method, body=None, **kw):
        if method == "PUT":
            self.writes.append((method, url, body))
            return {}
        return real(self, url, method, body, **kw)

    monkeypatch.setattr(type(box), "__call__", _ignores_put)
    res = ops.undo_action({}, {"session_id": out["session_id"], "card_id": "ap-close"})
    assert res["ok"] is False and res["error"]["code"] == "undo_failed"
    assert "status" in res["error"]["message"]
    monkeypatch.setattr(type(box), "__call__", real)
    assert ops.undo_action({}, {"session_id": out["session_id"],
                                "card_id": "ap-close"})["ok"] is True


def _close_alert(box, uuid, **fields):
    """Auto-triage one more alert the policy closes."""
    rec = {**ALERT, "@id": f"/api/3/alerts/{uuid}", "uuid": uuid, **fields}
    box.record = rec
    args = {"module": "alerts", "uuid": uuid,
            "fields": {"status": "/api/3/picklists/closed", "closureReason": "False Positive"}}
    box.transcript = [FP_VERDICT, _wire(dict(CLOSE, approval_id=f"ap-{uuid}",
                                             preview={"tool": "update_record", "args": args},
                                             policy={**CLOSE_POL, "targets": [uuid]}))]
    from connector_fsr_agentic_assistant import operations as ops
    return ops.auto_triage({}, {"record": rec["@id"]})


def test_the_monitor_counts_auto_closes_per_detection_rule(box, close):
    from connector_fsr_agentic_assistant import operations as ops
    _close_alert(box, "u-1", rule="Port scan from scanner subnet")
    _close_alert(box, "u-2", rule="Port scan from scanner subnet")
    out = _close_alert(box, "u-3", rule="Port scan from scanner subnet")
    _close_alert(box, "u-4", source="FortiEDR", rule="",
                 type={"@id": "/api/3/picklists/t", "itemValue": "Suspicious Process"})
    ops.undo_action({}, {"session_id": out["session_id"], "card_id": "ap-u-3"})
    tuning = ops.list_shadow_decisions({}, {})["tuning"]
    assert [{k: t[k] for k in ("source", "rule", "closed", "reopened")} for t in tuning] == [
        {"source": "FortiSIEM", "rule": "Port scan from scanner subnet",
         "closed": 3, "reopened": 1},
        {"source": "FortiEDR", "rule": "Suspicious Process", "closed": 1, "reopened": 0},
    ]
    assert all(t["last_at"] > 0 for t in tuning)


def test_a_close_that_did_not_read_back_is_not_counted(box, close):
    from connector_fsr_agentic_assistant import operations as ops
    close["apply"] = False
    _close_alert(box, "u-9", rule="Noisy rule")
    assert ops.list_shadow_decisions({}, {})["tuning"] == []
