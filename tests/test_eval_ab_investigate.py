"""A2 investigation rows of the A/B (scripts/eval_ab_investigate.py, world_server.py).

The rows only mean something if (1) the outside agent is served the SAME recorded
evidence the widget row mounts, (2) both sides are normalised into one record and
graded by one function, (3) that function fails a right verdict that is uncited or
not grounded in the deciding evidence. Each test builds an input with a known
answer. No model, no box (the world is a loopback server over a fixture bundle).
"""
from __future__ import annotations

import json
import sys
import urllib.request
import ssl
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import eval_ab  # noqa: E402
import eval_ab_investigate as inv  # noqa: E402
from scripts.world_server import WorldServer  # noqa: E402

C2 = inv.load_task("ka_c2_beacon")
UUID = C2["mount"]["uuid"]


def _get(base, path):
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(base + path, context=ctx) as r:
        return json.loads(r.read())


def _post(base, path, body):
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ctx) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def call(name, args, result, ok=True, cid=None):
    return inv.Call(name=name, args=args, result=result, ok=ok, id=cid or name)


# --- the world ------------------------------------------------------------
def test_world_serves_the_mounted_record_and_the_recorded_op():
    with WorldServer("ka_c2_beacon") as w:
        rec = _get(w.base_url, f"/api/3/alerts/{UUID}")
        assert rec["destinationIp"] == "198.51.100.77"
        _s, got = _post(w.base_url, "/api/integration/execute/", {
            "connector": "virustotal", "operation": "query_ip",
            "params": {"ip": "198.51.100.77"}})
        tags = got["data"]["data"]["attributes"]["tags"]
        assert "cobalt-strike" in tags and got["status"] == "Success"


def test_world_reports_an_unrecorded_op_as_a_miss_not_a_default_answer():
    """strict_ops carries over: an op the world did not record fails and is listed."""
    with WorldServer("ka_c2_beacon") as w:
        _s, got = _post(w.base_url, "/api/integration/execute/", {
            "connector": "virustotal", "operation": "query_ip",
            "params": {"ip": "203.0.113.200"}})
        assert got["status"] == "Failed" and got["data"] is None
        assert any("virustotal.query_ip" in m for m in w.misses)


def test_world_refuses_writes():
    with WorldServer("ka_c2_beacon") as w:
        status, _ = _post(w.base_url, "/api/3/alerts", {"name": "x"})
        assert status == 599
        assert any("POST /api/3/alerts" in m for m in w.misses)


# --- the grader -----------------------------------------------------------
def inv_of(disposition, findings, calls):
    return inv.Investigation(disposition=disposition, findings=findings, calls=calls)


GOOD_CALLS = [call("get_record", {"ref": "x"}, '{"name":"alert"}'),
              call("run_connector_operation", {"ip": "198.51.100.77"},
                   '{"tags":["cobalt-strike"]}')]


def test_right_cited_grounded_verdict_passes():
    g = inv.grade_investigation(inv_of("true_positive", [("c2", [1])], GOOD_CALLS), C2)
    assert g["pass"] and g["correct"] and g["cited"] and g["grounded"]


def test_wrong_verdict_fails_even_when_cited():
    g = inv.grade_investigation(inv_of("false_positive", [("c2", [1])], GOOD_CALLS), C2)
    assert g["cited"] and g["grounded"] and not g["correct"] and not g["pass"]


def test_right_verdict_with_an_uncited_finding_fails():
    g = inv.grade_investigation(
        inv_of("true_positive", [("c2", [1]), ("invented", [])], GOOD_CALLS), C2)
    assert g["correct"] and not g["cited"] and not g["pass"]


def test_citing_a_failed_call_is_not_citing():
    calls = [call("run_connector_operation", {}, '{"error":"x"}', ok=False)]
    g = inv.grade_investigation(inv_of("true_positive", [("c2", [0])], calls), C2)
    assert not g["cited"] and not g["pass"]


def test_citing_a_card_is_not_citing_evidence():
    calls = [call("emit_card", {}, '{"cobalt-strike": 1}')]
    g = inv.grade_investigation(inv_of("true_positive", [("c2", [0])], calls), C2)
    assert not g["cited"]


def test_right_verdict_not_grounded_in_the_deciding_evidence_fails():
    """Cited, correct, but only the record was read: the verdict was a guess."""
    calls = [call("get_record", {}, '{"name":"Outbound connection"}')]
    g = inv.grade_investigation(inv_of("true_positive", [("beacon", [0])], calls), C2)
    assert g["correct"] and g["cited"] and not g["grounded"] and not g["pass"]
    assert g["why"]["deciding_missing"] == ["cobalt-strike"]


