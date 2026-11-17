"""I3: the advertised-tools pass tells the framework's evidence cap which
ready log sources this turn can query, so a capped thin verdict names the
ones it never checked."""
from __future__ import annotations

import pytest

from connector_fsr_agentic_assistant.operations import ResolvedPersona, _advertised_tools
from fsr_playbooks.mcp_server import _citation_validator as cv
from fsr_soc_triage import source_availability as sa


def _avail(state_by_key):
    return {k: sa.SourceAvailability(k, label, f"fortinet-{k}", state)
            for k, (label, state) in state_by_key.items()}


@pytest.fixture(autouse=True)
def _reset():
    cv.set_turn_ready_sources(None)
    cv.set_turn_ready_connectors(None)
    yield
    cv.set_turn_ready_sources(None)
    cv.set_turn_ready_connectors(None)


def test_ready_sources_on_offer_are_handed_to_the_cap(monkeypatch):
    monkeypatch.setattr(sa, "ready_connector_sources", lambda: {})
    monkeypatch.setattr(sa, "source_availability", lambda: _avail({
        "fortisiem": ("FortiSIEM", sa.READY),
        "fortianalyzer": ("FortiAnalyzer", sa.NOT_INSTALLED),
    }))
    tools = _advertised_tools({}, ResolvedPersona(intent="triage"), include_mcp=False)
    names = {t["name"] for t in tools}
    assert any(n.startswith("siem_") for n in names), "fixture needs a siem tool on offer"
    ready = cv._turn_ready_sources.get() or {}
    assert ready.get("siem_") == "FortiSIEM"
    assert "faz_" not in ready
    assert cv.unchecked_ready_sources({}) == ["FortiSIEM"]


_CONFIGURED = {"configured": [
    {"name": "servicenow", "label": "ServiceNow", "status": "Available"},
    {"name": "virustotal", "label": "VirusTotal", "status": "Available"},
    {"name": "slack", "label": "Slack", "status": "Available"},
    {"name": "fortinet-fortisiemv2", "label": "FortiSIEM", "status": "Available"}]}


def _patch_connectors(monkeypatch):
    from fsr_playbooks.llm import autonomy
    from fsr_playbooks.mcp_server import tools_connector_discovery as tcd
    monkeypatch.setattr(tcd, "list_configured_connectors", lambda **kw: _CONFIGURED)
    cats = {"servicenow": "cmdb,threat intelligence", "virustotal": "threat intelligence",
            "slack": "messaging", "fortinet-fortisiemv2": "threat intelligence"}
    monkeypatch.setattr(autonomy, "_connector_category", cats.get)
    sa.forget()


def test_ready_connector_sources_come_from_catalog_category_not_names(monkeypatch):
    _patch_connectors(monkeypatch)
    got = sa.ready_connector_sources()
    assert got == {"servicenow": {"label": "ServiceNow", "kind": "tickets"},
                   "virustotal": {"label": "VirusTotal", "kind": "intel"}}


def test_advertised_tools_hands_ticket_and_intel_connectors_to_the_ledger(monkeypatch):
    _patch_connectors(monkeypatch)
    monkeypatch.setattr(sa, "source_availability", lambda: _avail({
        "fortisiem": ("FortiSIEM", sa.READY)}))
    tools = _advertised_tools({}, ResolvedPersona(intent="triage"), include_mcp=False)
    assert any(t["name"] == "run_op" for t in tools), "fixture needs run_op on offer"
    assert cv.unchecked_ready_sources({}) == ["FortiSIEM", "ServiceNow", "VirusTotal"]
    queried = {"c1": {"name": "run_op", "args": {"connector": "servicenow"}}}
    assert cv.unchecked_ready_sources(queried) == ["FortiSIEM", "VirusTotal"]


# ---- X7: the "What would help" line is computed from this ledger -------------
def _uncertain_card(unknowns):
    from fsr_playbooks.mcp_server._citation_validator import (
        TurnEvidence,
        register_tool_result,
        set_turn_evidence,
    )
    from fsr_playbooks.mcp_server.tools_emit import emit_verdict
    set_turn_evidence(TurnEvidence())
    register_tool_result("c1", "siem_search", True, {}, {"ok": True, "count": 1, "events": [1]})
    out = emit_verdict(disposition="needs_more_info", severity="medium", confidence=0.4,
                       summary="Unclear.", unknowns=unknowns,
                       findings=[{"claim": "Host active.", "evidence": ["c1"]}])
    assert out["ok"] is True, out
    return out["card"]


_ASKS = ["Whether this host's activity was approved is unknown",
         "The reputation of the destination address is unknown"]


def _offer(monkeypatch, connectors):
    monkeypatch.setattr(sa, "ready_connector_sources", lambda: connectors)
    monkeypatch.setattr(sa, "source_availability", lambda: _avail({
        "fortisiem": ("FortiSIEM", sa.READY)}))
    tools = _advertised_tools({}, ResolvedPersona(intent="triage"), include_mcp=False)
    assert any(t["name"] == "run_op" for t in tools), "fixture needs run_op on offer"


def test_no_connectors_configured_says_which_source_types_are_missing(monkeypatch):
    _offer(monkeypatch, {})
    card = _uncertain_card(_ASKS)
    assert [h["kind"] for h in card["what_would_help"]] == ["tickets", "intel"]
    assert card["what_would_help"][0]["line"].startswith("No ticket system is connected")


def test_connected_ticket_and_intel_sources_leave_no_missing_type_line(monkeypatch):
    _offer(monkeypatch, {"servicenow": {"label": "ServiceNow", "kind": "tickets"},
                         "virustotal": {"label": "VirusTotal", "kind": "intel"}})
    assert "what_would_help" not in _uncertain_card(_ASKS)


def test_unreadable_connector_roster_claims_nothing(monkeypatch):
    """A failed roster read is None, not {}: silence beats a false 'not connected'."""
    _patch_connectors(monkeypatch)
    from fsr_playbooks.mcp_server import tools_connector_discovery as tcd

    def boom(**kw):
        raise RuntimeError("no box")
    monkeypatch.setattr(tcd, "list_configured_connectors", boom)
    assert sa.ready_connector_sources() is None
    monkeypatch.setattr(sa, "source_availability", lambda: _avail({
        "fortisiem": ("FortiSIEM", sa.READY)}))
    _advertised_tools({}, ResolvedPersona(intent="triage"), include_mcp=False)
    assert "what_would_help" not in _uncertain_card(_ASKS)


def test_no_ready_log_source_on_offer_says_so_for_a_logs_unknown(monkeypatch):
    """Nothing ready is an empty roster (not an unreported one): the card names the gap."""
    monkeypatch.setattr(sa, "ready_connector_sources", lambda: {})
    monkeypatch.setattr(sa, "source_availability", lambda: _avail({
        "fortisiem": ("FortiSIEM", sa.NOT_INSTALLED)}))
    _advertised_tools({}, ResolvedPersona(intent="triage"), include_mcp=False)
    card = _uncertain_card(["Firewall or flow logs for the three connections are missing"])
    (h,) = [h for h in card["what_would_help"] if h["kind"] == "logs"]
    assert h["line"].startswith("No log source such as a SIEM or firewall log store is connected")
