"""B3a: a log-source helper is advertised only when its connector can answer.

Live on .159 there was no `fortinet-fortisiem` REST connector and no
FortiAnalyzer, yet `siem_search` and `faz_search` were on offer every turn and
failed 14/14 and 11/11 calls. A family is now withheld when its connector is
not installed, not configured, or unhealthy -- and the turn is told which and
how to fix it, so the analyst can make the assistant more useful instead of the
source silently disappearing.
"""
from __future__ import annotations

import time

import pytest

from fsr_soc_triage import source_availability as sa
from fsr_playbooks.mcp_server.triage_normalize import normalize_record
from fsr_soc_triage.triage_sources import SOURCE_TOOLS, build_source_routing_block

INSTALLED_ALL = {ts["connector"] for ts in SOURCE_TOOLS.values()}
# FortiSIEM is backed by either connector; v2 (the successor) is preferred.
SIEM_V2, SIEM_V1 = "fortinet-fortisiemv2", "fortinet-fortisiem"


def _pf(errors: dict[str, dict]):
    return lambda connector: errors.get(connector)


def _state(avail, key):
    return avail[key].state if key in avail else None


def test_every_verdict_is_decided_from_roster_and_preflight():
    avail = sa.compute_availability(
        installed=INSTALLED_ALL - {"fortinet-fortianalyzer"},
        preflight=_pf({
            SIEM_V2: {"code": "connector_not_configured"},
            "fortinet-fortimanager-json-rpc": {
                "code": "connector_unhealthy", "health_status": "Disconnected"},
        }))
    assert _state(avail, "fortianalyzer") == sa.NOT_INSTALLED
    assert _state(avail, "fortisiem") == sa.NOT_CONFIGURED
    assert _state(avail, "fortimanager") == sa.UNHEALTHY
    assert "Disconnected" in avail["fortimanager"].detail


def test_a_ready_connector_is_ready():
    avail = sa.compute_availability(INSTALLED_ALL, _pf({}))
    assert all(v.ready for v in avail.values())
    assert set(avail) == {"fortisiem", "fortianalyzer", "fortimanager", "fortiedr"}


@pytest.mark.parametrize("preflight", [
    _pf({SIEM_V2: {"code": "some_other_failure"}}),
    lambda c: (_ for _ in ()).throw(RuntimeError("lookup blew up")),
])
def test_an_unknown_verdict_leaves_the_family_advertised(preflight):
    avail = sa.compute_availability(INSTALLED_ALL, preflight)
    assert "fortisiem" not in avail
    tools = [{"name": "siem_search"}]
    assert sa.gate_source_tools(tools, avail) == tools


def test_a_slow_healthcheck_never_stalls_the_turn():
    def slow(connector):
        if connector == SIEM_V2:
            time.sleep(2)
        return None
    t0 = time.monotonic()
    avail = sa.compute_availability(INSTALLED_ALL, slow, budget_s=0.3)
    assert time.monotonic() - t0 < 1.5
    assert "fortisiem" not in avail            # unknown -> kept
    assert avail["fortianalyzer"].ready


def test_without_a_roster_a_missing_config_is_only_not_configured():
    avail = sa.compute_availability(
        None, _pf({SIEM_V2: {"code": "connector_not_configured"},
                   SIEM_V1: {"code": "connector_not_configured"}}))
    assert _state(avail, "fortisiem") == sa.NOT_CONFIGURED
    assert avail["fortisiem"].connector == SIEM_V2


def _down(*keys):
    return {k: sa.SourceAvailability(k, SOURCE_TOOLS[k]["label"],
                                     SOURCE_TOOLS[k]["connector"], sa.NOT_INSTALLED)
            for k in keys}


def test_gate_drops_only_the_down_family():
    tools = [{"name": n} for n in (
        "siem_search", "siem_search_ip", "faz_search", "fmg_get_device_status",
        "get_record", "mcp_fortisiem__search_events", "run_op")]
    kept = {t["name"] for t in sa.gate_source_tools(tools, _down("fortisiem"))}
    assert kept == {"faz_search", "fmg_get_device_status", "get_record",
                    "mcp_fortisiem__search_events", "run_op"}


