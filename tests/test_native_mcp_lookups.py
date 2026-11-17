"""Native MCP record lookups under the built-in HMAC identity (tracker A1.6).

- the on-box client must not use httpx's 5s default read timeout;
- HMAC configs advertise only the modules tools that work under HMAC;
- transport errors reach the caller as the leaf error, not a TaskGroup group;
- op bodies can import fsr_soc_triage (probe_native_mcp).
"""
import os
import sys

import pytest

import fsr_soc_triage._live_mcp as L
import ops_mcp


def test_session_call_uses_long_timeouts(monkeypatch):
    import httpx2
    seen = {}

    class Rec(httpx2.AsyncClient):
        def __init__(self, *a, **kw):
            seen.update(kw)
            super().__init__(*a, **kw)

    def boom(url, http_client=None):
        raise RuntimeError("stop here")

    monkeypatch.setattr(httpx2, "AsyncClient", Rec)
    monkeypatch.setattr(L, "_streamablehttp_client", lambda: boom)
    with pytest.raises(RuntimeError):
        L._CrudhubMCP._session_call("http://x/mcp/modules/", {}, lambda s: None)
    t = seen["timeout"]
    assert t.read == 300.0 and t.connect == 30.0


def _group():
    leaf = RuntimeError("SSE stream ended without a response")
    return ExceptionGroup("unhandled errors in a TaskGroup", [leaf]), leaf  # noqa: F821


def test_run_unwraps_exception_group_to_leaf(monkeypatch):
    grp, leaf = _group()
    mcp = L._CrudhubMCP(api_key="k")

    def raising(url, headers, coro, verify=False):
        raise grp

    monkeypatch.setattr(L._CrudhubMCP, "_session_call", staticmethod(raising))
    with pytest.raises(RuntimeError) as ei:
        mcp._run("modules", lambda s: None)
    assert ei.value is leaf
    assert "TaskGroup" not in str(ei.value)


def test_leaf_exception_follows_cause():
    inner = ValueError("401 Unauthorized")
    try:
        try:
            raise inner
        except ValueError as e:
            raise RuntimeError("wrapper") from e
    except RuntimeError as outer:
        assert L.leaf_exception(outer) is inner


HMAC = {}
KEYED = {"soar_api_key": "k"}


def _al(monkeypatch, cfg):
    monkeypatch.setattr(ops_mcp, "_platform_mcp_servers",
                        lambda: ["modules", "soc", "playbooks"])
    return ops_mcp._parse_mcp_allowlist(cfg)


def test_hmac_advertises_only_safe_modules_tools(monkeypatch):
    al = _al(monkeypatch, HMAC)
    assert al["modules"]["tools"] == ["get_modules", "get_module_schema"]
    assert "query_records" not in al["modules"]["tools"]
    assert "soc" not in al and "playbooks" not in al


def test_forwardable_identity_keeps_all_modules_tools(monkeypatch):
    al = _al(monkeypatch, KEYED)
    assert al["modules"]["tools"] == "*"


def test_hmac_narrows_selected_servers_mode(monkeypatch):
    cfg = {"mcp_access": "Selected servers", "mcp_servers": ["modules"]}
    monkeypatch.setattr(ops_mcp, "_platform_mcp_servers", lambda: [])
    al = ops_mcp._parse_mcp_allowlist(cfg)
    assert al["modules"]["tools"] == ["get_modules", "get_module_schema"]


def test_ops_common_puts_connector_dir_on_path(monkeypatch):
    import ops_common
    own = os.path.dirname(os.path.abspath(ops_common.__file__))
    monkeypatch.setattr(sys, "path", [p for p in sys.path if os.path.abspath(p) != own])
    # The bare/dotted names are aliases of one module the test loader will not
    # re-execute, so execute the source once more under a throwaway name.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "connector_fsr_agentic_assistant._ops_common_fresh", ops_common.__file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert own in sys.path
