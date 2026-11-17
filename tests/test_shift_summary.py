"""Shift summary (X6): the digest numbers, by value, from a seeded store.

The store is the real `Storage` on a temp file, seeded with auto-triage
sessions (reusing the Monitor test seeding) and hunt runs. Nothing runs a
model; the digest and its text are checked against the counts the seed implies.
"""
from __future__ import annotations

import time

import pytest

import ops_shift
from tests.test_monitor_cases import (  # noqa: F401
    EDR, INTEL, SIEM, block_card, close_card, iri, seed, soar, store,
)

HOUR = 3600.0


def run(store, name, outcome, age_h, *, summary=None, case=None, disposition=None,
        severity=None):
    store.record_hunt_run({
        "hunt_name": name, "session_id": f"hunt:{name}:{outcome}{age_h}",
        "started_at": time.time() - age_h * HOUR,
        "finished_at": time.time() - age_h * HOUR + 30, "outcome": outcome,
        "disposition": disposition, "severity": severity, "summary": summary,
        "case_iri": case, "case_action": "opened" if case else "none"})


@pytest.fixture
def shift(store, soar):
    # In the 8 hour window: 6 cases (one a storm lead with 2 members).
    seed(store, "a-closed", age_h=1, verdict=("benign", 0.88), name="Login burst",
         tools=[SIEM, INTEL], cards=[close_card("ap-a")], executions=["ap-a"])
    seed(store, "b-blocked", age_h=2, verdict=("true_positive", 0.92),
         tools=[SIEM, EDR], cards=[block_card("act-b")])
    seed(store, "c-uncertain", age_h=3, name="Phish report",
         verdict=("needs_more_info", None), tools=[("faz_search", {})],
         escalate={"reasons": ["The verdict needs review: needs more information."],
                   "verdict": {"disposition": "needs_more_info"}})
    store.append_transcript_assistant("auto:alerts:c-uncertain", [{
        "type": "verdict_card", "disposition": "needs_more_info", "severity": "high",
        "summary": "s", "findings": [],
        "what_would_help": [{"state": "not_connected", "kind": "intel"},
                            {"state": "failed", "kind": "logs", "sources": ["FortiSIEM"]}]}])
    seed(store, "d-shadow", age_h=4, verdict=("benign", 0.81), tools=[INTEL],
         cards=[{"type": "approval_request", "approval_id": "ap-d", "tool": "update_record",
                 "policy": {"rule": "close-false-positive", "outcome": "would_act",
                            "mode": "shadow"}}])
    seed(store, "e-suspicious", age_h=5, verdict=("suspicious", 0.6), tools=[SIEM],
         name="Odd login")
    # The oldest waiting case is the most urgent: P1 outranks the newer P2.
    store.save_card("auto:alerts:e-suspicious", {
        "type": "escalation_card", "id": "esc_e-suspicious",
        "record": {"name": "Odd login", "module": "alerts", "iri": iri("e-suspicious")},
        "verdict": {"disposition": "suspicious"}, "why": ["needs_review"],
        "reasons": ["The verdict needs review: suspicious, 60% confident."],
        "waiting": 0, "priority": "P1", "priority_rank": 1})
    seed(store, "l-lead", age_h=0.5, verdict=("benign", 0.9), tools=[SIEM],
         cards=[close_card("ap-l")], executions=["ap-l"])
    now = time.time()
    store.route_triage(iri("l-lead"), "auto:alerts:l-lead", ["host=h1|x"], 900, 0, 3600, now)
    for m in ("m1", "m2"):
        assert store.route_triage(iri(m), f"auto:alerts:{m}", ["host=h1|x"], 900, 0, 3600,
                                  now)["role"] == "member"
    # One deferred by the storm cap, in the window.
    assert store.route_triage(iri("z"), "auto:alerts:z", ["host=other"], 900, 1, 3600,
                              now)["role"] == "deferred"
    # Outside the window: a day and a half old.
    seed(store, "h-old", age_h=36, verdict=("true_positive", 0.9), tools=[SIEM])
    soar.records = {iri(k): {"name": n, "status": {"itemValue": "Open"}} for k, n in [
        ("a-closed", "Login burst"), ("b-blocked", "Outbound C2"),
        ("c-uncertain", "Phish report"), ("d-shadow", "New CDN host"),
        ("e-suspicious", "Odd login"), ("l-lead", "Beacon storm")]}
    soar.records[iri("a-closed")]["status"] = {"itemValue": "Closed"}
    soar.records[iri("l-lead")]["status"] = {"itemValue": "Closed"}
    # Hunt runs: in window 2 findings, 3 quiet, 1 inconclusive, 1 failed; 1 old.
    run(store, "c2-sweep", "finding", 1, summary="h1 beaconed to a known C2",
        case="/api/3/alerts/h1", disposition="true_positive", severity="high")
    run(store, "dns-tunnel", "finding", 2, summary="long TXT queries from h7",
        case="/api/3/alerts/h7", disposition="suspicious", severity="medium")
    for age in (3, 4, 6):
        run(store, "c2-sweep", "quiet", age, disposition="benign")
    run(store, "dns-tunnel", "inconclusive", 5, disposition="needs_more_info")
    run(store, "dns-tunnel", "error", 7)
    run(store, "c2-sweep", "finding", 40, summary="old", case="/api/3/alerts/old")
    return store


