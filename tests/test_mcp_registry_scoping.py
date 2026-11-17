"""Native-MCP tools must be scoped to the calling config's allowlist.

`materializer.SERVER_MAP` is a process-global that accumulates every server any
config ever materialized. On a live box that meant a configuration with an EMPTY
allowlist still listed 29 `mcp_*` tools left behind by a sibling config, and the
answer depended on which of the 6 workers replied -- so the observable behavior
tracked worker warmth rather than the config passed in.
"""
import sys
import types

import pytest
import ops_chat
import ops_mcp
import ops_personas
import ops_resume
import ops_tools


@pytest.fixture
def warm_materializer(monkeypatch):
    """A worker whose SERVER_MAP is already warm with two servers."""
    materializer = types.ModuleType("fsr_playbooks.mcp_server.materializer")
    materializer.SERVER_MAP = {
        "mcp_deepwiki__ask_question": ("deepwiki", "ask_question"),
        "mcp_soc__get_alert": ("soc", "get_alert"),
    }
    materializer.ensure_initialized = lambda: None
    materializer.configure = lambda **kw: None

    llm_tools = types.ModuleType("fsr_playbooks.llm.tools")
    llm_tools.TOOL_TIERS = {n: 1 for n in materializer.SERVER_MAP}
    llm_tools.REGISTRY = {
        n: types.SimpleNamespace(fn=lambda **kw: None) for n in materializer.SERVER_MAP
    }

    monkeypatch.setitem(sys.modules,
                        "fsr_playbooks.mcp_server.materializer", materializer)
    monkeypatch.setitem(sys.modules, "fsr_playbooks.llm.tools", llm_tools)
    return materializer


def _ops():
    from connector_fsr_agentic_assistant import operations as ops
    return ops


def test_empty_allowlist_sees_no_native_tools(warm_materializer):
    ops = _ops()
    reg = ops._mcp_registry_for({"mcp_allowlist": ""})
    leaked = sorted(n for n in reg if n.startswith("mcp_"))
    assert leaked == [], f"sibling config's native tools leaked: {leaked}"


def test_allowlist_scopes_to_its_own_servers(warm_materializer):
    ops = _ops()
    reg = ops._mcp_registry_for(
        {"mcp_allowlist": '{"deepwiki": {"tools": "*", "tier": "read_only"}}'})
    native = sorted(n for n in reg if n.startswith("mcp_"))
    assert native == ["mcp_deepwiki__ask_question"], native


def test_full_allowlist_sees_both(warm_materializer):
    ops = _ops()
    reg = ops._mcp_registry_for({
        "mcp_allowlist": '{"deepwiki": {"tools": "*", "tier": "read_only"}, '
                         '"soc": {"tools": "*", "tier": "read_only"}}'})
    native = sorted(n for n in reg if n.startswith("mcp_"))
    assert native == ["mcp_deepwiki__ask_question", "mcp_soc__get_alert"], native


# --- #62: the diagnostic op's own report ------------------------------------
#
# `_mcp_registry_for` fixed what a playbook can CALL. `list_mcp_servers` builds
# `servers[]` from `anthropic_tools()` -- the same process-global surface -- so
# it kept reporting the union and contradicted its own `allowlist_keys`.

_SURFACE = [
    {"name": "get_record"},                        # curated: every config has it
    {"name": "mcp_deepwiki__ask_question"},
    {"name": "mcp_soc__get_alert"},
]


def test_scope_tools_to_config_drops_other_configs_servers():
    ops = _ops()
    kept = [t["name"] for t in ops._scope_tools_to_config(_SURFACE, {"deepwiki"})]
    assert kept == ["get_record", "mcp_deepwiki__ask_question"]


def test_scope_tools_to_config_empty_allowlist_keeps_only_curated():
    """An empty allowlist means no MCP -- but the connector's own tools are not
    a materialization and stay."""
    ops = _ops()
    kept = [t["name"] for t in ops._scope_tools_to_config(_SURFACE, set())]
    assert kept == ["get_record"]


def test_list_mcp_servers_does_not_contradict_its_own_allowlist(monkeypatch):
    """The live symptom: allowlist_keys=[] alongside servers=[deepwiki, soc]."""
    ops = _ops()
    monkeypatch.setattr(ops_chat, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_mcp, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_resume, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_tools, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_mcp, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_personas, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_resume, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_tools, "_resolve_profile", lambda *a, **k: None)

    seen = {}

    def _inventory(tools, module, **kw):
        seen["tools"] = [t["name"] for t in tools]
        return []

    triage = types.ModuleType("fsr_soc_triage.triage_sources")
    triage.mcp_inventory = _inventory
    triage.effective_surfaced_servers = lambda *a, **k: None
    llm_tools = types.ModuleType("fsr_playbooks.llm.tools")
    llm_tools.anthropic_tools = lambda: list(_SURFACE)
    monkeypatch.setitem(sys.modules, "fsr_soc_triage.triage_sources", triage)
    monkeypatch.setitem(sys.modules, "fsr_playbooks.llm.tools", llm_tools)

    out = ops.list_mcp_servers({"mcp_allowlist": ""}, {})
    assert out["allowlist_keys"] == []
    assert out["servers"] == []
    assert seen["tools"] == ["get_record"], seen["tools"]


def test_list_agent_tools_reports_only_this_configs_native_tools(monkeypatch):
    """Live on .159 a 3-server config and an empty-allowlist one both returned
    66 tools, byte-identical, against 20 curated cold -- `_advertised_tools`
    reads the same process-global surface. Scoped on the REPORT, not in
    `_advertised_tools`, which is the turn surface."""
    ops = _ops()
    monkeypatch.setattr(ops_chat, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_mcp, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_resume, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_tools, "_apply_mcp_allowlist", lambda cfg: None)
    monkeypatch.setattr(ops_mcp, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_personas, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_resume, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_tools, "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(ops_chat, "_advertised_tools",
                        lambda *a, **k: list(_SURFACE))
    monkeypatch.setattr(ops_resume, "_advertised_tools",
                        lambda *a, **k: list(_SURFACE))
    monkeypatch.setattr(ops_tools, "_advertised_tools",
                        lambda *a, **k: list(_SURFACE))

    def _names(cfg):
        out = ops.list_agent_tools(cfg, {"intent": "triage", "include_mcp": True})
        return [t.name if hasattr(t, "name") else t["name"]
                for t in out["surfaces"][0]["tools"]]

    assert _names({"mcp_allowlist": ""}) == ["get_record"]
    assert _names({"mcp_allowlist":
                   '{"deepwiki": {"tools": "*", "tier": "read_only"}}'}) == [
        "get_record", "mcp_deepwiki__ask_question"]
