"""A fixture world can serve native-MCP servers (roadmap S11).

Before this no offline row could reach an `mcp_*` tool: off the box the
connector's gateway client is the on-box HMAC adapter, which returns nothing.
A bundle's `mcp_servers` now reach the turn through the connector's own
All-servers discovery, and the graders know the tools are real.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_REPO = _SCRIPTS.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import local_turn as lt  # noqa: E402
from scripts.conv_scenarios import world_mcp_tool_names  # noqa: E402
from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_mcp_after():
    """A world turn redirects the connector's MCP seams and materializes the
    world's tools into the worker-wide registry. Undo both, or every later test
    in the process sees FortiSIEM tools it never configured."""
    yield
    from fsr_playbooks.mcp_server import materializer
    materializer.reset()


MOUNT = {"module": "alerts", "iri": "/api/3/alerts/5c1e2a90-1b7d-4c55-9a3e-7d0c2b1a4e01"}


def _turn(records):
    return lt.local_turn(messages=[{"role": "user", "content": "triage"}],
                         intent="triage", module="alerts", entity=dict(MOUNT),
                         llm="fake", persona="none", records=records)


def test_a_world_with_mcp_servers_advertises_their_tools():
    res = _turn("ka_mcp_c2_beacon")
    names = set(res.tools or [])
    assert "mcp_fortisiem__get_reputation_by_entity" in names
    assert "mcp_fortisiem__get_incidents_by_entity" in names


def test_the_worlds_write_tool_is_approval_gated():
    _turn("ka_mcp_c2_beacon")
    from fsr_playbooks.llm.tools import TOOL_TIERS
    assert TOOL_TIERS.get("mcp_fortisiem__update_incident_severity_by_id") == 3


def test_a_later_world_without_mcp_does_not_inherit_the_servers():
    _turn("ka_mcp_c2_beacon")
    res = _turn("ka_c2_beacon")
    assert not [n for n in (res.tools or []) if n.startswith("mcp_")]


def test_mcp_call_answers_recorded_args_and_misses_honestly():
    box = FixtureBox(load_bundle("ka_mcp_c2_beacon"))
    hit = box.mcp_call("fortisiem", "get_reputation_by_entity",
                       {"params": {"ip": ["198.51.100.77"]}})
    assert hit[0]["malwareGroups"][0]["verdict"].startswith("Malware")
    miss = box.mcp_call("fortisiem", "get_reputation_by_entity",
                        {"params": {"ip": ["203.0.113.1"]}})
    assert miss["status"] == "failed" and miss["result"] is None
    assert box.mcp_calls == ["fortisiem.get_reputation_by_entity"] * 2
    assert any(m.startswith("MCP fortisiem.get_reputation_by_entity") for m in box.misses)


def test_world_mcp_tool_names_lists_what_the_world_serves():
    box = FixtureBox(load_bundle("ka_mcp_c2_beacon"))
    names = world_mcp_tool_names(box)
    assert "mcp_fortisiem__get_context_by_entity" in names
    assert world_mcp_tool_names(None) == set()