def digest(**params):
    out = ops_shift.shift_summary({}, params)
    assert out["ok"], out
    return out


def test_what_the_assistant_handled_by_value(shift):
    h = digest()["handled"]
    assert h == {"triaged": 6, "closed": 2, "handled": 3, "sent_to_you": 2,
                 "grouped": 2, "deferred": 1, "would_have_acted": 1, "reopened": 0}


def test_what_waits_for_a_human_is_the_needs_you_queue_in_urgency_order(shift):
    w = digest()["waiting"]
    # c and e (sent to you) and d (would have closed): the M1 needs_you set.
    assert w["total"] == 3
    # Urgency first (P1 before P2), then the case nothing escalated, each with its why.
    assert [(c["record_name"], c["priority"]) for c in w["cases"]] == [
        ("Odd login", "P1"), ("Phish report", "P2"), ("New CDN host", None)]
    assert w["cases"][1]["reasons"] == ["The verdict needs review: needs more information."]


def test_notable_verdicts_are_confirmed_then_suspicious_by_confidence(shift):
    n = digest()["notable"]
    assert n["total"] == 2
    assert [(c["record_name"], c["verdict"], c["confidence"]) for c in n["cases"]] == [
        ("Outbound C2", "True positive", 0.92), ("Odd login", "Suspicious", 0.6)]


def test_the_would_help_items_come_from_the_uncertain_cases_in_the_window(shift):
    wh = digest()["would_help"]
    assert (wh["uncertain"], wh["with_help"]) == (1, 1)
    assert sorted((i["label"], i["cases"]) for i in wh["items"]) == [
        ("FortiSIEM", 1), ("Threat intelligence source", 1)]
    assert {i["problem"] for i in wh["items"]} == {
        "Threat intelligence source is not connected", "FortiSIEM returned an error"}


def test_hunt_findings_are_counted_and_listed_inside_the_window_only(shift):
    hu = digest()["hunts"]
    assert (hu["runs"], hu["findings"], hu["quiet"], hu["inconclusive"], hu["failed"]) == (
        7, 2, 3, 1, 1)
    assert [(f["hunt"], f["summary"], f["case_iri"]) for f in hu["cases"]] == [
        ("c2-sweep", "h1 beaconed to a known C2", "/api/3/alerts/h1"),
        ("dns-tunnel", "long TXT queries from h7", "/api/3/alerts/h7")]


def test_the_window_picker_moves_the_digest(shift):
    # A 2.5 hour window: only the cases and hunt runs started inside it.
    d = digest(hours=2.5)
    assert d["handled"]["triaged"] == 3                      # l-lead, a, b
    assert d["hunts"]["runs"] == 2 and d["hunts"]["findings"] == 2
    assert d["waiting"]["total"] == 0
    # A window that ended two hours ago excludes the newer cases.
    d = digest(hours=4, until=time.time() - 2.2 * HOUR)
    assert d["handled"]["triaged"] == 3                      # c (3h), d (4h), e (5h)
    assert (d["handled"]["closed"], d["handled"]["handled"], d["waiting"]["total"]) == (0, 0, 3)
    assert d["hours"] == 4
    # A week reaches the old case and the old hunt run.
    d = digest(hours=24 * 7)
    assert d["handled"]["triaged"] == 7 and d["hunts"]["findings"] == 3


def test_the_text_carries_the_same_numbers_and_stays_plain(shift):
    d = digest()
    text = d["text"]
    for line in ("Triaged 6 cases: 3 handled, 2 closed for you, 2 sent to you.",
                 "Grouped 2 related alerts under a lead case; 1 held back by the storm cap.",
                 "Waiting for you (3)", "- P1 Odd login - The verdict needs review: suspicious", "- P2 Phish report - The verdict needs review",
                 "Notable verdicts (2)", "- Outbound C2: True positive, 92% confident",
                 "- Threat intelligence source is not connected: would have helped 1 uncertain case.",
                 "7 runs: 2 found something, 3 quiet, 1 inconclusive, 1 failed.",
                 "- c2-sweep: h1 beaconed to a known C2"):
        assert line in text, line
    assert d["subject"] in text
    assert all(ch not in text for ch in "–—")
    assert text.isascii()


def test_an_empty_store_reads_as_a_quiet_shift(store, soar):
    d = digest()
    assert d["handled"]["triaged"] == 0 and d["hunts"]["runs"] == 0
    assert "Nothing is waiting on you." in d["text"]
    assert "No uncertain cases." in d["text"]


def test_a_bad_window_is_refused(store, soar):
    assert ops_shift.shift_summary({}, {"hours": 100000})["error"]["code"] == "bad_window"
