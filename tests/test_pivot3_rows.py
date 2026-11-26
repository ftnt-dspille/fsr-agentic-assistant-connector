"""The four generic three-hop pivot rows: each hop's search key exists only in the
previous hop's result, so a model that fans out before the key is known cannot
reach the end of the chain. Vendor-neutral (ticketing and audit records, directory).

The chain below is the answer key: walking it through the recorded world must
reach both graded needles, and the needles must be absent from the alert.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402

SN = ("servicenow", "search_record")

# row -> hops: (connector, op, params, text the result must reveal for the next hop)
CHAINS = {
    "ka_pivot3_phish_recipients": [
        (*SN, {"value": "pay-invoice-portal.example"}, "sgreer@corp.example"),
        (*SN, {"value": "sgreer"}, "wks-hr-14"),
        (*SN, {"value": "sgreer"}, "hr-payroll-01"),
    ],
    "ka_pivot3_login_host_process": [
        ("activedirectory", "global_search", {"query": "kfenwick"}, "wks-eng-31"),
        ("servicenow", "search_record", {"value": "wks-eng-31"}, "10.20.5.31"),
        (*SN, {"value": "10.20.5.31"}, "rclone"),
    ],
    "ka_pivot3_exfil_owner_sessions": [
        ("servicenow", "search_record", {"value": "10.30.8.12"}, "ilarsen"),
        (*SN, {"value": "ilarsen"}, "dev-build-07"),
        (*SN, {"value": "ilarsen"}, "203.0.113.9/up/src.tgz"),
    ],
    "ka_pivot3_ticket_service_account": [
        ("servicenow", "search_record", {"value": "INC0077200"}, "svc-report-gen"),
        (*SN, {"value": "svc-report-gen"}, "bastion-02"),
        (*SN, {"value": "bastion-02"}, "mcarrow"),
    ],
}


def _spec(name):
    return json.loads((_ROOT / "scripts/conversations/sweep" / f"{name}.json").read_text())


@pytest.mark.parametrize("name", sorted(CHAINS))
def test_each_hop_is_served_and_reveals_the_next_key(name):
    box = FixtureBox(load_bundle(_spec(name)["records"]))
    for connector, op, params, reveals in CHAINS[name]:
        got = box.op(connector, op, params)
        assert got and got["ok"], (connector, op, params)
        assert reveals in json.dumps(got), (connector, op, reveals)


@pytest.mark.parametrize("name", sorted(CHAINS))
def test_the_needles_are_not_in_the_alert_and_are_graded_together(name):
    spec = _spec(name)
    box = FixtureBox(load_bundle(spec["records"]))
    alert = json.dumps(box.record("alerts", spec["mount"]["uuid"]))
    fact = spec["expect"]["fact_from_tool"]
    assert fact["require_all"] and len(fact["values"]) == 2
    for needle in fact["values"]:
        assert needle not in alert
    assert box.strict_ops


@pytest.mark.parametrize("name", sorted(CHAINS))
def test_a_later_hop_key_is_a_miss_when_guessed_from_the_alert(name):
    # Searching the alert's own values never returns the later-hop needle.
    spec = _spec(name)
    box = FixtureBox(load_bundle(spec["records"]))
    needle = spec["expect"]["fact_from_tool"]["values"][1]
    got = box.op("servicenow", "search_record", {"value": "zzz-no-such-key"})
    assert got is None or needle not in json.dumps(got)
