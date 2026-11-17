"""A cassette turn leaves the process exactly as it found it.

`local_turn` swaps module globals (the FSR client seam, run_op, the registry
specs, the connector inventory, the MCP seams, the fake provider, env vars) to
serve a fixture world. Every one of those writes must be undone when the turn
returns OR raises, or the next test in the same process inherits the world.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_REPO = _SCRIPTS.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import local_turn as lt  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_materialized_mcp_tools():
    """A world with MCP servers materializes its tools into the worker-wide
    registry. Graders read them after the turn, so the turn leaves them; this
    file resets them, as test_local_turn_mcp_world does, so later files do not
    see FortiSIEM tools they never configured."""
    yield
    from fsr_playbooks.mcp_server import materializer
    materializer.reset()


MOUNT = {"module": "alerts", "iri": "/api/3/alerts/5c1e2a90-1b7d-4c55-9a3e-7d0c2b1a4e01"}

_ENV_KEYS = ("FSR_BASE_URL", "FSR_USERNAME", "FSR_PASSWORD", "FSR_API_KEY",
             "FSRPB_DB", "FSR_REFERENCE_DB", "FSR_PLAYBOOK_BUILDER_DB")


def _snapshot() -> tuple[dict, dict]:
    """(identity-compared objects, value-compared plain state).

    Objects are compared with `is`: a registry spec is a dataclass, so `==`
    would hide a swapped-in replacement that happens to be equal by value.
    """
    lt._bootstrap()
    import fsr_playbooks.execution_api as ea
    import fsr_playbooks.mcp_server.tools_connector_discovery as tcd
    import fsr_playbooks.mcp_server.tools_execution as te
    import fsr_playbooks.mcp_server._shared as shared
    import fsr_soc_triage._live_mcp as live_mcp
    import connector_fsr_agentic_assistant.ops_chat as ops_chat
    import connector_fsr_agentic_assistant.ops_mcp as ops_mcp
    import connector_fsr_agentic_assistant.operations as ops
    import probes._env as penv
    from fsr_playbooks.llm.tools import REGISTRY

    objs: dict = {
        "shared._live_client": shared._live_client,
        "shared._invalidate_live_client": shared._invalidate_live_client,
        "probes._env.get_client": penv.get_client,
        "probes._env.get_config": penv.get_config,
        "tools_execution.run_op": te.run_op,
        "execution_api.run_op": ea.run_op,
        "REGISTRY['run_op']": REGISTRY["run_op"],
        "REGISTRY['find_enrichment_actions']": REGISTRY["find_enrichment_actions"],
        "REGISTRY['list_configured_connectors']": REGISTRY["list_configured_connectors"],
        "REGISTRY['connector_health']": REGISTRY["connector_health"],
        "tools_connector_discovery.list_configured_connectors": tcd.list_configured_connectors,
        "tools_connector_discovery._healthcheck_many": tcd._healthcheck_many,
        "ops_chat._advertised_tools": ops_chat._advertised_tools,
        "_live_mcp.build_client": live_mcp.build_client,
        "ops_mcp._read_mcp_configurations": ops_mcp._read_mcp_configurations,
    }
    prefix = ops.__name__.rpartition(".")[0]
    mod_prefix = (f"{prefix}." if prefix else "") + "ops_"
    for key, mod in list(sys.modules.items()):
        if mod is ops or (mod is not None and key.startswith(mod_prefix)
                          and "_build_provider" in vars(mod)):
            objs[f"{key}._build_provider"] = vars(mod)["_build_provider"]

    plain = {
        "env": {k: os.environ.get(k) for k in _ENV_KEYS},
        "live_client_cache": dict(shared._LIVE_CLIENT_CACHE),
        "ops_mcp._platform_servers_cache": ops_mcp._platform_servers_cache,
        "ops._PROFILE_CACHE": dict(ops._PROFILE_CACHE),
        "ops._PROFILE_NEG_TS": dict(ops._PROFILE_NEG_TS),
    }
    return objs, plain


def _changed(before: tuple[dict, dict], after: tuple[dict, dict]) -> list[str]:
    b_objs, b_plain = before
    a_objs, a_plain = after
    out = [f"{k} (identity)" for k in b_objs
           if k not in a_objs or a_objs[k] is not b_objs[k]]
    out += [f"{k} (value)" for k in b_plain if a_plain.get(k) != b_plain[k]]
    out += [f"{k} (added)" for k in a_objs if k not in b_objs]
    return out


def _turn(records=None, llm="fake"):
    return lt.local_turn(messages=[{"role": "user", "content": "triage"}],
                         intent="triage", module="alerts", entity=dict(MOUNT),
                         llm=llm, persona="none", records=records)


@pytest.mark.parametrize("records", [None, "ka_mcp_c2_beacon"])
def test_a_cassette_turn_restores_every_global_it_patched(records):
    before = _snapshot()
    _turn(records)
    assert _changed(before, _snapshot()) == []


def test_a_turn_that_raises_still_restores_its_patches():
    before = _snapshot()
    with pytest.raises(SystemExit):
        _turn(llm="no-such-llm")
    assert _changed(before, _snapshot()) == []
