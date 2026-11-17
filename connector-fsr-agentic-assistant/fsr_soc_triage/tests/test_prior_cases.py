"""prior_cases: cross-case memory over a fake SOAR query API.

The fake box evaluates the query body it is sent (the OR of indicator fields,
the uuid exclusion, the createDate sort, $limit) instead of replaying a canned
answer, so a wrong field name or a missing exclusion in the tool shows up as a
wrong result here, not as a green test.
"""
from __future__ import annotations

import re

import pytest
from fsr_playbooks.mcp_server import _shared

from fsr_soc_triage import tools_prior_cases as pc
from fsr_soc_triage.registry import REGISTRY


def _pl(label):
    return {"@id": f"/api/3/picklists/{label}", "itemValue": label}


def _alert(uuid, name, created, **kw):
    return {"@id": f"/api/3/alerts/{uuid}", "uuid": uuid, "name": name,
            "createDate": created, "status": _pl(kw.pop("status", "Closed")),
            "severity": _pl(kw.pop("severity", "Medium")), **kw}


def _incident(uuid, name, created, **kw):
    return {"@id": f"/api/3/incidents/{uuid}", "uuid": uuid, "name": name,
            "createDate": created, "status": _pl(kw.pop("status", "Resolved")),
            "severity": _pl(kw.pop("severity", "High")), **kw}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeBox:
    base_url = "https://soar.example.com"

    def __init__(self, alerts=(), incidents=()):
        self.data = {"alerts": list(alerts), "incidents": list(incidents)}
        self.calls = []

    def post(self, url, json):
        module = re.search(r"/api/query/(\w+)", url).group(1)
        limit = int(re.search(r"\$limit=(\d+)", url).group(1))
        self.calls.append({"module": module, "body": json, "limit": limit})
        ors = json["filters"][0]["filters"]
        neq = [c["value"] for c in json["filters"][1:] if c["operator"] == "neq"]
        rows = [r for r in self.data[module]
                if any(str(r.get(c["field"])) == c["value"] for c in ors)
                and r["uuid"] not in neq]
        rows.sort(key=lambda r: r["createDate"], reverse=json["sort"][0]["direction"] == "DESC")
        return _Resp({"hydra:member": rows[:limit], "hydra:totalItems": len(rows)})


@pytest.fixture
def box(monkeypatch):
    def install(alerts=(), incidents=(), current_iri=None):
        b = _FakeBox(alerts, incidents)
        monkeypatch.setattr(_shared, "_live_client", lambda: b)
        monkeypatch.setattr(pc, "_request", lambda m, c, url, **kw: getattr(c, m)(url, **kw))
        monkeypatch.setattr(_shared, "active_record_iri", lambda: current_iri)
        return b
    return install


def test_match_found_carries_the_verdict_fields_as_labels(box):
    box([_alert("a1", "Port scan from vendor", 1_700_000_000, sourceIp="203.0.113.7",
                closureReason=_pl("False Positive"),
                closureNotes="<p>Authorised  vendor scanner.</p>")],
        [_incident("i1", "Scanner ticket", 1_690_000_000, sourceIP="203.0.113.7",
                   resolution="<p>Known scanner</p>")])
    out = pc.prior_cases("203.0.113.7")
    assert out["ok"] and out["count"] == 2 and "capped" not in out
    a, i = out["cases"]  # newest first: the alert, then the incident
    assert a["kind"] == "alert" and a["name"] == "Port scan from vendor"
    assert a["status"] == "Closed" and a["closed_as"] == "False Positive"
    assert a["closure_notes"] == "Authorised vendor scanner."
    assert a["severity"] == "Medium" and a["date"] == "2023-11-14"
    assert a["matched_as"] == "source IP"
    assert i["kind"] == "incident" and i["closure_notes"] == "Known scanner"
    assert i["severity"] == "High"
    assert "closed_as" not in i
    # plain values, not picklist dicts or hydra noise
    assert all(isinstance(v, str) for c in out["cases"] for v in c.values())


def test_current_record_is_excluded_and_asked_to_be(box):
    b = box([_alert("cur", "This alert", 1_700_000_100, sourceIp="203.0.113.7"),
             _alert("old", "Earlier alert", 1_700_000_000, sourceIp="203.0.113.7")],
            current_iri="/api/3/alerts/cur")
    out = pc.prior_cases("203.0.113.7")
    assert [c["name"] for c in out["cases"]] == ["Earlier alert"]
    assert out["count"] == 1
    assert any(c["operator"] == "neq" and c["value"] == "cur"
               for call in b.calls for c in call["body"]["filters"][1:])


def test_current_record_dropped_even_if_the_server_ignores_the_exclusion(box, monkeypatch):
    b = box([_alert("cur", "This alert", 1_700_000_100, sourceIp="203.0.113.7"),
             _alert("old", "Earlier alert", 1_700_000_000, sourceIp="203.0.113.7")],
            current_iri="/api/3/alerts/cur")
    real = b.post
    monkeypatch.setattr(b, "post", lambda url, json: real(
        url, {**json, "filters": json["filters"][:1]}))
    out = pc.prior_cases("203.0.113.7")
    assert [c["name"] for c in out["cases"]] == ["Earlier alert"]
    assert out["count"] == 1


