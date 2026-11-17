"""Known-answer rows: a recorded connector world, and a grader on the verdict.

The offline default answers EVERY enrichment lookup "malicious, confidence 85",
so before this nothing offline could ask "is this alert benign?" and mean it.
A bundle with `ops` + `strict_ops` fixes the evidence; `verdict_is` grades the
disposition against it. Each rule below is paired with the case that must not
fire -- a grader that can't fail, or a world that silently falls back to the
canned answer, would grade the fixture instead of the agent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts import conv_scenarios as CS  # noqa: E402
from scripts.fixture_box import FixtureBox, FixtureBoxError, load_bundle  # noqa: E402
from scripts.local_conversation import Conversation, Step, unserved_shape  # noqa: E402

KA_ROWS = ("ka_c2_beacon", "ka_authorized_scanner", "ka_cdn_first_seen",
           "ka_prior_case_false_positive", "ka_needle_in_ticket_siem_empty")


def _box(**world):
    return FixtureBox({"modules": {}, **world})


# ------------------------------------------------------------------ world
class TestRecordedOps:
    def test_a_recorded_op_is_served(self):
        b = _box(ops=[{"connector": "virustotal", "op": "query_ip",
                       "match": {"ip": "198.51.100.77"}, "response": {"ok": True, "v": 1}}])
        assert b.op("virustotal", "query_ip", {"ip": "198.51.100.77"}) == {"ok": True, "v": 1}

    def test_a_different_argument_is_not(self):
        # Serving the malicious IP's report for any IP would hand every
        # indicator the same verdict -- the canned-table bug again.
        b = _box(ops=[{"connector": "virustotal", "op": "query_ip",
                       "match": {"ip": "198.51.100.77"}, "response": {"ok": True}}])
        assert b.op("virustotal", "query_ip", {"ip": "10.0.0.1"}) is None

    def test_connector_globs_cover_both_fortisiem_versions(self):
        b = _box(ops=[{"connector": "fortinet-fortisiem*", "op": "execute_api_request",
                       "match": {"endpoint": "/q"}, "response": {"ok": True}}])
        for name in ("fortinet-fortisiem", "fortinet-fortisiemv2"):
            assert b.op(name, "execute_api_request", {"endpoint": "/q"}) == {"ok": True}

    def test_an_entry_without_a_response_is_a_bundle_error(self):
        with pytest.raises(FixtureBoxError):
            _box(ops=[{"connector": "virustotal", "op": "query_ip"}])

    def test_a_plain_bundle_has_no_world(self):
        # Every pre-existing row must keep the canned defaults it was written
        # against.
        assert not _box().has_world
        assert _box(strict_ops=True).has_world


def test_a_strict_world_never_falls_back_to_the_canned_malicious_answer(monkeypatch):
    from scripts import local_turn as LT
    from fsr_playbooks.llm.tools import REGISTRY
    world = _box(strict_ops=True)
    monkeypatch.setattr(LT, "_ACTIVE_WORLD", world)
    monkeypatch.setattr(LT, "_ACTIVE_CASSETTE", None)
    with LT.patch_scope() as scope:
        LT._patch_run_op_for_cassette(scope)
        out = REGISTRY["run_op"].fn(connector="virustotal", op="query_ip",
                                    params={"ip": "203.0.113.9"})
    assert out["ok"] is False and out["code"] == "unserved_op"
    assert "malicious" not in json.dumps(out)


def test_op_misses_keep_their_name_in_the_unserved_report():
    assert unserved_shape("OP fortinet-fortisiemv2.get_entity_context") == \
        "OP fortinet-fortisiemv2.get_entity_context"


# ----------------------------------------------------------------- grader
def _step_with(frames):
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {"transcript": frames}
    return s


def _verdict(disposition):
    return {"type": "verdict_card", "id": "v", "disposition": disposition}


def _grade(frames, any_of):
    ctx = CS.ConvCtx(conv=Conversation(name="t", steps=[_step_with(frames)]), mounted_yaml="")
    return CS.verdict_is(any_of=any_of)(ctx)


def test_the_right_disposition_passes():
    ok, _ = _grade([_verdict("false_positive")], ("false_positive", "benign"))
    assert ok


def test_the_wrong_disposition_fails():
    ok, why = _grade([_verdict("suspicious")], ("true_positive",))
    assert not ok and "suspicious" in why


def test_no_verdict_card_fails():
    # An action card is not an answer to "what is this?" -- the defect the
    # C2 row found on its first run.
    action = {"type": "tool_use", "name": "emit_card",
              "input": {"card_type": "action", "payload": {"op": "block_ip_new"}}}
    ok, why = _grade([action], ("true_positive",))
    assert not ok and "no verdict card" in why


def test_a_refused_verdict_attempt_is_not_a_verdict():
    """The emit_card CALL is an attempt; only the delivered card answers."""
    attempt = {"type": "tool_use", "name": "emit_card",
               "input": {"card_type": "verdict",
                         "payload": {"disposition": "true_positive"}}}
    ok, why = _grade([attempt], ("true_positive",))
    assert not ok and "no verdict card" in why


def test_a_verdict_kept_ahead_of_a_gating_card_counts():
    """The wire cuts the calls after an action card but keeps the verdict."""
    action = {"type": "action_card", "id": "a", "operation": "block_ip_new"}
    ok, _ = _grade([_verdict("true_positive"), action], ("true_positive",))
    assert ok


def test_the_last_verdict_wins():
    ok, _ = _grade([_verdict("suspicious"), _verdict("true_positive")], ("true_positive",))
    assert ok


# ------------------------------------------------------------------ rows
@pytest.mark.parametrize("name", KA_ROWS)
def test_every_known_answer_row_fixes_its_evidence(name):
    spec = json.loads((_ROOT / "scripts/conversations/sweep" / f"{name}.json").read_text())
    assert "verdict_is" in spec["expect"]
    box = FixtureBox(load_bundle(spec["records"]))
    assert box.strict_ops and box.ops
    assert box.record("alerts", spec["mount"]["uuid"]) is not None


@pytest.mark.parametrize("name", KA_ROWS)
def test_the_alert_does_not_state_the_answer(name):
    # If the record says "benign", the row grades reading, not investigating.
    spec = json.loads((_ROOT / "scripts/conversations/sweep" / f"{name}.json").read_text())
    alert = FixtureBox(load_bundle(spec["records"])).record("alerts", spec["mount"]["uuid"])
    text = json.dumps({k: alert[k] for k in ("name", "description")}).lower()
    for word in ("benign", "false positive", "malicious", "true positive", "cobalt"):
        assert word not in text


def test_verdict_is_is_refused_without_a_strict_world(tmp_path):
    from scripts import chat_sweep
    graders = chat_sweep.build_graders({"verdict_is": "benign"}, where="t")
    with pytest.raises(SystemExit):
        chat_sweep._known_answer_guard({"records": "soc_alert_surface"}, graders, where="t")


def test_recording_redacts_the_box_before_writing(monkeypatch):
    import re
    from scripts import record_world
    # The real lab range comes from the local-only overlay; pin a stand-in so the
    # test exercises the redaction on a public clone too.
    monkeypatch.setattr(record_world, "_LAB_RANGE", re.compile(r"\b10\.250\.\d{1,3}\.\d{1,3}\b"))
    scrub = record_world._redactor("fsr-lab-7.internal")
    out = scrub({"url": "https://fsr-lab-7.internal/api/3/alerts", "mgmt": "10.250.12.34",
                 "alert_ip": "10.50.60.70"})
    assert "fsr-lab-7.internal" not in json.dumps(out)
    assert "10.250." not in json.dumps(out)
    # The investigation's own data is evidence, not infrastructure.
    assert out["alert_ip"] == "10.50.60.70"


def test_recorded_ops_match_on_every_scalar_param():
    from scripts.record_world import _ops_from_calls
    calls = [{"connector": "virustotal", "op": "query_ip", "params": {"ip": "1.2.3.4"},
              "response": {"ok": True, "n": 1}},
             {"connector": "virustotal", "op": "query_ip", "params": {"ip": "1.2.3.4"},
              "response": {"ok": True, "n": 2}},
             {"connector": "virustotal", "op": "query_ip", "params": {"ip": "5.6.7.8"},
              "response": {"ok": True, "n": 3}}]
    ops = _ops_from_calls(calls)
    assert [o["response"]["n"] for o in ops] == [1, 3]   # first answer wins, per replay
    assert ops[1]["match"] == {"ip": "5.6.7.8"}


# ------------------------------------------------------- autonomy policy rows
class TestPolicyWorld:
    def test_a_bundle_key_store_record_is_served_by_key(self):
        b = _box(keys=[{"key": "fsr_autonomy_policy", "value": "{}"}])
        assert b.keys_body("fsr_autonomy_policy")["hydra:member"] == [
            {"key": "fsr_autonomy_policy", "value": "{}"}]
        assert b.keys_body("fsr_assistant_profile:alerts")["hydra:member"] == []

    def test_it_is_served_on_the_module_persona_substrate_too(self):
        b = _box(keys=[{"key": "fsr_autonomy_policy", "value": "{}"}],
                 persona_source="module")
        assert len(b.keys_body("fsr_autonomy_policy")["hydra:member"]) == 1

    def test_the_policy_rows_fix_their_world(self):
        bundle = load_bundle("ka_c2_beacon_policy")
        assert FixtureBox(bundle).strict_ops
        assert json.loads(bundle["keys"][0]["value"])["rules"][0]["id"] == "block-external-ip"

    def test_the_close_policy_row_fixes_its_world(self):
        """Phase F: the scanner evidence plus a close-false-positive rule."""
        bundle = load_bundle("ka_authorized_scanner_close_policy")
        assert FixtureBox(bundle).strict_ops
        rule = json.loads(bundle["keys"][0]["value"])["rules"][0]
        assert rule["id"] == "close-false-positive"
        assert rule["action"]["tool"] == "update_record"
        assert bundle["modules"]["alerts"][0]["uuid"] == "5c1e2a90-1b7d-4c55-9a3e-7d0c2b1a4e02"


def _ctx_with_cards(*cards):
    step = Step(index=0, kind="user", sent="x", transcript=list(cards))
    conv = Conversation.__new__(Conversation)
    conv.steps = [step]
    return CS.ConvCtx(conv=conv, mounted_yaml="")


class TestPolicyDecidedGrader:
    g = staticmethod(lambda **k: CS.policy_decided(**k))

    def test_the_decision_on_the_last_card_is_graded(self):
        c = _ctx_with_cards({"type": "action_card", "policy": {
            "rule": "block-external-ip", "outcome": "would_act", "failed": []}})
        assert self.g(outcome="would_act", rule="block-external-ip")(c)[0] is True
        assert self.g(outcome="would_not_act")(c)[0] is False

    def test_a_record_write_is_graded_on_its_approval_card(self):
        """A phase F close is an approval_request (update_record), not an
        action card; the grader read only action cards, so a close row could
        never pass."""
        c = _ctx_with_cards({"type": "approval_request", "tool": "update_record", "policy": {
            "rule": "close-false-positive", "outcome": "would_act", "failed": []}})
        assert self.g(outcome="would_act", rule="close-false-positive")(c)[0] is True
        assert self.g(outcome="would_not_act")(c)[0] is False

    def test_no_card_or_no_decision_fails(self):
        assert self.g(outcome="would_act")(_ctx_with_cards())[0] is False
        assert self.g(outcome="would_act")(
            _ctx_with_cards({"type": "action_card"}))[0] is False

    def test_a_decision_under_another_rule_fails(self):
        c = _ctx_with_cards({"type": "action_card", "policy": {
            "rule": "other", "outcome": "would_act"}})
        assert self.g(outcome="would_act", rule="block-external-ip")(c)[0] is False


def test_a_world_key_store_record_beats_the_persona_keys_rule():
    """The cassette's persona rules are blanket `/api/3/keys` substrings checked
    BEFORE the box; they answered the policy lookup with the persona list, so
    the policy rows ran with no policy at all."""
    from scripts.local_turn import _CassetteSession
    box = FixtureBox({"modules": {}, "keys": [{"key": "fsr_autonomy_policy", "value": "{}"}]})
    persona_rule = ("/api/3/keys", {"hydra:member": [{"key": "fsr_assistant_profile:alerts"}]})
    sess = _CassetteSession([persona_rule], box=box)
    got = sess.get("https://x/api/3/keys?key=fsr_autonomy_policy&$limit=1").json()
    assert got["hydra:member"] == [{"key": "fsr_autonomy_policy", "value": "{}"}]
    # A persona lookup still goes to the persona rule.
    got = sess.get("https://x/api/3/keys?key=fsr_assistant_profile%3Aalerts&$limit=1").json()
    assert got["hydra:member"][0]["key"] == "fsr_assistant_profile:alerts"


# ------------------------------------------------------- auto-triage rows
AUTO_ROWS = ("ka_auto_c2_beacon", "ka_auto_authorized_scanner", "ka_auto_cdn_first_seen")


def _stopped(stop, *frames):
    step = Step(index=1, kind="user", sent="x", transcript=list(frames))
    step.stop = stop
    return CS.ConvCtx(conv=Conversation(name="t", steps=[step]), mounted_yaml="")


def _triaged(stop, uuid, *frames, module="alerts"):
    """A finished auto-triage turn over the record `uuid`."""
    step = Step(index=1, kind="user", sent="x", transcript=list(frames))
    step.stop = stop
    return CS.ConvCtx(conv=Conversation(name="t", steps=[step]),
                      mounted_yaml="", triaged_uuid=uuid, triaged_module=module)


def _close_card(uuid, approval_id="ap-x", **extra):
    """The agent's own update_record close of record `uuid`, as the turn carries
    it (an action_card)."""
    return {"type": "action_card", "id": approval_id, "approval_id": approval_id,
            "tool": "update_record", "target": {"module": "alerts", "uuid": uuid},
            "args": {"status": "/api/3/picklists/closed"}, **extra}


class TestEscalatesGrader:
    """Graded by the auto_triage op's own outcome, so each case below is a
    queue decision the box would make."""

    def test_a_closed_false_positive_does_not_escalate(self):
        c = _stopped("end_turn", _verdict("false_positive"))
        assert CS.escalates(needs_human=False)(c)[0] is True
        assert CS.escalates(needs_human=True)(c)[0] is False

    def test_a_staged_action_escalates_with_its_reason(self):
        c = _stopped("awaiting_action_card", _verdict("true_positive"),
                     {"type": "action_card", "id": "a"})
        assert CS.escalates(needs_human=True, why=("awaiting_approval",))(c)[0] is True
        assert CS.escalates(needs_human=False)(c)[0] is False

    def test_a_true_positive_with_nothing_staged_escalates_but_stages_nothing(self):
        # It reaches the queue (confirmed_threat), but a row demanding the
        # containment card still fails: the agent did not stage it.
        c = _stopped("end_turn", _verdict("true_positive"))
        assert CS.escalates(needs_human=True, why=("confirmed_threat",))(c)[0] is True
        ok, why = CS.escalates(needs_human=True, why=("awaiting_approval",))(c)
        assert not ok and "confirmed_threat" in why

    def test_a_question_nobody_answers_escalates_a_right_verdict(self):
        c = _stopped("awaiting_choice", _verdict("benign"))
        ok, why = CS.escalates(needs_human=False)(c)
        assert not ok and "awaiting_answer" in why

    def test_a_missing_reason_fails(self):
        c = _stopped("end_turn", _verdict("suspicious"))
        assert CS.escalates(needs_human=True, why=("awaiting_approval",))(c)[0] is False

    def test_a_staged_false_positive_close_waits_for_a_human_offline(self):
        """Policy-driven: authorizing an FP close is the autonomy policy's job,
        not the agent's. The grader binds no policy, so a staged close waits for
        a human -- the default (shadow) behavior. Whether an enforce-mode policy
        would auto-close is proven by the auto_triage policy unit tests and the
        live gate, not by this offline routing grade."""
        c = _triaged("awaiting_action_card", "u1", _verdict("false_positive"),
                     _close_card("u1"))
        assert CS.escalates(needs_human=True, why=("awaiting_approval",))(c)[0] is True
        assert CS.escalates(needs_human=False)(c)[0] is False


@pytest.mark.parametrize("name", AUTO_ROWS)
def test_auto_rows_drive_the_ops_own_prompt_on_a_strict_world(name):
    row = json.loads((_ROOT / "scripts/conversations/sweep" / f"{name}.json").read_text())
    assert row["auto_triage"] is True and "steps" not in row
    assert FixtureBox(load_bundle(row["records"])).strict_ops
    assert "escalates" in row["expect"] and "verdict_is" in row["expect"]


def test_an_auto_row_with_its_own_message_is_refused():
    from scripts.local_conversation import DriveError, run_conversation
    row = json.loads((_ROOT / "scripts/conversations/sweep/ka_auto_c2_beacon.json").read_text())
    with pytest.raises(DriveError, match="takes no `steps`"):
        run_conversation({**row, "steps": [{"user": "hi"}]}, llm="fake")


def test_because_pins_the_failed_check():
    c = _ctx_with_cards({"type": "action_card", "policy": {
        "rule": "block-external-ip", "outcome": "would_not_act",
        "failed": ["confidence 0.7 is below 0.9"]}})
    assert CS.policy_decided(outcome="would_not_act")(c)[0] is True
    assert CS.policy_decided(outcome="would_not_act", because="protected list")(c)[0] is False


def test_the_protected_world_protects_the_c2_address():
    bundle = load_bundle("ka_c2_beacon_protected")
    pol = json.loads(bundle["keys"][0]["value"])
    assert "198.51.100.77" in pol["protected"]["protected_ips"]
    assert FixtureBox(bundle).strict_ops


class _BoxSession:
    """`client.session` over a FixtureBox: what the connector's tools POST to."""

    def __init__(self, box):
        self._box = box

    def post(self, url, json=None):
        status, body = self._box.post(url, json)

        class _R:
            status_code = status
            text = ""

            def json(self_inner):
                return body
        return _R()