def test_every_registered_hunt_helper_belongs_to_a_family():
    # A new `siem_*`/`faz_*`/`fmg_*` helper must be gated with its family; a
    # prefix list that drifts from the registry would leave it on offer.
    from fsr_playbooks.llm.tools import anthropic_tools
    names = {t["name"] for t in anthropic_tools()}
    hunt = {n for n in names if n.split("_", 1)[0] in ("siem", "faz", "fmg")}
    assert hunt, "no hunt helpers registered -- the test is vacuous"
    assert all(sa.family_of(n) for n in hunt)
    for key in sa.FAMILY_PREFIXES.values():
        assert key in SOURCE_TOOLS


def test_note_tells_the_analyst_what_is_missing_and_how_to_fix_it():
    avail = {
        "fortisiem": sa.SourceAvailability("fortisiem", "FortiSIEM",
                                           "fortinet-fortisiem", sa.NOT_INSTALLED),
        "fortianalyzer": sa.SourceAvailability(
            "fortianalyzer", "FortiAnalyzer", "fortinet-fortianalyzer",
            sa.UNHEALTHY, "status 'Disconnected'"),
        "fortimanager": sa.SourceAvailability(
            "fortimanager", "FortiManager", "fortinet-fortimanager-json-rpc",
            sa.READY),
    }
    note = sa.availability_note(avail, {"get_record"})
    assert "FortiSIEM" in note and "Content Hub" in note
    assert "FortiAnalyzer" in note and "health check" in note
    assert "Disconnected" in note
    assert "FortiManager" not in note
    assert "capability_gap" in note


def test_note_points_at_the_mcp_route_when_there_is_one():
    note = sa.availability_note(_down("fortisiem"),
                                {"mcp_fortisiem__search_events", "get_record"})
    assert "mcp_fortisiem__search_events" in note


def test_no_note_when_everything_is_reachable():
    assert sa.availability_note({}, set()) == ""
    ready = {"fortisiem": sa.SourceAvailability(
        "fortisiem", "FortiSIEM", "fortinet-fortisiem", sa.READY)}
    assert sa.availability_note(ready, set()) == ""


def test_routing_block_stops_recommending_a_withheld_source():
    norm = normalize_record({"name": "C2", "source": "Fortinet FortiSIEM",
                             "sourceIp": "10.0.0.5"})
    before = build_source_routing_block(norm)
    assert 'siem_search(by="ip"' in before
    after = build_source_routing_block(norm, _down("fortisiem"))
    assert 'siem_search(by="ip"' not in after
    assert "UNAVAILABLE" in after


def test_offline_availability_is_empty_and_memoized(monkeypatch):
    sa.forget()
    calls = []
    monkeypatch.setattr(sa, "_live_inputs", lambda: calls.append(1))
    assert sa.source_availability() == {}
    assert sa.source_availability() == {}
    assert len(calls) == 1
    sa.forget()
    sa.source_availability()
    assert len(calls) == 2
    sa.forget()


def test_live_inputs_reach_a_real_client_accessor(monkeypatch):
    # The first draft imported `fsr_playbooks.execution_api.live_client`, which
    # does not exist; the ImportError was swallowed as "unknown" and the gate
    # never fired. Pin the accessor the framework actually has.
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    assert sa._live_inputs() is None


def test_the_card_handed_to_the_model_passes_the_card_validator():
    """Live, the model built its own capability_gap payload twice and both were
    refused (bad_payload, bad_resume): the analyst saw no card. The note now
    carries one the emitter accepts, with an id the re-check path parses."""
    import json as _json
    import re

    from fsr_playbooks.mcp_server.tools_emit import emit_card
    for state in (sa.NOT_INSTALLED, sa.NOT_CONFIGURED, sa.UNHEALTHY):
        v = sa.SourceAvailability("fortisiem", "FortiSIEM", "fortinet-fortisiem",
                                  state, "status 'Disconnected'")
        note = sa.availability_note({"fortisiem": v}, set())
        m = re.search(r"payload=(\{.*\})\)`", note)
        assert m, note
        payload = _json.loads(m.group(1))
        res = emit_card(card_type="capability_gap", payload=payload)
        assert res.get("ok") is not False, res
        want = "capgap_health_" if state == sa.UNHEALTHY else "capgap_cfg_"
        assert payload["id"] == want + "fortinet-fortisiem"


