"""Phase 2 step 4 (shadow): two more trap worlds for the start-of-turn record read.

* ``ka_trap_approval_no_window``: an approved change names the host and the
  rated address but states no window, so it covers the alert only by omission.
* ``ka_trap_jira_change_row``: the change record comes from a Jira connector
  (its search body, ``fields`` nesting, status object and rich-text description),
  not ServiceNow.
The read must bind both worlds' records and the extractors must read them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402
from tests.test_chat_operations import tmp_db  # noqa: E402,F401

SWEEP = _ROOT / "scripts" / "conversations" / "sweep"
_es = pytest.importorskip("fsr_playbooks.mcp_server._evidence_score")
cv = pytest.importorskip("fsr_playbooks.mcp_server._citation_validator")
if not hasattr(cv, "set_turn_preread_records"):
    pytest.skip("the framework predates the start-of-turn record read", allow_module_level=True)

WORLDS = {
    "ka_trap_approval_no_window": ("6b1d0a10-2f3e-4a51-8c01-aa0000000207", "fin-recon-04",
                                   "servicenow", "CHG-7201"),
    "ka_trap_jira_change_row": ("6b1d0a10-2f3e-4a51-8c01-aa0000000208", "lab-eval-15",
                                "jirav2", "CHG-42"),
}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _serve(monkeypatch, row):
    from fsr_playbooks.mcp_server import _shared, tools_execution
    from fsr_soc_triage import source_availability, triage_preflight
    uuid, _host, connector, _num = WORLDS[row]
    bundle = load_bundle(json.loads((SWEEP / f"{row}.json").read_text())["records"])
    box = FixtureBox(bundle)
    monkeypatch.setattr(_shared, "_live_client", lambda: SimpleNamespace(base_url="https://box.example.com"))
    monkeypatch.setattr(_shared, "live_request_with_reauth",
                        lambda method, c, url, **kw: _Resp(*box.get(url)))
    ready = {c["name"]: {"label": c["name"], "kind": "tickets"}
             for c in bundle.get("configured_connectors", []) if c["name"] == connector}
    monkeypatch.setattr(source_availability, "ready_connector_sources", lambda: ready)
    asked = []

    def run_op(connector, op, params=None, **kw):
        asked.append((connector, op, params))
        r = box.op(connector, op, params)
        return r if r is not None else {"ok": False, "code": "unserved_op"}
    monkeypatch.setattr(tools_execution, "run_op", run_op)
    monkeypatch.setattr(triage_preflight, "fetch_raw_record", lambda key: box.record("alerts", uuid))
    return box, asked


@pytest.mark.parametrize("row", sorted(WORLDS))
def test_the_start_of_turn_read_binds_the_change_record_and_parses_it(monkeypatch, row):
    from fsr_soc_triage.subject_records import preread_subject_records
    uuid, host, connector, number = WORLDS[row]
    box, asked = _serve(monkeypatch, row)
    got = preread_subject_records(f"alerts:{uuid}")
    assert got["names"][0] == host and got["dst"] == ["203.0.113.77"]
    assert {c for c, _, _ in asked} == {connector}
    assert got["asset"] and got["change"]
    rows, unparsed = _es.preread_rows(got, "change")
    assert rows and unparsed is False
    assert number in _es.preread_note(got)
    assert box.misses == []


def test_the_jira_listing_is_asked_with_an_escaped_phrase(monkeypatch):
    from fsr_soc_triage.subject_records import _CHANGE_OPS, _jql_phrase
    assert _jql_phrase('a"b\\c') == '"a\\"b\\\\c"'
    op, build = _CHANGE_OPS["jirav2"]
    assert op == "list_tickets"
    assert build("lab-eval-15") == [{"jql_query": 'text ~ "lab-eval-15" ORDER BY created DESC',
                                     "maxResults": 5}]


def test_the_jira_world_is_a_real_issue_shape():
    bundle = load_bundle("ka_trap_jira_change_row")
    op = next(o for o in bundle["ops"] if o["connector"] == "jirav2")
    issue = op["response"]["data"]["issues"][0]
    assert issue["key"] and issue["fields"]["status"]["name"]
    assert issue["fields"]["description"]["type"] == "doc"
    assert not any(c["name"] == "servicenow" for c in bundle["configured_connectors"])


def test_the_windowless_world_states_no_window_and_names_the_destination():
    bundle = load_bundle("ka_trap_approval_no_window")
    row = next(o for o in bundle["ops"] if o["op"] == "get_all_change_requests"
               )["response"]["data"]["result"][0]
    assert row["approval"] == "approved" and "203.0.113.77" in row["description"]
    assert not {"start_date", "end_date"} & set(row)
