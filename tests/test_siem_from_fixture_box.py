"""Offline SIEM queries answer from the bundle's `siem_events` table.

The canned rows answered every FortiSIEM query alike -- by source IP, by host,
by destination, any window -- so "what else has this host done" could not be
graded on what the hunt found: the bundle's DNS query and internal connection
were never served. A bundle with the table now answers the pub/v2 query from
it, filtered by the where clause the SIEM helpers write.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as lt  # noqa: E402
from fixture_box import FixtureBox, load_bundle  # noqa: E402

CONN = "fortinet-fortisiemv2"


def _query(box, where):
    sub = lt._siem_from_box(box, CONN, "execute_api_request", {
        "endpoint": "/rest/pub/v2/query/eventQuery", "method": "POST",
        "payload": {"where": where}})
    qid = sub["data"]["queryId"]
    res = lt._siem_from_box(box, CONN, "execute_api_request", {
        "endpoint": "/rest/pub/v2/query/events/results", "method": "GET",
        "query_params": {"queryId": qid}})
    return res["data"]


def _box():
    return FixtureBox(load_bundle("soc_alert_surface"))


def test_a_host_pivot_returns_every_event_for_that_source():
    rows = _query(_box(), '(srcIpAddr="10.50.60.70" OR destIpAddr="10.50.60.70")')
    kinds = {r["eventName"] for r in rows}
    assert {"HTTPS Session", "DNS Query", "Internal Connection"} <= kinds


def test_the_where_clause_filters():
    rows = _query(_box(), 'destIpAddr="10.10.100.1"')
    assert [r["eventName"] for r in rows] == ["Internal Connection"]
    assert _query(_box(), 'srcIpAddr="198.51.100.1"') == []


def test_the_query_is_recorded_as_a_read():
    box = _box()
    _query(box, 'hostName="smithDesktop"')
    assert 'SIEM hostName="smithDesktop"' in box.reads


def test_a_bundle_without_the_table_falls_through():
    box = FixtureBox(load_bundle("ztpf_device_surface"))
    assert lt._siem_from_box(box, CONN, "execute_api_request",
                             {"endpoint": "/rest/pub/v2/query/eventQuery"}) is None
    assert lt._siem_from_box(None, CONN, "execute_api_request", {}) is None