def test_cap_is_ten_newest_first_and_says_so(box):
    rows = [_alert(f"a{n}", f"Alert {n}", 1_700_000_000 + n, sourceIp="198.51.100.9")
            for n in range(25)]
    box(rows)
    out = pc.prior_cases("198.51.100.9", type="ip")
    assert len(out["cases"]) == 10 and out["count"] == 25 and out["capped"] is True
    assert out["cases"][0]["name"] == "Alert 24" and out["cases"][-1]["name"] == "Alert 15"
    assert "10 most recent of 25" in out["note"]


def test_cap_merges_both_modules_by_date(box):
    box([_alert(f"a{n}", f"Alert {n}", 1_700_000_000 + 2 * n, sourceIp="198.51.100.9")
         for n in range(8)],
        [_incident(f"i{n}", f"Incident {n}", 1_700_000_001 + 2 * n, sourceIP="198.51.100.9")
         for n in range(8)])
    out = pc.prior_cases("198.51.100.9")
    assert out["count"] == 16 and len(out["cases"]) == 10
    assert [c["name"] for c in out["cases"][:3]] == ["Incident 7", "Alert 7", "Incident 6"]


def test_no_match_says_there_is_no_prior_verdict(box):
    box([_alert("a1", "Other", 1_700_000_000, sourceIp="192.0.2.1")])
    out = pc.prior_cases("203.0.113.7")
    assert out["ok"] and out["count"] == 0 and out["cases"] == []
    assert "no prior verdict" in out["note"]


@pytest.mark.parametrize("value,kind,alert_field,incident_field,role", [
    ("203.0.113.7", "ip", "destinationIp", "destinationIP", "destination IP"),
    ("WS-042", "host", "computerName", "affectedHost", "host"),
    ("jdoe", "user", "userName", "affectedUser", "user"),
    ("d41d8cd98f00b204e9800998ecf8427e", "hash", "fileHash", "filehash", "file hash"),
    ("evil.example.org", "domain", "senderDomain", "senderDomain", "sender domain"),
])
def test_each_indicator_type_hits_its_own_fields(box, value, kind, alert_field,
                                                  incident_field, role):
    box([_alert("a1", "A", 1_700_000_000, **{alert_field: value}),
         _alert("a2", "Unrelated", 1_700_000_001, sourceIp="192.0.2.1")],
        [_incident("i1", "I", 1_690_000_000, **{incident_field: value})])
    out = pc.prior_cases(value, type=kind)
    assert [c["name"] for c in out["cases"]] == ["A", "I"], out
    assert out["cases"][0]["matched_as"] == role
    assert out["searched_as"] == [kind]


def test_url_type_searches_alerts_only(box):
    b = box([_alert("a1", "Phish", 1_700_000_000, url="http://bad.example.org/x")])
    out = pc.prior_cases("http://bad.example.org/x")
    assert out["searched_as"] == ["url"] and out["count"] == 1
    assert [c["module"] for c in b.calls] == ["alerts"]


@pytest.mark.parametrize("value,kinds", [
    ("203.0.113.7", ["ip"]),
    ("2001:db8::1", ["ip"]),
    ("d41d8cd98f00b204e9800998ecf8427e", ["hash"]),
    ("http://bad.example.org/x", ["url"]),
    ("evil.example.org", ["host", "domain"]),
    ("jdoe", ["host", "user"]),
])
def test_type_is_detected_when_not_given(box, value, kinds):
    box()
    assert pc.prior_cases(value)["searched_as"] == kinds


def test_bad_type_and_empty_value_are_refused(box):
    box()
    assert pc.prior_cases("x", type="banana")["code"] == "bad_type"
    assert pc.prior_cases("  ")["code"] == "missing_value"


def test_one_module_failing_is_reported_but_the_other_still_answers(box, monkeypatch):
    b = box([_alert("a1", "A", 1_700_000_000, sourceIp="203.0.113.7")])
    real = b.post

    def post(url, json):
        if "/incidents" in url:
            r = _Resp({})
            r.status_code = 500
            return r
        return real(url, json)

    monkeypatch.setattr(b, "post", post)
    out = pc.prior_cases("203.0.113.7")
    assert out["ok"] and out["count"] == 1 and "incidents search returned HTTP 500" in out["partial"]


def test_no_box_is_a_clear_partial(monkeypatch):
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    out = pc.prior_cases("203.0.113.7")
    assert not out.get("ok") or out.get("partial")


def test_registered_as_a_read_only_triage_tool():
    import fsr_soc_triage  # noqa: F401 - registers on import
    from fsr_playbooks.llm.tools import SAFE_TOOLS, TOOL_TIERS
    spec = REGISTRY["prior_cases"]
    assert spec.tier == 1 and TOOL_TIERS["prior_cases"] == 1 and "prior_cases" in SAFE_TOOLS
    assert set(spec.input_schema["properties"]) == {"value", "type"}
    assert spec.input_schema["required"] == ["value"]
