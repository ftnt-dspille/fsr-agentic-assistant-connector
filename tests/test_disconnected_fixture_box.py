"""The Disconnected box is the default inventory with two connectors down.

A hand copy of the canned inventory would drift from it silently (the Slack box
pins the same relationship). And the `health` field must reach every probe the
offline tier stubs, or the row reads Available and grades nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (_SCRIPTS, _SCRIPTS.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import local_turn  # noqa: E402
from scripts.fixture_box import load_bundle  # noqa: E402


def test_it_is_the_default_inventory_plus_servicenow_with_two_down():
    inv = load_bundle("soc_alert_surface_disconnected")["configured_connectors"]
    strip = [{k: v for k, v in c.items() if k != "health"} for c in inv[:-1]]
    assert strip == local_turn._CANNED_CONFIGURED_CONNECTORS
    down = {c["name"] for c in inv if c.get("health") == "Disconnected"}
    assert down == {"fortigate-firewall", "servicenow"}
    assert inv[-1]["name"] == "servicenow"


def test_a_new_cassette_world_does_not_inherit_the_previous_worlds_connector_roster():
    """A sweep worker runs many worlds in one process; the roster memo and the
    framework's configured-connector cache are keyed by nothing."""
    import fsr_playbooks.mcp_server.tools_execution as te
    from fsr_soc_triage import source_availability as sa
    sa._conn_memo["value"], sa._conn_memo["ts"] = {"servicenow": {"kind": "tickets"}}, 9e18
    te._CONFIGURED_CACHE["rows"], te._CONFIGURED_CACHE["ts"] = [{"name": "servicenow"}], 9e18
    scope = local_turn.PatchScope()
    try:
        local_turn.install_offline_seams(scope, records="ka_trap_jira_change_row")
        assert sa._conn_memo["value"] is None
        assert te._CONFIGURED_CACHE["rows"] is None
    finally:
        scope.close()
