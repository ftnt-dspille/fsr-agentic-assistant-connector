# Mutating MCP tools must be tier-3 (approval-carded), not tier-1 auto-run.
#
# The dynamic-tool materializer tiers MCP tools PER SERVER, so a mixed gateway
# like `soc` (reads: get_alert; mutations: block_indicator) marked read_only
# leaves block_indicator at tier-1 -> containment auto-executes with NO approval
# card, defeating the tier-3 gate. `_gate_mutating_mcp_tools` elevates the
# OPERATOR-DECLARED mutating tools (config `mcp_allowlist[server].mutating_tools`)
# to tier-3. Classification is explicit config, never a name heuristic (the
# gateway exposes no read-only/destructive annotations to key off).
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_HAVE_FW = importlib.util.find_spec("fsr_playbooks") is not None


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


pytestmark = pytest.mark.skipif(
    not _HAVE_FW, reason="fsr_playbooks framework not importable in this env")

_SOC_SURFACE = [
    {"name": "mcp_soc__get_alert", "tier": 1, "confirm_mode": "auto"},
    {"name": "mcp_soc__get_indicators", "tier": 1, "confirm_mode": "auto"},
    {"name": "mcp_soc__block_indicator", "tier": 1, "confirm_mode": "auto"},
    {"name": "mcp_soc__update_alert_ai_analysis", "tier": 1, "confirm_mode": "auto"},
    {"name": "run_op", "tier": 1, "confirm_mode": "auto"},  # curated, not mcp_
]


def _cfg(mutating):
    return {"mcp_allowlist": json.dumps(
        {"soc": {"tools": "*", "tier": "read_only", "mutating_tools": mutating}})}


def test_parse_declared_mutating_tools(ops):
    got = ops._mcp_mutating_tools_by_server(_cfg(["block_indicator", "update_alert_ai_analysis"]))
    assert got["soc"] == {"block_indicator", "update_alert_ai_analysis"}


def test_absent_config_still_gates_the_builtin_mutations(ops):
    """The gateway's write tools card even when nobody declared them. Before,
    an undeclared `block_indicator` ran at tier 1 with no approval."""
    builtin = {"soc": {"block_indicator", "update_alert_ai_analysis"},
               "playbooks": {"execute_playbook"}}
    assert ops._mcp_mutating_tools_by_server({}) == builtin
    assert ops._mcp_mutating_tools_by_server({"mcp_allowlist": "not json"}) == builtin


def test_undeclared_block_indicator_is_tier3(ops):
    from fsr_playbooks.llm.tools import TOOL_TIERS
    for t in _SOC_SURFACE:
        TOOL_TIERS.pop(t["name"], None)
    tools = [dict(t) for t in _SOC_SURFACE]
    ops._gate_mutating_mcp_tools(tools, {})
    assert TOOL_TIERS["mcp_soc__block_indicator"] == 3
    assert TOOL_TIERS.get("mcp_soc__get_alert", 1) == 1
    TOOL_TIERS.pop("mcp_soc__block_indicator", None)
    TOOL_TIERS.pop("mcp_soc__update_alert_ai_analysis", None)


def test_operator_declarations_add_to_the_builtins(ops):
    got = ops._mcp_mutating_tools_by_server(_cfg(["enrich_indicator"]))
    assert got["soc"] == {"block_indicator", "update_alert_ai_analysis", "enrich_indicator"}


def test_declared_mutating_tool_elevated_to_tier3(ops):
    from fsr_playbooks.llm.tools import TOOL_TIERS
    for t in _SOC_SURFACE:
        TOOL_TIERS.pop(t["name"], None)
    tools = [dict(t) for t in _SOC_SURFACE]
    ops._gate_mutating_mcp_tools(tools, _cfg(["block_indicator", "update_alert_ai_analysis"]))
    # mutations -> tier 3 (approval card), both in TOOL_TIERS and the advertised spec
    assert TOOL_TIERS["mcp_soc__block_indicator"] == 3
    assert TOOL_TIERS["mcp_soc__update_alert_ai_analysis"] == 3
    blk = next(t for t in tools if t["name"] == "mcp_soc__block_indicator")
    assert blk["tier"] == 3 and blk["confirm_mode"] == "approve"
    # reads and non-declared tools untouched (stay auto)
    assert TOOL_TIERS.get("mcp_soc__get_alert", 1) == 1
    assert next(t for t in tools if t["name"] == "mcp_soc__get_alert")["confirm_mode"] == "auto"


def test_never_lowers_an_existing_higher_tier(ops):
    from fsr_playbooks.llm.tools import TOOL_TIERS
    TOOL_TIERS["mcp_soc__block_indicator"] = 4  # e.g. server already marked mutating+
    tools = [{"name": "mcp_soc__block_indicator", "tier": 4, "confirm_mode": "step_up"}]
    ops._gate_mutating_mcp_tools(tools, _cfg(["block_indicator"]))
    assert TOOL_TIERS["mcp_soc__block_indicator"] == 4  # not lowered to 3
    TOOL_TIERS.pop("mcp_soc__block_indicator", None)


