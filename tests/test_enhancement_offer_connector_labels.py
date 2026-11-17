"""U6 follow-up: the enhancement offer carries the box's connector catalog
display labels, read through the same roster as the I5 ready-source ledger, and
falls back (no map, the widget humanizes the id) when the catalog is unreadable."""
from __future__ import annotations

import pytest

from connector_fsr_agentic_assistant.operations import ResolvedPersona, _advertised_tools
from fsr_playbooks.mcp_server import _citation_validator as cv
from fsr_playbooks.mcp_server import _verified_yaml, emit_enhancement_offer, verify_enhancement
from fsr_playbooks.mcp_server import tools_connector_discovery as tcd
from fsr_soc_triage import source_availability as sa

pytestmark = pytest.mark.skipif(
    not hasattr(cv, "set_turn_connector_labels"),
    reason="the installed fsr_playbooks predates the offer connector_labels map")

BEFORE = """collection: C
playbooks:
- name: PB
  trigger_step_id: start
  steps:
  - type: start
    name: Start
    module: alerts
    next: Lookup
  - type: connector
    name: Lookup
    connector: fortinet-fortiguard-threat-intelligence
    operation: ip_lookup
    params: {ip: '{{vars.ip}}'}
"""
AFTER = BEFORE.replace("    params: {ip: '{{vars.ip}}'}",
                       "    params: {ip: '{{vars.ip}}', verbose: true}")

_ROSTER = {"configured": [
    {"name": "fortinet-fortiguard-threat-intelligence",
     "label": "FortiGuard Threat Intelligence", "status": "Available"},
    {"name": "servicenow", "label": "ServiceNow", "status": "Available"},
    {"name": "unlabelled", "label": "", "status": "Available"}]}


@pytest.fixture(autouse=True)
def _reset():
    cv.set_turn_connector_labels(None)
    _verified_yaml.clear()
    sa.forget()
    yield
    cv.set_turn_connector_labels(None)
    _verified_yaml.clear()
    sa.forget()


def _enhance_turn(monkeypatch):
    monkeypatch.setattr(sa, "ready_connector_sources", lambda: {})
    monkeypatch.setattr(sa, "source_availability", lambda: {})
    tools = _advertised_tools({}, ResolvedPersona(intent="enhance"), include_mcp=False)
    assert any(t["name"] == "edit_playbook" for t in tools), \
        "fixture needs the enhance tools on offer"


def _offer() -> dict:
    r = verify_enhancement(BEFORE, AFTER, "add verbose")
    assert r["ready_to_push"], r.get("required_fixes")
    out = emit_enhancement_offer("o1", "Adds verbose output.", r["verified_id"])
    assert out["ok"] is True, out
    return out["card"]


def test_connector_labels_come_from_the_configured_roster(monkeypatch):
    monkeypatch.setattr(tcd, "list_configured_connectors", lambda **kw: _ROSTER)
    assert sa.connector_labels() == {
        "fortinet-fortiguard-threat-intelligence": "FortiGuard Threat Intelligence",
        "servicenow": "ServiceNow"}


def test_an_unreadable_roster_is_none_not_empty(monkeypatch):
    def boom(**kw):
        raise RuntimeError("no box")
    monkeypatch.setattr(tcd, "list_configured_connectors", boom)
    assert sa.connector_labels() is None


def test_the_offer_carries_the_catalog_label_for_the_connector_it_names(monkeypatch):
    monkeypatch.setattr(tcd, "list_configured_connectors", lambda **kw: _ROSTER)
    _enhance_turn(monkeypatch)
    assert _offer()["connector_labels"] == {
        "fortinet-fortiguard-threat-intelligence": "FortiGuard Threat Intelligence"}


def test_the_offer_falls_back_to_no_map_when_the_catalog_is_unreadable(monkeypatch):
    def boom(**kw):
        raise RuntimeError("no box")
    monkeypatch.setattr(tcd, "list_configured_connectors", boom)
    _enhance_turn(monkeypatch)
    assert "connector_labels" not in _offer()


def test_a_turn_that_never_offers_a_change_reads_no_roster(monkeypatch):
    calls = []
    monkeypatch.setattr(tcd, "list_configured_connectors",
                        lambda **kw: calls.append(1) or _ROSTER)
    _enhance_turn(monkeypatch)
    assert calls == []