def test_prior_case_row_is_decided_by_the_earlier_case_the_tool_finds(monkeypatch):
    """X2: the world serves the earlier-case lookup, and what it serves is the
    false-positive precedent: not the alert under triage, not the unrelated
    true positive on another address."""
    from fsr_playbooks.mcp_server import _shared
    from fsr_soc_triage import tools_prior_cases

    spec = json.loads((_ROOT / "scripts/conversations/sweep/ka_prior_case_false_positive.json"
                       ).read_text())
    box = FixtureBox(load_bundle(spec["records"]))
    client = type("C", (), {"base_url": "https://soar.example.com",
                            "session": _BoxSession(box)})()
    monkeypatch.setattr(_shared, "_live_client", lambda: client)
    monkeypatch.setattr(_shared, "active_record_iri",
                        lambda: f"/api/3/alerts/{spec['mount']['uuid']}")
    monkeypatch.setattr(tools_prior_cases, "_request",
                        lambda m, c, url, **kw: getattr(c.session, m)(url, **kw))

    alert = box.record("alerts", spec["mount"]["uuid"])
    out = tools_prior_cases.prior_cases(alert["destinationIp"], type="ip")
    assert out["count"] == 1, out
    [case] = out["cases"]
    assert case["closed_as"] == "False Positive" and case["status"] == "Closed"
    assert "Partner SFTP gateway" in case["closure_notes"]
    assert case["matched_as"] == "destination IP"
    assert "/api/query/alerts" in " ".join(box.reads)