def test_no_card_when_an_mcp_route_exists():
    note = sa.availability_note(_down("fortisiem"), {"mcp_fortisiem__search_events"})
    assert "capability_gap', payload=" not in note


# --- backstop: the record's own source is unreachable ----------------------

_SIEM_ALERT = {"name": "C2", "source": "Fortinet FortiSIEM", "sourceIp": "10.0.0.5"}
_BLOCKING = {"action_card", "choice_card", "capability_gap", "approval_request"}
_VERDICT_TURN = [{"type": "tool_use", "id": "c1", "name": "get_record"},
                 {"type": "text", "text": "I could not check the SIEM."},
                 {"type": "verdict_card", "id": "v1"}]


def test_only_the_records_own_withheld_sources_are_gaps():
    norm = normalize_record(_SIEM_ALERT)
    assert [v.key for v in sa.originating_gaps(norm, _down("fortisiem", "fortianalyzer"))] \
        == ["fortisiem"]
    assert sa.originating_gaps(norm, _down("fortianalyzer")) == []
    assert sa.originating_gaps(None, _down("fortisiem")) == []


def _read_turn(record, extra=()):
    return ([{"type": "tool_use", "id": "g1", "name": "get_record"},
             {"type": "tool_result", "tool_use_id": "g1", "content": record}]
            + list(extra))


@pytest.mark.parametrize("content", [
    _SIEM_ALERT, {"record": _SIEM_ALERT}, {"ok": True, "data": _SIEM_ALERT}])
def test_the_record_comes_from_this_turns_get_record(content):
    gaps = sa.record_gaps_in_turn(_read_turn(content), _down("fortisiem"))
    assert [v.key for v in gaps] == ["fortisiem"]


def test_no_gap_without_a_record_read_or_a_down_source():
    other = [{"type": "tool_use", "id": "s1", "name": "search_module_records"},
             {"type": "tool_result", "tool_use_id": "s1", "content": _SIEM_ALERT}]
    assert sa.record_gaps_in_turn(other, _down("fortisiem")) == []
    assert sa.record_gaps_in_turn(_read_turn(_SIEM_ALERT), {}) == []


def test_a_verdict_turn_gets_the_card_the_model_left_out():
    out = sa.append_gap_cards(_VERDICT_TURN, list(_down("fortisiem").values()),
                              mcp_servers=set(), blocking_types=_BLOCKING)
    assert out is not None and out[:3] == _VERDICT_TURN
    card = out[-1]
    assert card["type"] == "capability_gap"
    assert card["id"] == "capgap_cfg_" + SOURCE_TOOLS["fortisiem"]["connector"]
    assert card["resume"] == {"label": "Re-check & continue", "value": "recheck_connector"}


@pytest.mark.parametrize("transcript,mcp", [
    (_VERDICT_TURN + [{"type": "capability_gap", "id": "x"}], set()),   # model sent one
    (_VERDICT_TURN, {"fortisiem"}),                                      # reachable over MCP
    (_VERDICT_TURN + [{"type": "action_card", "id": "a"}], set()),       # approval waits
    ([], set()),
])
def test_the_backstop_stays_out_of_the_way(transcript, mcp):
    assert sa.append_gap_cards(transcript, list(_down("fortisiem").values()),
                               mcp_servers=mcp, blocking_types=_BLOCKING) is None


