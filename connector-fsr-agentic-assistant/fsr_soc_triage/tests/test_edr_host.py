"""edr_host: one call for "is this host known to FortiEDR, and what else has it raised".

Live (GA rehearsal), the model took ten rounds of generic FortiEDR ops to learn
this, one of them refused over the date format. These drive the tool against
connector responses shaped like the live ones (collector rows carry kilobytes of
systemInformation; a long event list arrives already digested) and check the
calls it makes as well as what it returns.
"""
from __future__ import annotations

import re

import fsr_playbooks.execution_api as exec_api
import pytest
from fsr_playbooks.mcp_server import tools_edr

_FLAME = {"id": 672233, "name": "The-Flame", "ipAddress": "192.0.2.39",
          "state": "Disconnected", "lastSeenTime": "2026-09-15 13:17",
          "loggedUsers": ["THE-FLAME\\jlocke"], "operatingSystem": "Windows 10 Enterprise",
          "organization": "Lab Org", "macAddresses": ["00-00-5E-00-53-01"],
          "systemInformation": {"CPU": {"Cache": [{"Level": "L1"}] * 40}}}
_OTHER = {"id": 1, "name": "DC-01", "ipAddress": "192.0.2.10", "state": "Running"}
_DIGEST = {"_digest": "event_list", "count": 60,
           "time_window": {"field": "firstSeen", "min": "2026-03-30 09:17:49",
                           "max": "2026-09-15 12:45:00"},
           "facets": {"action": {"top": [{"value": "SimulationBlock", "count": 56}]}},
           "samples": [{"eventId": n, "process": "powershell.exe"} for n in range(12)]}
_EDR_DT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z$")


@pytest.fixture
def edr(monkeypatch):
    calls: list[dict] = []
    replies = {"get_collector_list": lambda p: (
                   {"ok": True, "data": [r for r in (_FLAME, _OTHER)
                                         if r["ipAddress"] == p["ips"]]}
                   if "ips" in p else {"ok": True, "data": [_FLAME, _OTHER]}),
               "get_event_list_extended": lambda p: {"ok": True, "data": _DIGEST}}

    def fake_run_op(connector, op, params):
        calls.append({"connector": connector, "op": op, "params": dict(params)})
        return replies[op](params)

    monkeypatch.setattr(exec_api, "run_op", fake_run_op)
    monkeypatch.setattr(tools_edr, "_edr_connector", lambda: "fortinet-fortiedr")
    return calls, replies


def test_by_ip_finds_the_collector_then_its_events(edr):
    calls, _ = edr
    out = tools_edr.edr_host(ip="192.0.2.39")
    assert [c["op"] for c in calls] == ["get_collector_list", "get_event_list_extended"]
    assert calls[0]["params"] == {"ips": "192.0.2.39"}
    ev = calls[1]["params"]
    assert ev["device"] == "The-Flame", "events must be asked for by the collector's name"
    assert _EDR_TIME_FMT_OK(ev["lastSeenFrom"]) and _EDR_TIME_FMT_OK(ev["lastSeenTo"])
    assert ev["lastSeenFrom"] < ev["lastSeenTo"]

    assert out["ok"] and out["known_to_edr"]
    [c] = out["collectors"]
    assert c["name"] == "The-Flame" and c["state"] == "Disconnected"
    assert c["loggedUsers"] == ["THE-FLAME\\jlocke"]
    assert "systemInformation" not in c
    assert out["events"]["count"] == 60
    assert out["events"]["facets"]["action"]["top"][0]["value"] == "SimulationBlock"
    assert out["events"]["time_window"]["max"] == "2026-09-15 12:45:00"
    assert len(out["events"]["samples"]) == 10


def _EDR_TIME_FMT_OK(v: str) -> bool:
    return bool(_EDR_DT.match(v))


def test_by_name_matches_the_roster_case_insensitively(edr):
    calls, _ = edr
    out = tools_edr.edr_host(host="the-flame")
    assert "ips" not in calls[0]["params"], "the collector list cannot filter by name"
    assert [c["name"] for c in out["collectors"]] == ["The-Flame"]
    assert calls[1]["params"]["device"] == "The-Flame"


def test_an_unknown_name_is_said_plainly(edr):
    calls, _ = edr
    out = tools_edr.edr_host(host="WIN-RDP-04")
    assert out["ok"] and out["known_to_edr"] is False and out["collectors"] == []
    # Still asks the events by the given name: FortiEDR events outlive collectors.
    assert calls[-1]["params"]["device"] == "WIN-RDP-04"


def test_an_ip_match_under_another_name_is_flagged(edr):
    out = tools_edr.edr_host(host="old-name", ip="192.0.2.39")
    assert [c["name"] for c in out["collectors"]] == ["The-Flame"]
    assert "old-name" in out["note"] and "192.0.2.39" in out["note"]


def test_window_sets_the_lookback(edr):
    calls, _ = edr
    tools_edr.edr_host(host="The-Flame", window="2w")
    from datetime import datetime
    p = calls[-1]["params"]
    span = (datetime.strptime(p["lastSeenTo"], "%Y-%m-%dT%H:%M:%S.000Z")
            - datetime.strptime(p["lastSeenFrom"], "%Y-%m-%dT%H:%M:%S.000Z"))
    assert span.days == 14


def test_nothing_to_look_up_is_refused_without_a_call(edr):
    calls, _ = edr
    out = tools_edr.edr_host()
    assert out["ok"] is False and out["code"] == "missing_host"
    assert calls == []


def test_a_failed_collector_lookup_keeps_the_connectors_reason(edr):
    _, replies = edr
    replies["get_collector_list"] = lambda p: {
        "ok": False, "code": "op_request_rejected", "message": "HTTP 401: bad token"}
    out = tools_edr.edr_host(host="The-Flame")
    assert out["ok"] is False and out["step"] == "collector"
    assert out["code"] == "op_request_rejected" and "401" in out["message"]


def test_edr_host_is_advertised_and_gated_with_its_source():
    from fsr_playbooks.llm.hunt_tools import HUNT_TOOLS as _TRIAGE_TOOL_NAMES
    from fsr_soc_triage.source_availability import family_of
    from fsr_soc_triage.triage_sources import canonical_source, toolset_for
    assert "edr_host" in _TRIAGE_TOOL_NAMES
    assert family_of("edr_host") == "fortiedr"
    assert canonical_source("FortiEDR") == "fortiedr"
    assert "edr_host(" in toolset_for("fortinet-fortiedr")["host"]
