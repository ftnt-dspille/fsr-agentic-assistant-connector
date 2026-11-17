"""The sweep's own oracle: prove every invariant can FAIL.

A checker that cannot go red manufactures confidence -- it reports PASS whether
or not the property holds, and a false clean bill costs more than a missing
check (the T1 graders carry the same rule, `test_t1_scenario_graders.py`). So
each invariant here is exercised twice: once against the real registry, and once
against a deliberately broken tool that must trip it.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts import tool_sweep as TS  # noqa: E402


@pytest.fixture(scope="module")
def registry():
    # Undone after the module: a socket guard and FSRPB_DB left behind broke
    # every later test that opens a Unix socket or reads the reference DB.
    import os
    old_db = os.environ.get("FSRPB_DB")
    os.environ["FSRPB_DB"] = TS._isolated_db()
    REGISTRY, slices = TS.load_registry()
    uninstall = TS.install_hermetic_guard()
    yield REGISTRY, slices
    uninstall()
    if old_db is None:
        os.environ.pop("FSRPB_DB", None)
    else:
        os.environ["FSRPB_DB"] = old_db


def test_the_guard_lets_unix_sockets_through_and_blocks_remote_hosts():
    import socket
    real = socket.socket.connect
    uninstall = TS.install_hermetic_guard()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with pytest.raises(OSError) as e:      # no such path: refused by the OS,
            s.connect("/nonexistent/hermetic-guard-probe.sock")
        assert "SWEEP-HERMETIC" not in str(e.value)   # not by the guard
        s.close()
        r = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(OSError, match="SWEEP-HERMETIC"):
            r.connect(("192.0.2.1", 443))
        r.close()
    finally:
        uninstall()
    assert socket.socket.connect is real


def test_the_guard_caps_sleeps_so_the_sweep_never_waits_on_retry_or_poll_pacing():
    """54s of this module was real time.sleep (a 0.5s transient retry, a 1s x 9 SIEM
    poll) on every probe. The sweep grades contracts, not pacing."""
    import time
    real = time.sleep
    uninstall = TS.install_hermetic_guard()
    try:
        t0 = time.monotonic()
        time.sleep(5)
        assert time.monotonic() - t0 < 1.0
    finally:
        uninstall()
    assert time.sleep is real


@pytest.fixture(scope="module")
def T():
    from fsr_playbooks.llm import tools as _T
    return _T


def _findings(name, spec, T_, floor=3):
    return TS.check_tool(name, spec, T_, floor)


# --------------------------------------------------------------------------
# green: the real registry satisfies the contract
# --------------------------------------------------------------------------
def test_real_registry_passes_contract_invariants(registry, T):
    REGISTRY, _ = registry
    bad = []
    for name, spec in REGISTRY.items():
        bad.extend(_findings(name, spec, T))
    assert bad == [], f"contract violations in the real registry: {bad}"


# --------------------------------------------------------------------------
# red: each invariant trips on a deliberately broken tool
# --------------------------------------------------------------------------
def test_N_trips_when_dispatch_raises(registry, T):
    """A tool whose fn raises something dispatch does not catch.

    Uses a tool with NO required arguments on purpose. N probes with empty and
    junk args, so for a tool like compile_yaml (required: yaml_text) the call
    bounces at argument validation and never reaches fn -- the invariant would
    be vacuous there. 49 of 61 tools do reach fn on empty args; the remaining 12
    are covered by the behavioural cases, which pass real arguments and fail the
    case on any raise.
    """
    REGISTRY, _ = registry
    name = "build_playbook_from_trace"  # required: []
    original = REGISTRY[name]

    class _SweepBoom(BaseException):
        """Derives from BaseException, so dispatch's `except Exception` misses it.

        Deliberately not KeyboardInterrupt/SystemExit: pytest treats those as
        session aborts, which would end the run rather than fail the assertion.
        """

    def _explode(*a, **k):
        raise _SweepBoom("not caught by dispatch's except Exception")

    REGISTRY[name] = dataclasses.replace(original, fn=_explode)
    try:
        found = _findings(name, REGISTRY[name], T)
    finally:
        REGISTRY[name] = original
    assert any(f["invariant"] == "N" for f in found), \
        f"N did not trip on a raising tool: {found}"


def test_G_trips_when_a_gated_tool_executes(registry, T):
    """The gate-bypass class: a tier-3 tool that runs without approval.

    Simulated by lowering the approval floor above the tool's tier, so the
    sweep's own tripwire is what decides -- exactly the mechanism that would
    catch a `validating()` wrapper being dropped from run_op.
    """
    REGISTRY, _ = registry
    name = "create_record"  # tier 3
    spec = REGISTRY[name]
    # floor=99 => dispatch never gates, so the fn executes; the sweep is told
    # floor=0 => it EXPECTS a gate. The disagreement is the defect being modelled.
    # The floor dispatch reads lives in authorization (tools re-exports it).
    import fsr_playbooks.llm.authorization as real
    orig_floor = real._approval_floor
    real._approval_floor = lambda: 99
    try:
        found = _findings(name, spec, T, floor=0)
    finally:
        real._approval_floor = orig_floor
    assert any(f["invariant"] == "G" for f in found), \
        f"G did not trip when a gated tool executed: {found}"


def test_I_trips_when_injection_is_honoured(registry, T):
    """A registry whose dispatch accepts wire-supplied `_approved`."""
    class _Permissive:
        REGISTRY = registry[0]

        @staticmethod
        def dispatch(name, args, **kw):
            # Honours _approved instead of rejecting it.
            return {"ok": True}

        @staticmethod
        def _resolve_tier(name, args):
            return 0

    found = _findings("compile_yaml", registry[0]["compile_yaml"], _Permissive)
    assert any(f["invariant"] == "I" for f in found), \
        f"I did not trip on an injection-honouring dispatch: {found}"


def test_R_trips_on_unknown_tool(registry):
    """dispatch answering 'unknown tool' for a registered name."""
    class _Amnesiac:
        @staticmethod
        def dispatch(name, args, **kw):
            return {"error": f"unknown tool: {name}"}

        @staticmethod
        def _resolve_tier(name, args):
            return 0

    found = _findings("compile_yaml", registry[0]["compile_yaml"], _Amnesiac)
    assert any(f["invariant"] == "R" for f in found), \
        f"R did not trip on an unknown-tool answer: {found}"


def test_S_trips_when_schema_requires_a_nonexistent_param(registry, T):
    """input_schema advertising an argument the function cannot accept."""
    REGISTRY, _ = registry
    name = "compile_yaml"
    original = REGISTRY[name]
    broken_schema = dict(original.input_schema or {})
    broken_schema["required"] = ["no_such_parameter_at_all"]
    REGISTRY[name] = dataclasses.replace(original, input_schema=broken_schema)
    try:
        found = _findings(name, REGISTRY[name], T)
    finally:
        REGISTRY[name] = original
    assert any(f["invariant"] == "S" for f in found), \
        f"S did not trip on an impossible required arg: {found}"


def _dispatching(answer, registry):
    class _Fake:
        REGISTRY = registry[0]

        @staticmethod
        def dispatch(name, args, **kw):
            return answer

        @staticmethod
        def _resolve_tier(name, args):
            return 0
    return _Fake


def test_P_trips_on_a_bare_unreachable_source_error(registry):
    """A transport failure answered as a bare error, not a partial result."""
    bare = {"ok": False, "code": "transport_error", "message": "GET x failed"}
    found = _findings("compile_yaml", registry[0]["compile_yaml"], _dispatching(bare, registry))
    assert any(f["invariant"] == "P" and "unreachable-source" in f["detail"]
               for f in found), found


def test_P_accepts_a_partial_unreachable_source_answer(registry):
    from fsr_playbooks.llm.tool_result import unreachable_partial
    ok = unreachable_partial("the data", "GET", "https://x/y", RuntimeError("down"))
    found = _findings("compile_yaml", registry[0]["compile_yaml"], _dispatching(ok, registry))
    assert not [f for f in found if f["invariant"] == "P"], found


def test_unreachable_source_probe_restores_the_world(registry):
    from fsr_playbooks import execution_api
    from fsr_playbooks.mcp_server import _shared
    before = (_shared._LIVE_CLIENT_CACHE.get("client"), execution_api.run_op)
    with TS._unreachable_source():
        assert _shared._live_client().base_url
        assert execution_api.run_op("c", "o", {})["code"] == "transport_failed"
    assert (_shared._LIVE_CLIENT_CACHE.get("client"), execution_api.run_op) == before


# --------------------------------------------------------------------------
# the expect-checker itself
# --------------------------------------------------------------------------
@pytest.mark.parametrize("result,expect,should_fail", [
    ({"ok": True}, {"ok": True}, False),
    ({"ok": False}, {"ok": True}, True),
    ({"ok": True}, {"has_keys": ["missing"]}, True),
    ({"ok": True, "a": 1}, {"has_keys": ["a"]}, False),
    ([1, 2, 3], {"min_count": 2}, False),
    ([1], {"min_count": 2}, True),
    ([{"name": "upper"}], {"contains": ["upper"]}, False),
    ([{"name": "lower"}], {"contains": ["upper"]}, True),
    ({"code": "bad_field"}, {"code": "bad_field"}, False),
    ({"code": "other"}, {"code": "bad_field"}, True),
])
def test_expect_checker_can_pass_and_fail(result, expect, should_fail):
    fails = TS._check_expect(result, expect)
    assert bool(fails) is should_fail, f"{result} vs {expect} -> {fails}"


# --------------------------------------------------------------------------
# coverage accounting must not silently inflate
# --------------------------------------------------------------------------
def test_box_required_tools_are_real_tool_names(registry):
    """A typo in box_required would hide a tool from the coverage denominator."""
    import json
    REGISTRY, _ = registry
    blob = json.loads((_ROOT / "scripts" / "tool_sweep_cases.json").read_text())
    unknown = sorted(set(blob["box_required"]) - set(REGISTRY))
    assert unknown == [], \
        f"box_required names no longer in the registry (typo or removal): {unknown}"


def test_every_case_targets_a_registered_tool(registry):
    import json
    REGISTRY, _ = registry
    blob = json.loads((_ROOT / "scripts" / "tool_sweep_cases.json").read_text())
    unknown = sorted({c["tool"] for c in blob["cases"]} - set(REGISTRY))
    assert unknown == [], f"cases target unregistered tools: {unknown}"


# --------------------------------------------------------------------------
# slice reachability (invariants A and F)
#
# These live in the sweep's main(), not in check_tool(), so the oracle above
# does NOT cover them -- and they are the two that caught a real, shipped
# defect. Pin them in the SUITE so the regression cannot return quietly between
# manual `make tool-sweep` runs.
# --------------------------------------------------------------------------
def test_no_registered_tool_is_orphaned(registry):
    """A tool in no slice is dead code: no model can call it in any intent.

    `run_playbook` reached that state by being in BUILD_ONLY_TOOLS (framework)
    AND added to TRIAGE_ONLY_TOOLS (connector) -- each exclusion defensible
    alone, together fatal. Parallel name lists drift; assert the relationship.
    """
    REGISTRY, slices = registry
    advertised = set().union(*slices.values())
    # Phase 1 retirement: CONSOLIDATED_AWAY names stay registered (dispatch,
    # approval resumes, persona-allowlist translation) but are deliberately
    # unadvertised -- the model reaches each through its union tool. They are
    # not orphans PROVIDED the mapping covers them and every union is itself
    # advertised.
    from connector_fsr_agentic_assistant.operations import _retired_to_union
    from fsr_playbooks.llm.tools import CONSOLIDATED_AWAY
    _RETIRED_TO_UNION = _retired_to_union()
    assert set(CONSOLIDATED_AWAY) <= set(_RETIRED_TO_UNION), (
        "a CONSOLIDATED_AWAY name has no union mapping -- it is unreachable "
        "from a persona allowlist")
    unions_missing = sorted(set(_RETIRED_TO_UNION.values()) - advertised)
    assert unions_missing == [], (
        f"union tools advertised in NO slice: {unions_missing}")
    # Host retirements (the connector's merged hunt helpers) are reachable
    # through their union exactly like CONSOLIDATED_AWAY names.
    # Tools a host registered but withholds from the model until it opts in
    # (fsr_playbooks.llm.tools.withhold_from_model, e.g. the agentic toolkit's
    # workflows): not orphans, gated.
    from fsr_playbooks.llm.tools import _HOST_WITHHELD
    orphans = sorted(set(REGISTRY) - advertised - set(_RETIRED_TO_UNION)
                     - set(_HOST_WITHHELD))
    assert orphans == [], (
        f"registered but advertised in NO slice, so unreachable: {orphans}")


def test_run_playbook_is_reachable_from_triage(registry):
    """Running an existing playbook is a first-class TRIAGE action.

    An analyst working an alert fires the block-IP playbook, an investigation
    playbook, or one their own team wrote. Authoring is build-only; RUNNING is
    not.
    """
    _, slices = registry
    assert "run_playbook" in slices["triage"], (
        "run_playbook missing from the triage slice -- chat cannot drive an "
        "existing playbook from an alert")


def test_run_playbook_is_reachable_from_build(registry):
    """"Run the playbook I just made" is a normal build-turn follow-up, so the
    build slice must offer a tool that can land it. This used to be asserted
    because Lever 2 narrowed the slice to run_playbook alone; that lever is
    gone, which makes the tool's presence in the un-narrowed slice the ONLY
    thing making a run request actionable in build."""
    _, slices = registry
    assert "run_playbook" in slices["build"], (
        "run_playbook missing from the build slice -- a 'run it' request in "
        "build has no tool to land on")


def test_run_playbook_still_requires_approval(registry):
    """Reachability must not have cost the gate.

    run_playbook is tier -1 (dynamic) and must resolve to tier 3 -- an approval
    card -- for an ordinary playbook. Only a host-designated auto-runnable one
    drops to tier 2. Widening the slice while silently lowering the tier would
    let triage fire live playbooks with no human in the loop.
    """
    from fsr_playbooks.llm import tools as T
    tier = T._resolve_tier("run_playbook", {"playbook": "some-playbook"})
    assert tier == 3, f"run_playbook resolved to tier {tier}, expected 3 (carded)"
