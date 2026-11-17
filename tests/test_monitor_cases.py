"""Monitor revamp data layer (V2 M1-M5), box-free.

The sqlite store is the real `Storage` on a temp file, seeded with the rows an
auto-triage run leaves behind (sessions, stored cards, decisions, executions,
assistant frames). The SOAR client is a fake that serves record reads. Every
bucket and filter is checked by value.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

import ops_common
import ops_monitor
import ops_policy

HOUR = 3600.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "test.db"))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    s = storage.default_storage()
    monkeypatch.setattr(ops_policy, "_load_autonomy_policy",
                        lambda force=False: ({"mode": "shadow", "rules": []}, None))
    yield s
    storage._DEFAULT_STORAGE = None


@pytest.fixture
def soar(monkeypatch):
    """A fake SOAR client: GET returns the record registered for the IRI."""
    class _Soar:
        records: dict = {}
        reads: list = []

        def __call__(self, url, method, body=None, **kw):
            self.reads.append(url)
            return dict(self.records.get(url, {}))
    s = _Soar()
    s.records, s.reads = {}, []
    monkeypatch.setattr(ops_common, "_make_request", lambda: s)
    return s


ACTOR = {"name": "Auto-triage", "iri": "system:auto-triage", "system": True}


def iri(key: str) -> str:
    return f"/api/3/alerts/{key}"


def seed(store, key, *, age_h=1.0, verdict=None, tools=(), cards=(),
         executions=(), undone=(), name=None, escalate=None):
    """One auto-triage session for alert `key`, started `age_h` hours ago."""
    sid = f"auto:alerts:{key}"
    assert store.claim_session(sid, ACTOR)
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE session_initiator SET created_at=? WHERE session_id=?",
                  (time.time() - age_h * HOUR, sid))
    frames = [{"type": "tool_use", "name": n, "input": a} for n, a in tools]
    if verdict:
        frames.append({"type": "verdict_card", "disposition": verdict[0],
                       "confidence": verdict[1], "severity": "high",
                       "summary": "s", "findings": []})
    if frames:
        store.append_transcript_assistant(sid, frames)
    for card in cards:
        store.save_card(sid, card)
    for card_id in executions:
        store.record_card_execution(sid, card_id, connector="fortisoar",
                                    operation="update_record", config=None,
                                    args={}, ok=True)
    for card_id in undone:
        assert store.claim_card_undo(sid, card_id, "analyst") is None
    if escalate:
        store.save_card(sid, {
            "type": "escalation_card", "id": "esc_" + key,
            "record": {"name": name, "module": "alerts", "iri": iri(key)},
            "verdict": escalate.get("verdict"), "why": ["needs_review"],
            "reasons": escalate["reasons"], "waiting": 0,
            "priority": "P2", "priority_rank": 2})
    return sid


def close_card(card_id, *, source="FortiSIEM", rule="Beacon", rule_id="close-false-positive"):
    return {"type": "approval_request", "approval_id": card_id,
            "tool": "update_record",
            "policy": {"rule": rule_id, "outcome": "would_act", "mode": "enforced"},
            "enforced": {"by": "system:autonomy", "rule": rule_id, "ok": True,
                         "verified": True, "undoable": True,
                         "detection": {"source": source, "rule": rule}}}


def block_card(card_id):
    return {"type": "action_card", "id": card_id, "connector": "fortigate-firewall",
            "operation": "block_ip_new", "title": "Blocked 198.51.100.7",
            "policy": {"rule": "block-c2", "outcome": "would_act", "mode": "enforced"},
            "enforced": {"by": "system:autonomy", "rule": "block-c2", "ok": True,
                         "verified": True, "undoable": True}}


INTEL = ("run_op", {"connector": "virustotal", "op": "ip_reputation"})
SIEM = ("siem_search_ip", {"ip": "198.51.100.7"})
EDR = ("edr_host", {"host": "h1"})


@pytest.fixture
def world(store, soar):
    """Alerts in every bucket, one outside the window, one storm group."""
    seed(store, "a-handled", age_h=1, verdict=("benign", 0.88), name="Login burst",
         tools=[SIEM, INTEL, ("find", {}), ("emit_card", {})],
         cards=[close_card("ap-a")], executions=["ap-a"])
    seed(store, "b-blocked", age_h=2, verdict=("true_positive", 0.92),
         tools=[SIEM, EDR], cards=[block_card("act-b")])
    seed(store, "c-escalated", age_h=3, name="Phish report",
         verdict=("needs_more_info", None), tools=[("faz_search", {})],
         escalate={"reasons": ["The verdict needs review: needs more information."],
                   "verdict": {"disposition": "needs_more_info"}})
    seed(store, "d-shadow", age_h=4, verdict=("benign", 0.81), tools=[INTEL],
         cards=[{"type": "approval_request", "approval_id": "ap-d",
                 "tool": "update_record",
                 "policy": {"rule": "close-false-positive", "outcome": "would_act",
                            "mode": "shadow"}}])
    seed(store, "e-noaction", age_h=5, verdict=("suspicious", 0.6), tools=[SIEM])
    seed(store, "f-undone", age_h=6, verdict=("benign", 0.9), tools=[SIEM],
         cards=[close_card("ap-f", rule="Port scan")], executions=["ap-f"],
         undone=["ap-f"])
    seed(store, "g-open-again", age_h=7, verdict=("benign", 0.85), tools=[SIEM],
         cards=[close_card("ap-g", rule="Port scan")], executions=["ap-g"])
    seed(store, "h-old", age_h=48, verdict=("benign", 0.9), tools=[SIEM],
         cards=[close_card("ap-h")], executions=["ap-h"])
    # A storm: lead L with two members that never get a session.
    seed(store, "l-lead", age_h=0.5, verdict=("benign", 0.9), tools=[SIEM],
         cards=[close_card("ap-l")], executions=["ap-l"])
    now = time.time()
    store.route_triage(iri("l-lead"), "auto:alerts:l-lead", ["host=h1|x"], 900, 0, 3600, now)
    for m in ("m1", "m2"):
        r = store.route_triage(iri(m), f"auto:alerts:{m}", ["host=h1|x"], 900, 0, 3600, now)
        assert r["role"] == "member"
    soar.records = {
        iri("a-handled"): {"name": "Login burst", "status": {"itemValue": "Closed"}},
        iri("b-blocked"): {"name": "Outbound C2", "status": {"itemValue": "Investigating"}},
        iri("d-shadow"): {"name": "New CDN host", "status": {"itemValue": "Open"}},
        iri("e-noaction"): {"name": "Odd login", "status": {"itemValue": "Open"}},
        iri("f-undone"): {"name": "Scan A", "status": {"itemValue": "Open"}},
        iri("g-open-again"): {"name": "Scan B", "status": {"itemValue": "Investigating"}},
        iri("l-lead"): {"name": "Beacon storm", "status": {"itemValue": "Closed"}},
    }
    return store


def cases(**params):
    return ops_monitor.monitor_cases({}, {"view": "list", **params})


def by_key(out):
    return {r["session_id"].split(":")[-1]: r for r in out["rows"]}


# ---- M1: every bucket, by value --------------------------------------------

def test_every_row_has_its_bucket_verdict_sources_and_action(world):
    out = cases()
    rows = by_key(out)
    assert out["ok"] and out["view"] == "list" and out["hours"] == 24
    assert set(rows) == {"a-handled", "b-blocked", "c-escalated", "d-shadow",
                         "e-noaction", "f-undone", "g-open-again", "l-lead"}

    a = rows["a-handled"]
    assert (a["bucket"], a["verdict"], a["confidence"]) == ("handled", "Benign", 0.88)
    assert a["sources"] == ["FortiSIEM", "Threat intelligence"]
    assert (a["action"], a["action_kind"], a["undoable"]) == ("Closed the record", "taken", True)
    assert a["record_iri"] == iri("a-handled") and a["record_name"] == "Login burst"
    assert a["action_card_id"] == "ap-a" and a["group_size"] == 1

    b = rows["b-blocked"]
    assert (b["bucket"], b["verdict"], b["confidence"]) == ("handled", "True positive", 0.92)
    assert b["sources"] == ["FortiSIEM", "FortiEDR"]
    assert (b["action"], b["action_kind"]) == ("Blocked 198.51.100.7", "taken")

    c = rows["c-escalated"]
    assert (c["bucket"], c["action"], c["action_kind"]) == ("needs_you", "Sent to you", "escalated")
    assert c["verdict"] == "Needs more information" and c["confidence"] is None
    assert c["sources"] == ["FortiAnalyzer"] and c["record_name"] == "Phish report"
    assert c["reasons"] == ["The verdict needs review: needs more information."]

    d = rows["d-shadow"]
    assert (d["bucket"], d["action"], d["action_kind"]) == (
        "needs_you", "Would have closed the record", "would_have")
    assert d["undoable"] is False and d["record_name"] == "New CDN host"

    e = rows["e-noaction"]
    assert (e["bucket"], e["action"], e["action_kind"]) == (
        "needs_you", "No action taken", "none")
    assert e["verdict"] == "Suspicious"

    f = rows["f-undone"]
    assert (f["bucket"], f["action_kind"], f["undoable"]) == ("reopened", "taken", False)

    g = rows["g-open-again"]
    assert (g["bucket"], g["undoable"]) == ("reopened", False)

    assert out["counts"] == {"handled": 3, "needs_you": 3, "reopened": 2}
    assert out["total"] == 8


@pytest.mark.parametrize("bucket,expect", [
    ("handled", {"a-handled", "b-blocked", "l-lead"}),
    ("needs_you", {"c-escalated", "d-shadow", "e-noaction"}),
    ("reopened", {"f-undone", "g-open-again"}),
])
def test_bucket_filter(world, bucket, expect):
    out = cases(bucket=bucket)
    assert {r["session_id"].split(":")[-1] for r in out["rows"]} == expect
    assert {r["bucket"] for r in out["rows"]} == {bucket}
    assert out["total"] == len(expect) and out["bucket"] == bucket
    # The tab badges stay the whole window's counts.
    assert out["counts"] == {"handled": 3, "needs_you": 3, "reopened": 2}


def test_newest_first_and_window(world):
    out = cases()
    assert [r["session_id"].split(":")[-1] for r in out["rows"]] == [
        "l-lead", "a-handled", "b-blocked", "c-escalated", "d-shadow",
        "e-noaction", "f-undone", "g-open-again"]
    assert "h-old" not in by_key(out)
    wide = cases(hours=72)
    assert "h-old" in by_key(wide) and wide["total"] == 9
    assert wide["rows"][-1]["session_id"].endswith("h-old")
    narrow = cases(hours=2.5)
    assert {k for k in by_key(narrow)} == {"l-lead", "a-handled", "b-blocked"}


def test_cap_keeps_the_true_total(world):
    out = cases(limit=3)
    assert len(out["rows"]) == 3 and out["total"] == 8
    assert [r["session_id"].split(":")[-1] for r in out["rows"]] == [
        "l-lead", "a-handled", "b-blocked"]
    capped = cases(bucket="needs_you", limit=1)
    assert capped["total"] == 3 and len(capped["rows"]) == 1
    assert capped["rows"][0]["session_id"].endswith("c-escalated")


def test_storm_members_are_not_rows_and_the_lead_carries_group_size(world):
    out = cases()
    keys = set(by_key(out))
    assert "m1" not in keys and "m2" not in keys
    assert by_key(out)["l-lead"]["group_size"] == 3
    assert out["total"] == 8


def test_stray_session_for_a_member_record_is_left_out(world):
    # A member record that somehow also has a session of its own.
    seed(world, "m1", age_h=0.2, verdict=("benign", 0.9), tools=[SIEM])
    assert "m1" not in by_key(cases())


def test_reopen_check_can_be_skipped(world):
    out = cases(check_reopened=False)
    assert by_key(out)["g-open-again"]["bucket"] == "handled"
    assert by_key(out)["f-undone"]["bucket"] == "reopened"   # stored undo needs no read


def test_a_failed_read_neither_reopens_nor_breaks_the_list(world, soar, monkeypatch):
    def boom(url, method, body=None, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr(ops_common, "_make_request", lambda: boom)
    out = cases()
    assert by_key(out)["g-open-again"]["bucket"] == "handled"
    assert out["ok"]


def test_unknown_view_and_bucket_are_refused(world):
    assert ops_monitor.monitor_cases({}, {"view": "nope"})["error"]["code"] == "bad_view"
    assert cases(bucket="nope")["error"]["code"] == "bad_bucket"


def test_analyst_text_has_no_tool_names(world):
    blob = json.dumps([[r["action"], r["sources"], r["verdict"], r["reasons"]]
                       for r in cases()["rows"]])
    for raw in ("siem_", "run_op", "update_record", "edr_host", "faz_search",
                "emit_card", "block_ip_new"):
        assert raw not in blob


# ---- M3: feedback ---------------------------------------------------------

def rate(record, rating, actor="alice", note=None):
    return ops_monitor.monitor_feedback(
        {}, {"record": record, "rating": rating, "note": note, "actor": {"iri": actor}})


def test_feedback_write_then_read_back_with_the_row(world):
    out = rate(iri("a-handled"), "right", "alice", note="Matches the SIEM data")
    assert out["ok"] and out["session_id"] == "auto:alerts:a-handled"
    assert out["feedback"] == {"mine": "right", "note": "Matches the SIEM data",
                               "right": 1, "wrong": 0}
    row = by_key(cases(actor={"iri": "alice"}))["a-handled"]
    assert row["feedback"] == {"mine": "right", "note": "Matches the SIEM data",
                               "right": 1, "wrong": 0}
    # Another analyst sees the totals but not alice's answer as theirs.
    other = by_key(cases(actor={"iri": "bob"}))["a-handled"]
    assert other["feedback"] == {"mine": None, "note": None, "right": 1, "wrong": 0}


def test_feedback_is_idempotent_per_analyst_last_write_wins(world):
    rate(iri("a-handled"), "right", "alice")
    rate(iri("a-handled"), "right", "alice")
    assert rate(iri("a-handled"), "right", "alice")["feedback"]["right"] == 1
    flipped = rate(iri("a-handled"), "wrong", "alice", note="It was a real login")
    assert flipped["feedback"] == {"mine": "wrong", "note": "It was a real login",
                                   "right": 0, "wrong": 1}
    two = rate(iri("a-handled"), "right", "bob")
    assert two["feedback"] == {"mine": "right", "note": None, "right": 1, "wrong": 1}
    assert len(world.get_decision_feedback(iri("a-handled"))) == 2


def test_empty_rating_clears_and_bad_rating_is_refused(world):
    rate(iri("a-handled"), "wrong", "alice")
    cleared = rate(iri("a-handled"), "", "alice")
    assert cleared["ok"] and cleared["feedback"] == {
        "mine": None, "note": None, "right": 0, "wrong": 0}
    assert rate(iri("a-handled"), "maybe", "alice")["error"]["code"] == "bad_rating"


def test_feedback_needs_a_triaged_record_and_an_analyst(world):
    assert rate(iri("never-seen"), "right")["error"]["code"] == "not_found"
    anon = ops_monitor.monitor_feedback({}, {"record": iri("a-handled"), "rating": "right"})
    assert anon["error"]["code"] == "no_analyst"
    assert rate("not-an-iri", "right")["error"]["code"] == "bad_iri"


def test_verified_caller_outranks_the_claimed_actor(world):
    from connector_fsr_agentic_assistant.ops_approvals import _CALLER
    tok = _CALLER.set({"uuid": "u-123"})
    try:
        rate(iri("a-handled"), "right", "someone-else")
    finally:
        _CALLER.reset(tok)
    assert [e["analyst"] for e in world.get_decision_feedback(iri("a-handled"))] == ["u-123"]


# ---- M2: KPIs -------------------------------------------------------------

def test_kpis_values(world):
    rate(iri("a-handled"), "right", "alice")
    rate(iri("a-handled"), "right", "bob")
    rate(iri("b-blocked"), "right", "alice")
    rate(iri("l-lead"), "wrong", "alice")
    world.record_agent_usage({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                              "session_id": "x", "cost_usd": 0.5, "status": "success"})
    world.record_agent_usage({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                              "session_id": "y", "cost_usd": 0.34, "status": "success"})
    # A deferred record (storm cap) is counted, not dropped.
    world.route_triage(iri("z-deferred"), "auto:alerts:z", [], 900, 1, 3600, time.time())
    out = ops_monitor.monitor_cases({}, {"view": "kpis"})
    assert out["view"] == "kpis"
    assert out["kpis"] == {
        "handled": 3, "auto_closed": 4, "escalated": 1, "needs_you": 3, "reopened": 2,
        "rated": 4, "right": 3, "wrong": 1, "agreement": 0.75,
        "deferred": 1, "cost_today_usd": 0.84}


def test_kpis_with_nothing_rated_have_no_agreement_and_no_cost(store, soar):
    out = ops_monitor.monitor_cases({}, {"view": "kpis"})["kpis"]
    assert out["rated"] == 0 and out["agreement"] is None
    assert out["cost_today_usd"] is None and out["handled"] == 0


# ---- M4: readiness maths at the boundaries --------------------------------

R = ops_monitor.rule_readiness


def test_readiness_sample_boundary():
    assert R(19, 0, 0, 0, 0, 0)["ready"] is False
    assert R(20, 0, 0, 0, 0, 0)["ready"] is True
    assert R(10, 0, 9, 0, 0, 0)["ready"] is False   # sample 19
    assert R(10, 0, 10, 0, 0, 0)["ready"] is True   # sample 20, right counts too
    assert "1 more reviewed decision " in R(19, 0, 0, 0, 0, 0)["blockers"][0]


def test_readiness_agreement_boundary_is_inclusive():
    at = R(19, 1, 0, 0, 0, 0)            # 19 / 20 = exactly 95%
    assert at["ready"] is True and at["agreement"] == 0.95
    below = R(18, 2, 0, 0, 0, 0)         # 90%
    assert below["ready"] is False and below["agreement"] == 0.9
    assert "90%" in below["blockers"][0] and "95%" in below["blockers"][0]
    # 94.99...: 36 of 38 is 94.7%
    assert R(36, 2, 0, 0, 0, 0)["ready"] is False
    # Feedback counts like decisions.
    assert R(0, 0, 19, 1, 0, 0)["ready"] is True
    assert R(0, 0, 18, 2, 0, 0)["ready"] is False


def test_readiness_reopen_rate_boundary_is_inclusive():
    ok = R(20, 0, 0, 0, 20, 1)           # 1 / 20 = exactly 5%
    assert ok["ready"] is True and ok["reopen_rate"] == 0.05
    bad = R(20, 0, 0, 0, 19, 1)          # 5.26%
    assert bad["ready"] is False and bad["reopen_rate"] == 0.0526
    assert "5%" in bad["blockers"][0]
    assert R(20, 0, 0, 0, 0, 0)["reopen_rate"] is None   # never acted: passes


def test_readiness_empty_and_inputs_and_thresholds():
    e = R(0, 0, 0, 0, 0, 0)
    assert e["ready"] is False and e["sample"] == 0 and e["agreement"] is None
    full = R(15, 1, 3, 1, 10, 0)
    assert full["inputs"] == {"approved": 15, "rejected": 1, "right": 3, "wrong": 1,
                              "acted": 10, "reopened": 0}
    assert full["sample"] == 20 and full["agreement"] == 0.9
    assert full["thresholds"] == {"min_sample": 20, "min_agreement": 0.95,
                                  "max_reopen_rate": 0.05}
    custom = R(5, 0, 0, 0, 0, 0, min_sample=5, min_agreement_pct=100)
    assert custom["ready"] is True and custom["thresholds"]["min_sample"] == 5


def shadow_card(store, sid, card_id, rule, human):
    store.save_card(sid, {"type": "approval_request", "approval_id": card_id,
                          "tool": "update_record",
                          "policy": {"rule": rule, "outcome": "would_act", "mode": "shadow"}})
    if human:
        store.claim_card_decision(sid, card_id, human, "analyst")


def test_policy_view_combines_the_shadow_matrix_feedback_and_reopens(world):
    sid = "auto:alerts:a-handled"
    for i in range(18):
        shadow_card(world, sid, f"s{i}", "close-false-positive", "approve")
    shadow_card(world, sid, "s-no", "close-false-positive", "reject")
    shadow_card(world, sid, "s-wait", "close-false-positive", None)
    rate(iri("a-handled"), "right", "alice")
    out = ops_monitor.monitor_cases({}, {"view": "policy"})
    assert out["view"] == "policy" and out["hours"] == 336 and out["mode"] == "shadow"
    rules = {r["rule_id"]: r for r in out["rules"]}
    fp = rules["close-false-positive"]
    assert fp["inputs"]["approved"] == 18 and fp["inputs"]["rejected"] == 1
    assert fp["inputs"]["right"] == 1 and fp["inputs"]["wrong"] == 0
    assert fp["sample"] == 20 and fp["agreement"] == 0.95
    # Enforced closes under this rule in the two-week window: a-handled,
    # f-undone (reopened), g-open-again (reopened live), l-lead and h-old
    # => 5 acted, 2 reopened.
    assert fp["inputs"]["acted"] == 5 and fp["inputs"]["reopened"] == 2
    assert fp["ready"] is False and fp["reopen_rate"] == 0.4
    assert fp["label"] == "Close false positive"
    assert out["thresholds"] == {"min_sample": 20, "min_agreement": 0.95,
                                 "max_reopen_rate": 0.05}


def test_policy_view_marks_a_clean_rule_ready(store, soar):
    sid = seed(store, "p1", verdict=("benign", 0.9))
    for i in range(20):
        shadow_card(store, sid, f"c{i}", "close-false-positive", "approve")
    rules = ops_monitor.monitor_cases({}, {"view": "policy"})["rules"]
    assert [(r["rule_id"], r["ready"], r["sample"], r["agreement"], r["blockers"])
            for r in rules] == [("close-false-positive", True, 20, 1.0, [])]


# ---- M5: tuning -----------------------------------------------------------

def test_tuning_flag_boundaries():
    T = ops_monitor.tuning_flag
    assert T(4, 0)["safe_to_tune"] is False and "4 of 5" in T(4, 0)["note"]
    assert T(5, 0)["safe_to_tune"] is True
    assert T(10, 1)["safe_to_tune"] is True       # exactly 10%
    assert T(9, 1)["safe_to_tune"] is False       # 11.1%
    assert T(10, 1)["reopen_rate"] == 0.1 and T(9, 1)["reopen_rate"] == 0.1111
    assert T(0, 0)["reopen_rate"] is None


def test_tuning_view_counts_stored_and_live_reopens_per_detection_rule(store, soar):
    # Beacon: 6 closes, none reopened.  Port scan: 5 closes, 1 undone, 1 open again.
    for i in range(6):
        seed(store, f"bk{i}", verdict=("benign", .9), tools=[SIEM],
             cards=[close_card(f"cb{i}", rule="Beacon")], executions=[f"cb{i}"])
        soar.records[iri(f"bk{i}")] = {"status": {"itemValue": "Closed"}}
    for i in range(5):
        seed(store, f"ps{i}", verdict=("benign", .9), tools=[SIEM],
             cards=[close_card(f"cp{i}", rule="Port scan")], executions=[f"cp{i}"],
             undone=["cp0"] if i == 0 else ())
        soar.records[iri(f"ps{i}")] = {"status": {
            "itemValue": "Investigating" if i == 1 else "Closed"}}
    out = ops_monitor.monitor_cases({}, {"view": "tuning"})
    rules = {(r["source"], r["rule"]): r for r in out["rules"]}
    beacon = rules[("FortiSIEM", "Beacon")]
    assert (beacon["count"], beacon["reopened"], beacon["reopen_rate"],
            beacon["safe_to_tune"]) == (6, 0, 0.0, True)
    scan = rules[("FortiSIEM", "Port scan")]
    assert (scan["count"], scan["reopened"], scan["reopen_rate"],
            scan["safe_to_tune"]) == (5, 2, 0.4, False)
    assert [r["rule"] for r in out["rules"]] == ["Beacon", "Port scan"]   # most closes first
    assert out["thresholds"] == {"min_closes": 5, "max_reopen_rate": 0.1}


# ---- registration ----------------------------------------------------------

def test_the_ops_are_registered_and_their_responses_validate(world):
    from connector_fsr_agentic_assistant import operations as ops
    out = ops.operations["monitor_cases"]({}, {"view": "list", "bucket": "handled"})
    assert out["total"] == 3
    fb = ops.operations["monitor_feedback"](
        {}, {"record": iri("a-handled"), "rating": "right", "actor": "alice"})
    assert fb["feedback"]["mine"] == "right"


# --- records deleted from SOAR drop out of the queue --------------------------

def _query_fake(soar, monkeypatch, present, *, shape="ok"):
    """Wrap the record-read fake: a POST to /api/query/alerts answers with only
    the `present` uuids (or a broken shape / an error)."""
    queries = []

    def mr(url, method, body=None, **kw):
        if method == "POST" and url.startswith("/api/query/alerts"):
            queries.append(body)
            if shape == "raise":
                raise RuntimeError("down")
            if shape == "odd":
                return {"unexpected": True}
            asked = body["filters"][0]["value"]
            return {"hydra:member": [{"uuid": u} for u in asked if u in present]}
        return soar(url, method, body, **kw)
    monkeypatch.setattr(ops_common, "_make_request", lambda: mr)
    return queries


def test_a_deleted_record_drops_out_of_every_bucket_and_the_counts(world, soar, monkeypatch):
    keys = set(by_key(cases()))
    gone = {"a-handled", "c-escalated", "g-open-again"}
    queries = _query_fake(soar, monkeypatch, present=keys - gone)
    out = cases()
    assert set(by_key(out)) == keys - gone
    assert len(queries) == 1                       # one existence query for the module
    assert sum(out["counts"].values()) == len(keys - gone)
    for bucket in ("handled", "needs_you", "reopened"):
        assert not gone & set(by_key(cases(bucket=bucket)))


def test_a_deleted_record_does_not_count_in_the_kpis(world, soar, monkeypatch):
    before = ops_monitor.monitor_cases({}, {"view": "kpis"})["kpis"]
    _query_fake(soar, monkeypatch, present=set(by_key(cases())) - {"c-escalated"})
    after = ops_monitor.monitor_cases({}, {"view": "kpis"})["kpis"]
    assert after["escalated"] == before["escalated"] - 1


@pytest.mark.parametrize("shape", ["raise", "odd"])
def test_an_unreadable_existence_query_hides_nothing(world, soar, monkeypatch, shape):
    keys = set(by_key(cases()))
    _query_fake(soar, monkeypatch, present=set(), shape=shape)
    assert set(by_key(cases())) == keys


# ---- X7: the would-help ranking ---------------------------------------------

def _helps(*entries):
    return list(entries)


def _nc(kind):
    return {"kind": kind, "state": "not_connected", "line": "x", "unknown": "u", "sources": []}


def _src(state, *labels):
    return {"kind": "source_failed" if state == "failed" else "data_gap", "state": state,
            "line": "x", "unknown": "u", "sources": list(labels)}


def seed_uncertain(store, key, helps, *, disposition="needs_more_info", age_h=1.0):
    sid = f"auto:alerts:{key}"
    assert store.claim_session(sid, ACTOR)
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE session_initiator SET created_at=? WHERE session_id=?",
                  (time.time() - age_h * HOUR, sid))
    card = {"type": "verdict_card", "disposition": disposition, "confidence": 0.4,
            "severity": "high", "summary": "s", "findings": []}
    if helps is not None:
        card["what_would_help"] = helps
    store.append_transcript_assistant(sid, [card])


def would_help(**params):
    return ops_monitor.monitor_cases({}, {"view": "would_help", **params})


def test_would_help_ranks_missing_and_failing_sources_by_uncertain_cases(store, soar):
    seed_uncertain(store, "u1", _helps(_nc("tickets"), _src("failed", "FortiSIEM")))
    seed_uncertain(store, "u2", _helps(_nc("tickets")))
    seed_uncertain(store, "u3", _helps(_nc("tickets"), _nc("intel")))
    seed_uncertain(store, "u4", _helps(_nc("intel"), _src("failed", "FortiSIEM", "FortiAnalyzer")))
    seed_uncertain(store, "u5", _helps(_src("no_data", "FortiAnalyzer")))
    seed_uncertain(store, "u6", [])                       # uncertain, nothing would help
    seed_uncertain(store, "u7", None)                     # an older card with no list
    # A confident verdict never counts, even when it carries entries.
    seed_uncertain(store, "c1", _helps(_nc("tickets")), disposition="benign")
    # Outside the window never counts.
    seed_uncertain(store, "old", _helps(_nc("tickets")), age_h=24 * 30)

    out = would_help()
    assert out["ok"] and out["view"] == "would_help" and out["hours"] == 336
    assert out["uncertain"] == 7 and out["with_help"] == 5
    got = [(i["label"], i["state"], i["cases"]) for i in out["items"]]
    assert got == [
        ("Ticket system", "not_connected", 3),
        ("Threat intelligence source", "not_connected", 2),
        ("FortiSIEM", "failed", 2),
        ("FortiAnalyzer", "failed", 1),
        ("FortiAnalyzer", "no_data", 1),
    ]
    first = out["items"][0]
    assert first["problem"] == "Ticket system is not connected" and first["kind"] == "tickets"
    assert out["items"][2]["problem"] == "FortiSIEM returned an error"
    assert out["items"][4]["problem"] == "FortiAnalyzer was searched and returned nothing"


def test_would_help_counts_a_case_once_per_source(store, soar):
    seed_uncertain(store, "u1", _helps(_nc("tickets"), _nc("tickets"),
                                       _src("failed", "FortiSIEM"),
                                       _src("failed", "FortiSIEM")))
    out = would_help()
    assert [(i["label"], i["cases"]) for i in out["items"]] == [
        ("Ticket system", 1), ("FortiSIEM", 1)]
    assert out["with_help"] == 1


def test_would_help_with_no_uncertain_cases_is_empty_not_an_error(store, soar):
    seed_uncertain(store, "b1", None, disposition="benign")
    out = would_help()
    assert out["ok"] and out["uncertain"] == 0 and out["with_help"] == 0 and out["items"] == []


def test_would_help_honours_the_hours_window(store, soar):
    seed_uncertain(store, "u1", _helps(_nc("logs")), age_h=30)
    assert would_help(hours=24)["uncertain"] == 0
    out = would_help(hours=48)
    assert out["uncertain"] == 1
    assert out["items"][0]["label"] == "Log source (SIEM or firewall logs)"


def test_would_help_reads_the_escalation_verdict_when_no_frames_are_stored(store, soar):
    seed(store, "e1", verdict=None, escalate={
        "reasons": ["r"], "verdict": {"disposition": "needs_more_info",
                                      "what_would_help": [_nc("intel")]}})
    out = would_help()
    assert out["uncertain"] == 1
    assert [(i["label"], i["cases"]) for i in out["items"]] == [("Threat intelligence source", 1)]
