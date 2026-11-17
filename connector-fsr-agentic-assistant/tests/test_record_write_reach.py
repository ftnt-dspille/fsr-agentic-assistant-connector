"""The Agentic assistant must be able to act on the record it is triaging.

`create_record` / `update_record` were listed in BOTH drop sets — the
connector's ``_BUILD_ONLY_TOOLS`` (excluding them from *triage*) and, via
``registry.register_record_tools()``, the framework's ``TRIAGE_ONLY_TOOLS``
(excluding them from *build*). Net: unreachable in every non-persona session,
so a plain triage turn had no tool that could edit the alert in front of it.

LIVE REPRO (session sess-v6uv6x15, an alert drawer): "change the type of the
alert" produced ZERO tool calls — the model reinterpreted it as playbook
authoring because nothing else was on offer.

Two independent things have to hold for the capability to actually exist, and
each failed on its own before:

  1. REACH   — the tool is advertised in the triage slice.
  2. SCOPE   — `_write_gate` permits the write. It refuses whenever no persona
               is active, so reach alone was a no-op: the model would call the
               tool and get `write_not_permitted`.

Safety is unchanged and is NOT provided by tool absence: both tools are tier 3
with confirm_mode="approve", so every write returns a pending_approval card.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fsr_playbooks") is None,
    reason="fsr_playbooks framework not importable in this env",
)

RECORD_TOOLS = ("create_record", "update_record")


@pytest.fixture(scope="module")
def ops():
    """Load operations.py off-platform (same bootstrap as test_list_agent_tools)."""
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_rw")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_rw"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_rw.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_rw.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_rw.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _slices(ops):
    from fsr_soc_triage.registry import (
        register_record_tools, register_triage_tools,
    )
    register_triage_tools()   # idempotent
    register_record_tools()   # idempotent
    return (
        {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="triage"), include_mcp=False)},
        {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build"), include_mcp=False)},
    )


# ── 1. reach ────────────────────────────────────────────────────────────────

def test_record_writes_are_reachable_in_triage(ops):
    triage, _ = _slices(ops)
    missing = [n for n in RECORD_TOOLS if n not in triage]
    assert not missing, (
        f"record-write tools absent from the triage slice: {missing} — the "
        "assistant cannot act on the record it is triaging"
    )


def test_record_writes_reach_build_gated_not_sliced(ops):
    # Phase 3 full-surface: the record writes are advertised in build too; what
    # keeps authoring from mutating live records is the tier-3 approve gate +
    # _write_gate (may_write), never slice absence.
    _, build = _slices(ops)
    missing = [n for n in RECORD_TOOLS if n not in build]
    assert not missing, f"record-write tools absent from build: {missing}"
    from fsr_playbooks.llm.tools import REGISTRY
    for n in RECORD_TOOLS:
        spec = REGISTRY.get(n)
        if spec is not None:
            assert spec.tier == 3 and spec.confirm_mode == "approve", n


# ── 2. scope ────────────────────────────────────────────────────────────────

def test_host_record_module_is_writable_without_a_persona(ops):
    """A mounted record's own module is in scope; nothing else is."""
    prof = ops._host_record_write_profile("alerts")
    assert prof is not None
    assert prof.may_write_module("alerts") is True
    assert prof.may_write_module("incidents") is False, (
        "the fallback profile must scope writes to the mounted record's module "
        "only — it is reach, not blanket write access"
    )


def test_off_record_session_stays_read_only(ops):
    """No mounted record ⇒ no write scope (dashboard / module-list mount)."""
    for empty in (None, "", "   ", 123):
        assert ops._host_record_write_profile(empty) is None


def test_fallback_profile_declares_no_tool_allowlist(ops):
    """It carries may_write ONLY.

    A `tools.allow` here would override the built-in intent slice entirely
    (see _tools_for_persona: an allowlist WINS), silently narrowing every
    triage session to whatever this synthetic profile happened to list.
    """
    prof = ops._host_record_write_profile("alerts")
    assert not prof.tools_allow if hasattr(prof, "tools_allow") else True
    assert prof.may_write == ["alerts"]


# ── 3. the HITL gate that makes the above safe ──────────────────────────────

def test_record_writes_remain_approval_carded():
    from fsr_playbooks.llm.tools import REGISTRY
    from fsr_soc_triage.registry import register_record_tools
    register_record_tools()
    for name in RECORD_TOOLS:
        spec = REGISTRY[name]
        assert spec.tier == 3, f"{name} must stay tier 3 (approval-gated)"
        assert spec.confirm_mode == "approve", (
            f"{name} must stay confirm_mode='approve' — reach was granted on "
            "the explicit basis that every write is analyst-approved"
        )
