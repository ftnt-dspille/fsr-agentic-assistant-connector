"""Regression tests for TRIAGE_BUILD_AUDIT findings A2 and E1.

A2 -- the triage tool slice must NOT advertise any build-only authoring/
mutation tool, so a triage session can't author YAML. (The provider also has
a dispatch-time backstop in anthropic_provider.py for names that slip through.)

E1 -- `_required_params` must dedupe by name; an op with conditional param
groups otherwise lists the same required param once per group.
"""
from __future__ import annotations

import sqlite3

from fsr_playbooks.llm.intents import (
    BUILD_ONLY_TOOLS,
    RUN_VERB_TOOLS,
    TRIAGE_ONLY_TOOLS,
    tools_for_intent,
)
from fsr_playbooks.mcp_server.tools_connector_discovery import _required_params


# --- A2: triage slice excludes every build-only tool ------------------------

def test_build_slice_drops_triage_only_tools():
    # Track C5 -- build excludes the triage-only tools (containment staging
    # emit_action_card, direct run_op, alert/incident investigation). The
    # fsr_soc_triage conftest runs register_triage_tools() at collection, so
    # TRIAGE_ONLY_TOOLS already includes the connector-owned hunt tools
    # (get_record, search_module_records, siem_*, faz_*, fmg_*).
    build = {t["name"] for t in tools_for_intent("build")}
    assert build, "build must advertise a non-empty (scoped) tool slice"
    # RUN_VERB_TOOLS survives BOTH subtractions by design: running a deployed
    # playbook is a whole verb in either intent, and splitting it left build
    # able to START a run it could not RESUME. Subtract it here or this test
    # re-asserts the split that bug came from.
    leaked = build & (TRIAGE_ONLY_TOOLS - RUN_VERB_TOOLS)
    assert not leaked, f"triage-only tools leaked into build: {sorted(leaked)}"
    # Authoring essentials survive.
    for keep in ("validate_yaml", "analyze_playbook", "verify_enhancement",
                 "verify_playbook", "emit_card", "find"):
        assert keep in build, f"build slice dropped essential authoring tool {keep!r}"


def test_triage_slice_excludes_all_build_only_tools():
    names = {t["name"] for t in tools_for_intent("triage")}
    assert names, "triage must advertise a non-empty tool slice"
    leaked = names & (BUILD_ONLY_TOOLS - RUN_VERB_TOOLS)  # see the note above
    assert not leaked, f"build-only tools leaked into triage: {sorted(leaked)}"
    # ...and the run verb really is whole on both sides.
    from fsr_playbooks.llm.tools import CONSOLIDATED_AWAY
    for verb in RUN_VERB_TOOLS:
        if verb in CONSOLIDATED_AWAY:
            continue  # reachable via its union (e.g. search_playbooks -> find)
        assert verb in names, f"triage lost run-verb tool {verb!r}"


def test_triage_slice_keeps_core_discovery_tools():
    names = {t["name"] for t in tools_for_intent("triage")}
    # The triage hunt loop depends on these read/discovery tools.
    for keep in ("run_op", "find", "get_record"):
        assert keep in names, f"triage slice dropped essential tool {keep!r}"


# --- E1: required-params dedupe ---------------------------------------------

def _params_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE operation_params ("
        "connector_name TEXT, op_name TEXT, param_name TEXT, type TEXT, "
        "title TEXT, required TEXT, visible TEXT, ord INTEGER)"
    )
    # `ip` appears in two conditional param groups (parent-gated), the exact
    # block_ip_new shape from the audit -- it must collapse to one row.
    rows = [
        ("fortigate", "block_ip_new", "ip", "text", "IP", "1", "1", 0),
        ("fortigate", "block_ip_new", "ip", "text", "IP", "1", "1", 1),
        ("fortigate", "block_ip_new", "ip_group_name", "text", "Grp", "1", "1", 2),
        ("fortigate", "block_ip_new", "ip_group_name", "text", "Grp", "1", "1", 3),
    ]
    conn.executemany(
        "INSERT INTO operation_params VALUES (?,?,?,?,?,?,?,?)", rows)
    return conn


def test_required_params_dedupes_by_name():
    conn = _params_db()
    try:
        params = _required_params(conn, "fortigate", "block_ip_new")
    finally:
        conn.close()
    names = [p["name"] for p in params]
    assert names == ["ip", "ip_group_name"], names
    assert len(names) == len(set(names)), "duplicates not collapsed"