def test_needle_in_ticket_world_serves_the_ticket_and_an_empty_siem():
    """I5: the SIEM answers nothing for the source address; the deciding fact is
    only in a ServiceNow ticket, next to a distractor ticket on another host."""
    spec = json.loads((_ROOT / "scripts/conversations/sweep/ka_needle_in_ticket_siem_empty.json"
                       ).read_text())
    box = FixtureBox(load_bundle(spec["records"]))
    alert = box.record("alerts", spec["mount"]["uuid"])
    assert alert["sourceIp"] == "198.51.100.88"

    # SIEM: configured, query accepted, and zero events come back.
    siem = "fortinet-fortisiem-v2"
    assert any(c["name"].startswith("fortinet-fortisiem")
               for c in box.configured_connectors)
    assert box.op(siem, "execute_api_request",
                  {"endpoint": "/rest/pub/v2/query/eventQuery"})["ok"]
    assert box.op(siem, "execute_api_request",
                  {"endpoint": "/rest/pub/v2/query/events/results"})["data"] == []

    # ServiceNow: configured, serves both ops, and the ticket holds the answer.
    assert any(c["name"] == "servicenow" for c in box.configured_connectors)
    for op in ("advance_search", "fetch_incidents"):
        tickets = box.op("servicenow", op, {"table_name": "incident"})["data"]["result"]
        by_num = {t["number"]: t for t in tickets}
        needle, distractor = by_num["INC0043817"], by_num["INC0044102"]
        assert alert["sourceIp"] in needle["description"]
        assert "web-dmz-03" in needle["description"]
        assert "penetration test" in needle["short_description"].lower()
        assert alert["sourceIp"] not in distractor["description"]
        assert "web-dmz-03" not in json.dumps(distractor)

    # search_record is served per value: the alert's host or address finds the
    # needle, the other host finds only the distractor, an unrelated value misses.
    for v in ("web-dmz-03", alert["sourceIp"]):
        got = box.op("servicenow", "search_record",
                     {"table_name": "incident", "column_name": "description", "value": v})
        assert [t["number"] for t in got["data"]["result"]] == ["INC0043817"]
    got = box.op("servicenow", "search_record", {"table_name": "incident", "value": "app-db-09"})
    assert [t["number"] for t in got["data"]["result"]] == ["INC0044102"]
    assert box.op("servicenow", "search_record", {"table_name": "incident", "value": "nobody"}) is None

    # The ticket fact is nowhere in the alert, so only a ServiceNow read finds it.
    assert "INC0043817" not in json.dumps(alert)
    # Unrecorded ops stay honest misses (no canned "malicious" fallback).
    assert box.strict_ops and box.op("servicenow", "get_SIR", {}) is None
