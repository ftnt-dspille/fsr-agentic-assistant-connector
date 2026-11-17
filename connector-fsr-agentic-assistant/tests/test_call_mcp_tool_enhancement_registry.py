"""`call_mcp_tool`'s registry (`ops_mcp._get_mcp_registry`) must expose the
read-only `verify_enhancement` and must NOT expose the authoring `edit_playbook`.

Both are `@mcp.tool`s in `fsr_playbooks.mcp_server.tools_enhancement`, which is
NOT one of the library modules `_get_mcp_registry` scans, and the registry is
cached on the worker's first call. So whether `call_mcp_tool` could reach either
one depended on whether `tools_enhancement` happened to be imported before that
first call -- a worker-order accident. The card-smoke `enhancement_offer` path
(which drives `verify_enhancement` through `call_mcp_tool`) hit that: on the
smoke worker the tool was absent, so it refused with ``unknown_tool``. And on a
worker where it WAS imported, the `_tool_manager` scan would silently expose the
mutating `edit_playbook` through `call_mcp_tool`, which bypasses the approval
card. These tests pin both directions deterministically by controlling the
FastMCP tool manager the scan reads.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_HAVE_FW = importlib.util.find_spec("fsr_playbooks") is not None


def _have_real_tool_manager() -> bool:
    """True only when the real FastMCP (with a `_tool_manager`) is wired.

    Both tests manipulate that manager. On the pinned-wheel CI mirror the
    framework wheel imports the mcp 1.x FastMCP API, which mcp 2.x no longer
    exposes, so `_shared.mcp` is the `_FallbackFastMCP` with no tool manager --
    there is nothing to exercise there."""
    try:
        from fsr_playbooks.mcp_server import _shared
        return hasattr(getattr(_shared, "mcp", None), "_tool_manager")
    except Exception:  # noqa: BLE001
        return False


pytestmark = [
    pytest.mark.skipif(
        not _HAVE_FW, reason="fsr_playbooks framework not importable in this env"),
    pytest.mark.skipif(
        not _have_real_tool_manager(),
        reason="pinned-wheel CI mirror: no real FastMCP tool manager"),
]


@pytest.fixture()
def ops_mcp():
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
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.ops_mcp", ROOT / "ops_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.ops_mcp"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mcp_tool_manager():
    from fsr_playbooks.mcp_server import _shared
    return _shared.mcp._tool_manager


def test_verify_enhancement_is_reachable_even_if_tool_manager_lacks_it(
        ops_mcp, monkeypatch):
    """Isolates the explicit import+add: with the FastMCP manager emptied, the
    ONLY way `verify_enhancement` reaches the registry is `_get_mcp_registry`
    importing tools_enhancement by name. Red without that add."""
    monkeypatch.setattr(_mcp_tool_manager(), "_tools", {}, raising=False)
    monkeypatch.setattr(ops_mcp, "_MCP_REGISTRY", None, raising=False)

    registry = ops_mcp._get_mcp_registry()

    assert "verify_enhancement" in registry
    assert callable(registry["verify_enhancement"])


def test_edit_playbook_is_not_reachable_even_if_tool_manager_has_it(
        ops_mcp, monkeypatch):
    """Isolates the drop: inject `edit_playbook` into the FastMCP manager (the
    state on a worker that imported tools_enhancement), and the `_tool_manager`
    scan would surface it through call_mcp_tool. The drop must remove it. Red
    without the drop."""
    fake = types.SimpleNamespace(fn=lambda **kw: {"ok": True}, func=None)
    tm = _mcp_tool_manager()
    monkeypatch.setitem(tm._tools, "edit_playbook", fake)
    monkeypatch.setattr(ops_mcp, "_MCP_REGISTRY", None, raising=False)

    registry = ops_mcp._get_mcp_registry()

    assert "edit_playbook" not in registry
