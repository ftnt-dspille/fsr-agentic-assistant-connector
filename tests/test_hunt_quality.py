"""Scheduled-hunt quality, box-free: the run record carries the verdict the
turn delivered, a configured connector is never reported not installed, and a
hunt names the log sources it can search.

The verdict frame below has the shape a real turn produced (a top-level
`verdict_card` frame with disposition and summary beside findings and
sources_checked), reduced to placeholder values."""
from __future__ import annotations

import copy

import pytest

import ops_hunts
from fsr_soc_triage import source_availability as sa

from test_scheduled_hunts import box, pushes, save, tmp_db  # noqa: F401

REAL_SHAPE_VERDICT = {
    "type": "verdict_card", "id": "hunt_broad_contact", "preceding_tool_use_id": "t9",
    "disposition": "needs_more_info", "severity": "info", "confidence": 0.3,
    "summary": "No source covering network connections was searchable.",
    "findings": [{"claim": "EDR returned zero events for the window",
                  "evidence": ["t7"]}],
    "sources_checked": ["EDR"], "unknowns": ["no network flow source"],
    "what_would_help": "Configure a log source."}


@pytest.fixture
def turn(monkeypatch):
    class _T:
        transcript = [{"type": "text", "text": "searching"},
                      {"type": "capability_gap", "id": "g"},
                      copy.deepcopy(REAL_SHAPE_VERDICT)]
        stop_reason = "end_turn"
        error = None
        calls: list = []
    t = _T()
    t.calls = []

    def _chat(config, params):
        t.calls.append(params)
        return {"ok": True, "session_id": params["session_id"],
                "stop_reason": t.stop_reason, "transcript": list(t.transcript),
                "error": None}
    monkeypatch.setattr(ops_hunts, "chat_turn", _chat)
    return t


def test_the_run_record_and_the_return_carry_the_delivered_verdict(tmp_db, box, pushes, turn):
    save()
    out = ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    assert (out["disposition"], out["severity"], out["confidence"], out["summary"]) == (
        "needs_more_info", "info", 0.3, REAL_SHAPE_VERDICT["summary"])
    (run,) = tmp_db.list_hunt_runs("c2-sweep")
    assert (run["disposition"], run["summary"]) == (
        "needs_more_info", REAL_SHAPE_VERDICT["summary"])
    (view,) = ops_hunts.list_hunts({}, {})["hunts"][0]["runs"][:1]
    assert view["verdict"] == "needs more information"
    assert view["summary"] == REAL_SHAPE_VERDICT["summary"]


def test_the_hunt_message_carries_the_rules_the_sources_and_the_question(
        tmp_db, box, pushes, turn, monkeypatch):
    monkeypatch.setattr(sa, "source_availability", lambda: {
        "fortisiem": sa.SourceAvailability("fortisiem", "FortiSIEM",
                                           "fortinet-fortisiemv2", sa.READY),
        "fortianalyzer": sa.SourceAvailability("fortianalyzer", "FortiAnalyzer",
                                               "fortinet-fortianalyzer", sa.NOT_CONFIGURED)})
    save()
    ops_hunts.run_hunt({}, {"name": "c2-sweep"})
    text = turn.calls[0]["messages"][0]["content"]
    assert "FortiSIEM with siem_search" in text
    assert "FortiAnalyzer" not in text            # not ready: not offered
    assert text.endswith("Hunt question: any host talking to known C2 in the last hour")
    # A question that names no address or host is a broad hunt, not a missing input.
    assert "names no address or host is a broad hunt" in text


def test_every_named_search_helper_is_a_registered_tool():
    import fsr_soc_triage  # noqa: F401 - registers the triage tools
    from fsr_playbooks.llm.tools import anthropic_tools
    names = {t["name"] for t in anthropic_tools()}
    assert set(sa.SEARCH_TOOLS.values()) <= names
    assert set(sa.SEARCH_TOOLS) == set(sa.FAMILY_PREFIXES.values())


def test_a_connector_that_is_configured_but_off_the_installed_roster_is_ready(monkeypatch):
    """An agent-proxied connector is configured (list_configured_connectors shows
    it Available) yet absent from the appliance's own installed roster."""
    from fsr_playbooks.mcp_server import _shared, tools_execution
    from fsr_soc_triage import catalog_probe
    monkeypatch.setattr(_shared, "_live_client", lambda: object())
    monkeypatch.setattr(catalog_probe, "live_connector_names",
                        lambda: {"fortinet-fortiedr"})
    monkeypatch.setattr(tools_execution, "_configured_rows",
                        lambda client, force=False: [{"name": "fortinet-fortisiemv2"},
                                                     {"name": "fortinet-fortiedr"}])
    monkeypatch.setattr(tools_execution, "_preflight_connector",
                        lambda client, c, config_name="": (
                            None if c in ("fortinet-fortisiemv2", "fortinet-fortiedr")
                            else {"ok": False, "code": "connector_not_configured"}))
    installed, preflight = sa._live_inputs()
    avail = sa.compute_availability(installed, preflight)
    assert avail["fortisiem"].state == sa.READY
    # A connector that is neither installed nor configured is still reported so.
    assert avail["fortianalyzer"].state == sa.NOT_INSTALLED