def test_curated_and_undeclared_tools_never_touched(ops):
    from fsr_playbooks.llm.tools import TOOL_TIERS
    TOOL_TIERS.pop("run_op", None)
    tools = [dict(t) for t in _SOC_SURFACE]
    ops._gate_mutating_mcp_tools(tools, _cfg(["block_indicator"]))
    # a curated (non-mcp_) tool and an undeclared mcp read stay put
    assert "run_op" not in TOOL_TIERS or TOOL_TIERS["run_op"] != 3
    assert TOOL_TIERS.get("mcp_soc__get_indicators", 1) == 1


def test_tool_listing_reports_the_tier_dispatch_applies(ops):
    """list_agent_tools read the REGISTRY spec, which keeps the per-server tier,
    so a gated block_indicator was listed as tier 1 auto-run."""
    from fsr_playbooks.llm.tools import REGISTRY, TOOL_TIERS, ToolSpec
    name = "mcp_soc__block_indicator"
    REGISTRY[name] = ToolSpec(name=name, description="d",
                              input_schema={"type": "object", "properties": {}},
                              fn=lambda **k: {}, tier=1, confirm_mode="auto")
    TOOL_TIERS[name] = 1
    try:
        ops._gate_mutating_mcp_tools([{"name": name}], {})
        row = ops._agent_tool_row({"name": name}, False)
        assert (row.tier, row.confirm_mode) == (3, "approve")
    finally:
        REGISTRY.pop(name, None)
        TOOL_TIERS.pop(name, None)


_SIEM_WRITES = {"append_incident_comment_by_id", "clear_incident_by_id",
                "update_incident_resolution_by_id", "update_incident_severity_by_id"}


@pytest.mark.parametrize("key,rule", [
    ("fortisiem", {"url": "https://siem.example.com/phoenix/mcp", "tools": "*"}),
    ("siem_prod", {"url": "https://siem.example.com/phoenix/mcp", "tools": "*",
                   "product": "fortisiem"}),
])
def test_fortisiem_write_tools_card_whatever_the_key(ops, key, rule):
    """FortiSIEM's MCP server publishes no annotations; its incident writes ran
    at tier 1 with no approval card on a read_only rule."""
    got = ops._mcp_mutating_tools_by_server({"mcp_allowlist": json.dumps({key: rule})})
    assert _SIEM_WRITES <= got[key]
    assert "get_incident_by_id" not in got[key]


def test_an_unrelated_external_server_gets_no_product_writes(ops):
    got = ops._mcp_mutating_tools_by_server({"mcp_allowlist": json.dumps(
        {"deepwiki": {"url": "https://mcp.example.com/mcp", "tools": "*"}})})
    assert "deepwiki" not in got


# --- MCP intel lookups for the autonomy policy (roadmap S12) -----------------

_FSM = {"fortisiem": {"url": "https://siem.example.com/phoenix/mcp", "tools": "*"}}
_REP = "mcp_fortisiem__get_reputation_by_entity"


def test_fortisiem_reputation_is_a_declared_intel_lookup(ops):
    got = ops._mcp_intel_tools({"mcp_allowlist": json.dumps(_FSM)})
    assert got == {_REP: "fortisiem"}


def test_intel_tools_are_never_guessed_for_an_unknown_server(ops):
    cfg = {"mcp_allowlist": json.dumps(
        {"other": {"url": "https://mcp.example.com/mcp", "tools": "*"}})}
    assert ops._mcp_intel_tools(cfg) == {}
    cfg = {"mcp_allowlist": json.dumps(
        {"other": {"url": "https://mcp.example.com/mcp", "tools": "*",
                   "intel_tools": ["lookup_ip"]}})}
    assert ops._mcp_intel_tools(cfg) == {"mcp_other__lookup_ip": "other"}


def _fsm_result(*verdicts):
    return {"ok": True, "data": [{"ip": "198.51.100.77", "malwareGroups": [
        {"name": "198.51.100.77", "verdict": v, "source": f"feed{i}"}
        for i, v in enumerate(verdicts)]}]}


def test_policy_reader_rates_a_declared_fortisiem_lookup(ops):
    from fsr_playbooks.mcp_server import materializer
    tok = materializer.set_turn_intel_mcp_tools({_REP: "fortisiem"})
    try:
        args = {"params": {"ip": ["198.51.100.77"]}}
        hit = ops._policy_result_reader("mcp:fortisiem", "get_reputation_by_entity",
                                        args, _fsm_result("Malware (IOC found)",
                                                          "Safe (IOC not found)"))
        assert (hit["source"], hit["severity"], hit["indicator"]) == (
            "FortiSIEM", "error", "198.51.100.77")
        assert hit["score_str"] == "1/2 feeds"
        clean = ops._policy_result_reader("mcp:fortisiem", "get_reputation_by_entity",
                                          args, _fsm_result("Safe (IOC not found)"))
        assert clean["severity"] == "ok"
        none = ops._policy_result_reader("mcp:fortisiem", "get_reputation_by_entity",
                                         args, {"ok": True, "data": [{"ip": "x"}]})
        assert none is None  # no feed answered: no rating, never "clean"
    finally:
        materializer._TURN_INTEL_MCP_TOOLS.reset(tok)


def test_policy_reader_ignores_an_undeclared_mcp_tool(ops):
    assert ops._policy_result_reader(
        "mcp:fortisiem", "get_reputation_by_entity",
        {"params": {"ip": ["198.51.100.77"]}}, _fsm_result("Malware (IOC found)")) is None