def test_no_verdict_fails_everything():
    g = inv.grade_investigation(inv_of(None, [], GOOD_CALLS), C2)
    assert not any(g[k] for k in ("delivered", "correct", "cited", "grounded", "pass"))


@pytest.mark.parametrize("task_id", inv.INVESTIGATION_TASKS)
def test_every_task_has_a_known_answer_and_deciding_evidence(task_id):
    t = inv.load_task(task_id)
    assert t["any_of"] and t["deciding"] and t["bundle"]


# --- the two normalisers --------------------------------------------------
def test_widget_frames_resolve_cited_ids_to_calls():
    frames = [
        {"type": "tool_use", "id": "a", "name": "get_record", "input": {}},
        {"type": "tool_result", "tool_use_id": "a", "content": {"name": "alert"}},
        {"type": "tool_use", "id": "b", "name": "run_op", "input": {"ip": "1"}},
        {"type": "tool_result", "tool_use_id": "b", "content": {"ok": True, "tags": ["cobalt-strike"]}},
        {"type": "tool_use", "id": "c", "name": "run_op", "input": {}},
        {"type": "tool_result", "tool_use_id": "c", "content": {"ok": False}},
        {"type": "verdict_card", "disposition": "true_positive", "summary": "s",
         "findings": [{"claim": "c2", "evidence": ["b", "nope"]}]},
    ]
    i = inv.widget_investigation(frames)
    assert i.disposition == "true_positive"
    assert i.findings == [("c2", [1])] and i.unresolved == 1
    assert i.calls[2].ok is False
    assert inv.grade_investigation(i, C2)["pass"]


def test_widget_without_a_verdict_card_delivers_nothing():
    assert inv.widget_investigation([{"type": "text", "text": "hi"}]).disposition is None


def _events(calls, final):
    ev = []
    for n, (name, args, text) in enumerate(calls):
        ev.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{n}", "name": f"mcp__fsrtk__{name}", "input": args}]}})
        ev.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{n}",
             "content": [{"type": "text", "text": text}]}]}})
    ev.append({"type": "result", "result": final})
    return ev


def fence(d):
    return "```json\n" + json.dumps(d) + "\n```"


def test_outside_citations_resolve_to_the_call_naming_the_most_arguments():
    """Two lookups share connector and operation; the citation names one IP."""
    ev = _events([
        ("run_connector_operation",
         {"connector": "virustotal", "operation": "query_ip", "params": {"ip": "198.51.100.77"}},
         '{"tags":["cobalt-strike"]}'),
        ("run_connector_operation",
         {"connector": "virustotal", "operation": "query_ip", "params": {"ip": "10.20.1.15"}},
         '{"data":{"error":"private address"}}'),
    ], fence({"disposition": "true_positive", "summary": "s", "findings": [
        {"claim": "c2", "evidence": ["run_connector_operation virustotal query_ip 198.51.100.77"]}]}))
    i = inv.outside_investigation(ev)
    assert i.findings == [("c2", [0])]
    assert inv.grade_investigation(i, C2)["pass"]


def test_outside_error_envelope_is_not_evidence():
    ev = _events([("get_record", {"ref": "r"}, '{"error": {"type": "NotFound"}}')],
                 fence({"disposition": "true_positive", "findings": [
                     {"claim": "x", "evidence": ["get_record r"]}]}))
    i = inv.outside_investigation(ev)
    assert i.calls[0].ok is False
    assert not inv.grade_investigation(i, C2)["cited"]


def test_outside_unparseable_final_answer_delivers_nothing():
    ev = _events([("get_record", {}, "{}")], "I think it is malicious.")
    assert inv.outside_investigation(ev).disposition is None


def test_outside_agent_loses_writers_but_keeps_the_lookup_tool():
    deny = inv.outside_deny(eval_ab.mutating_tools())
    assert "run_connector_operation" not in deny
    assert {"update_record", "create_record", "run_fortiai_investigation"} <= set(deny)


# --- plumbing -------------------------------------------------------------
def test_table_has_investigation_columns_and_regrades_from_storage(tmp_path):
    ev = _events([("run_connector_operation", {"ip": "198.51.100.77"},
                   '{"tags":["cobalt-strike"]}')],
                 fence({"disposition": "true_positive", "findings": [
                     {"claim": "c2", "evidence": ["run_connector_operation 198.51.100.77"]}]}))
    eval_ab.store(tmp_path, "outside", C2["id"], 1, ev, {"wall_s": 1.0})
    rows = eval_ab.regrade(tmp_path, [C2])
    assert len(rows) == 1 and rows[0]["pass"] is True and rows[0]["kind"] == "investigate"
    md = eval_ab.render_all(rows, {"model": "m", "runs": 1})
    assert "| correct | cited | grounded | pass |" in md
    assert "| outside | ka_c2_beacon | 1 | y | y | y | y | y |" in md
