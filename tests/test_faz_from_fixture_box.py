"""Offline FortiAnalyzer hunts answer from the bundle's `faz_logs` table.

The SOC agent's FAZ hunt (`faz_search(by=ip)`) is a run_op on
`fortinet-fortianalyzer` / `start_and_fetch_bulk_device_logs` with the filter
`srcip=X or dstip=X`. A bundle that carries the evidence only in FAZ must answer
that call from its table, filtered by the atoms in the filter, and must record
the call as a read, so a grader can tell "it hunted" from "it narrated".
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as lt  # noqa: E402
from fixture_box import FixtureBox  # noqa: E402

CONN = "fortinet-fortianalyzer"
OP = "start_and_fetch_bulk_device_logs"
NONCE = "FAZ-ONLY-test0000beef"

ROWS = [
    {"itime": 1780079400, "srcip": "10.50.60.70", "dstip": "198.51.100.77",
     "dstport": 8443, "action": "accept", "devname": "FGT_Edge",
     "msg": f"staged upload {NONCE}"},
    {"itime": 1780079460, "srcip": "198.51.100.1", "dstip": "198.51.100.2",
     "dstport": 22, "action": "accept", "devname": "FGT_Core",
     "msg": "unrelated flow"},
]


def _box(rows=ROWS):
    return FixtureBox({"modules": {"faz_logs": list(rows)}})


def _search(box, flt):
    return lt._faz_from_box(box, CONN, OP, {
        "devid": "All_FortiGate", "adom_name": "root", "logtype": "Traffic",
        "start": "2026-05-29T00:00:00.000000Z",
        "end": "2026-05-29T23:59:59.000000Z", "filter": flt,
        "limit": 25, "wait_for_search_process_to_complete": True})


def test_an_ip_pivot_returns_the_matching_row_with_its_nonce():
    res = _search(_box(), "srcip=10.50.60.70 or dstip=10.50.60.70")
    assert res["ok"] is True
    assert [r["msg"] for r in res["data"]] == [f"staged upload {NONCE}"]


def test_a_non_matching_filter_returns_nothing():
    res = _search(_box(), "srcip=192.0.2.250 or dstip=192.0.2.250")
    assert res == {"ok": True, "data": []}


def test_an_empty_table_answers_empty_not_canned():
    res = _search(_box(rows=[]), "srcip=10.50.60.70 or dstip=10.50.60.70")
    assert res == {"ok": True, "data": []}


def test_the_search_is_recorded_as_a_read_even_when_nothing_matches():
    box = _box()
    _search(box, "srcip=192.0.2.250 or dstip=192.0.2.250")
    assert "FAZ srcip=192.0.2.250 or dstip=192.0.2.250" in box.reads


def test_a_bundle_without_the_table_falls_through():
    assert lt._faz_from_box(FixtureBox({}), CONN, OP, {"filter": "srcip=1.2.3.4"}) is None
    assert lt._faz_from_box(None, CONN, OP, {}) is None


def test_other_connectors_and_ops_fall_through():
    box = _box()
    assert lt._faz_from_box(box, "fortinet-fortisiemv2", OP, {"filter": ""}) is None
    assert lt._faz_from_box(box, CONN, "get_alerts", {"filter": ""}) is None