def test_either_fortisiem_connector_backs_the_helpers():
    """Live on .159: only `fortinet-fortisiemv2` is installed. It carries every
    v1 operation under v1's name (6.1.0+), so the helpers run on it -- they
    were withheld there as "FortiSIEM not installed"."""
    v2_only = (INSTALLED_ALL - {SIEM_V1}) | {SIEM_V2}
    v = sa.compute_availability(v2_only, _pf({}))["fortisiem"]
    assert v.ready and v.connector == SIEM_V2
    v1_only = (INSTALLED_ALL - {SIEM_V2}) | {SIEM_V1}
    v = sa.compute_availability(v1_only, _pf({}))["fortisiem"]
    assert v.ready and v.connector == SIEM_V1


def test_v2_is_preferred_and_a_broken_v2_falls_back_to_v1():
    both = INSTALLED_ALL | {SIEM_V1, SIEM_V2}
    assert sa.compute_availability(both, _pf({}))["fortisiem"].connector == SIEM_V2
    v = sa.compute_availability(both, _pf({
        SIEM_V2: {"code": "connector_unhealthy", "health_status": "Disconnected"},
    }))["fortisiem"]
    assert v.ready and v.connector == SIEM_V1


def test_with_none_ready_the_fixable_one_is_reported():
    # v2 absent, v1 installed but unconfigured: "configure v1" is one step;
    # "install v2" would send the analyst the long way round.
    v = sa.compute_availability(
        (INSTALLED_ALL - {SIEM_V2}) | {SIEM_V1},
        _pf({SIEM_V1: {"code": "connector_not_configured"}}))["fortisiem"]
    assert v.state == sa.NOT_CONFIGURED and v.connector == SIEM_V1
    # Neither installed: name the successor for install.
    v = sa.compute_availability(INSTALLED_ALL - {SIEM_V1, SIEM_V2}, _pf({}))["fortisiem"]
    assert v.state == sa.NOT_INSTALLED and v.connector == SIEM_V2


def test_an_unrelated_sibling_connector_is_still_named():
    """A same-product connector the helpers can't use (neither v1 nor v2)
    still gets named, so "install FortiSIEM" isn't read as nonsense by an
    analyst looking at a working FortiSIEM connector."""
    other = "fortinet-fortisiem-custom"
    avail = sa.compute_availability(
        (INSTALLED_ALL - {SIEM_V1, SIEM_V2}) | {other}, _pf({}))
    v = avail["fortisiem"]
    assert v.state == sa.NOT_INSTALLED and v.related == (other,)
    note = sa.availability_note(avail, set())
    assert f"`{other}` is installed" in note and "run_op" in note
    card = sa.gap_card(v)
    assert other in card["why"] and card.get("tips")
    assert emit_card_ok(card)


def test_resolve_connector_follows_the_gate(monkeypatch):
    ready_v1 = sa.SourceAvailability("fortisiem", "FortiSIEM", SIEM_V1, sa.READY)
    monkeypatch.setattr(sa, "source_availability", lambda: {"fortisiem": ready_v1})
    assert sa.resolve_connector("fortisiem") == SIEM_V1
    # Nothing decided (offline, or the pass timed out): the roster decides,
    # and with no roster the successor is the default.
    from fsr_soc_triage import catalog_probe
    monkeypatch.setattr(sa, "source_availability", lambda: {})
    monkeypatch.setattr(catalog_probe, "live_connector_names", lambda: {SIEM_V1})
    assert sa.resolve_connector("fortisiem") == SIEM_V1
    monkeypatch.setattr(catalog_probe, "live_connector_names", lambda: None)
    assert sa.resolve_connector("fortisiem") == SIEM_V2


def emit_card_ok(payload):
    from fsr_playbooks.mcp_server.tools_emit import emit_card
    return emit_card(card_type="capability_gap", payload=payload).get("ok") is True


def test_no_related_connector_means_no_extra_wording():
    avail = sa.compute_availability(INSTALLED_ALL - {SIEM_V1, SIEM_V2}, _pf({}))
    assert avail["fortisiem"].related == ()
    assert "is installed, but" not in sa.availability_note(avail, set())
