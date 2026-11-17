"""The prompt must not tell the model to call a tool it cannot see.

WHY THIS EXISTS. The framework consolidated seven card emitters into
``emit_card(card_type=...)`` and the ``find_*`` family into ``find(kind=...)``,
keeping the old names in the REGISTRY -- they still dispatch -- but dropping
them from what is ADVERTISED (``CONSOLIDATED_AWAY``). The connector's triage
prompt kept naming the retired ones: 374 mentions across ten tools, including
the hard rule "Mutating actions always go through `emit_action_card`. No
exceptions."

The live symptom was a containment turn that ended in
"I have prepared the action card ... please review and approve" with ZERO cards
rendered. The model was obeying an instruction it could not carry out: told to
call a tool absent from its surface, it narrated the call instead. Nothing
failed loudly -- the turn returned ok, and the widget had nothing to show.

The tests that existed asserted the prompt MENTIONED `emit_action_card`, which
stayed true the entire time the tool was uncallable. Mentioning is not the
property that matters; being callable is. This test asserts that one.
"""
from __future__ import annotations

import re

import pytest

from fsr_soc_triage import prompt_assembly

CONSOLIDATED_AWAY = pytest.importorskip(
    "fsr_playbooks.llm.tools").CONSOLIDATED_AWAY

# Backticked identifiers, with or without a trailing call: `emit_action_card`
# and `find_containment_actions(target_type=ip)` both reduce to the bare name.
_NAME = re.compile(r"`([a-z_][a-z0-9_]*)\s*\(?")


def _named_tools(text: str) -> set[str]:
    return set(_NAME.findall(text))


def _all_prompt_text() -> dict[str, str]:
    """Every model-facing prompt surface this module can assemble."""
    out = {"triage": prompt_assembly.assemble_triage_prompt()}
    for mode in ("hunt", "contain"):
        try:
            out[f"triage[{mode}]"] = prompt_assembly.assemble_triage_prompt(mode=mode)
        except TypeError:  # pragma: no cover - signature drift
            pass
    return out


@pytest.mark.parametrize("mode", ["triage", "triage[hunt]", "triage[contain]"])
def test_prompt_never_names_a_retired_tool(mode: str) -> None:
    texts = _all_prompt_text()
    if mode not in texts:
        pytest.skip(f"{mode} not assemblable in this build")
    ghosts = sorted(_named_tools(texts[mode]) & set(CONSOLIDATED_AWAY))
    assert not ghosts, (
        f"{mode} prompt names {len(ghosts)} tool(s) the model is not "
        f"advertised and therefore cannot call: {ghosts}. They still dispatch, "
        "so nothing errors -- the model just narrates the call it was told to "
        "make and the analyst gets no card. Use the union entry point instead "
        "(emit_card(card_type=...) / find(kind=...))."
    )


def test_the_containment_rule_points_at_a_callable_tool() -> None:
    """The specific rule whose drift produced the empty containment turn."""
    prompt = prompt_assembly.assemble_triage_prompt()
    assert "Mutating actions always go through" in prompt
    rule = next(ln for ln in prompt.splitlines()
                if "Mutating actions always go through" in ln)
    named = _named_tools(rule)
    assert named, f"the containment rule names no tool at all: {rule!r}"
    assert not (named & set(CONSOLIDATED_AWAY)), (
        f"the containment rule still points at a retired tool: {rule!r}")


def test_the_fallback_prompt_is_not_a_back_door() -> None:
    """The fallback path must not reintroduce what the main path just fixed.

    ``_load_prompt`` falls back to an inline string when neither the fragment
    dir nor the monolith is present (an older tarball). It is rarely hit, which
    is exactly why it drifted: it named three retired tools long after the
    assembled prompt would have been corrected.
    """
    from connector_fsr_agentic_assistant import operations
    fallback = getattr(operations, "_FALLBACK_TRIAGE_PROMPT", None)
    if not fallback:
        pytest.skip("connector operations module exposes no fallback prompt")
    ghosts = sorted(_named_tools(fallback) | (
        {w for w in re.findall(r"[a-z_][a-z0-9_]*", fallback)}
        & set(CONSOLIDATED_AWAY)))
    ghosts = [g for g in ghosts if g in CONSOLIDATED_AWAY]
    assert not ghosts, (
        f"the fallback triage prompt names retired tool(s): {ghosts}")


# --- B3a: allowlist, not denylist -------------------------------------------
# The check above only knew CONSOLIDATED_AWAY. The #69 hunt merge deleted
# siem_search_ip, faz_search_device_events, fmg_get_device_status & co. from the
# registry outright, so they were in NO list -- and the prompt, the routing
# block and the scenario hints kept recommending them for a month, each an
# `unknown tool` (the grounded-hunt golden literally read
# `siem_search_ip(ip="203.0.113.66", direction="dst")`). The property that
# matters is positive: every tool a model-facing surface tells it to CALL is one
# it is advertised.

_CALL = re.compile(r"\b([a-z][a-z0-9_]*)\(")


def _advertised() -> set[str]:
    from fsr_playbooks.llm.tools import anthropic_tools
    return {t["name"] for t in anthropic_tools()}


def _tool_universe() -> set[str]:
    """Every name that is, or ever was, a tool -- so a deleted tool still
    counts as a tool when a prompt names it."""
    from fsr_playbooks.llm import tools as T
    return set(T.REGISTRY) | set(T.retired_to_union())


def _surfaces() -> dict[str, str]:
    from fsr_playbooks.mcp_server.triage_normalize import normalize_record
    from fsr_soc_triage.triage_scenarios import SCENARIOS, classify_alert, render_recipes
    from fsr_soc_triage.triage_sources import SOURCE_TOOLS, build_source_routing_block
    out = dict(_all_prompt_text())
    for key, ts in SOURCE_TOOLS.items():
        out[f"SOURCE_TOOLS[{key}]"] = "\n".join(
            v if isinstance(v, str) else "\n".join(v) for v in ts.values())
    records = {
        "siem_c2": {"name": "C2 beacon", "source": "Fortinet FortiSIEM",
                    "sourceIp": "10.0.0.5", "destinationIp": "203.0.113.9",
                    "sourcedata": '{"incident_data": {"incidentId": 10868}}'},
        "faz": {"name": "x", "source": "FortiAnalyzer", "sourceIp": "10.0.0.5"},
        "fmg_down": {"name": "Device FGT-BRANCH-04 is down",
                     "source": "FortiManager", "deviceName": "FGT-BRANCH-04"},
    }
    for rid, raw in records.items():
        norm = normalize_record(raw)
        out[f"routing[{rid}]"] = build_source_routing_block(norm)
        out[f"recipes[{rid}]"] = "\n".join(render_recipes(classify_alert(norm), norm))
    for sc in SCENARIOS:
        out[f"fragment[{sc['id']}]"] = str(sc.get("fragment", ""))
    return out


@pytest.mark.parametrize("surface", sorted(_surfaces()))
def test_every_tool_a_surface_names_is_advertised(surface: str) -> None:
    text = _surfaces()[surface]
    named = (_named_tools(text) | set(_CALL.findall(text))) & _tool_universe()
    ghosts = sorted(named - _advertised())
    assert not ghosts, (
        f"{surface} tells the model to use {ghosts}, which it is not advertised "
        "-- an `unknown tool` or a narrated call. Name the union instead "
        "(siem_search(by=...), faz_search(by=...), fmg_device(by=...), "
        "find(kind=...), emit_card(card_type=...)).")


def test_the_allowlist_check_can_see_a_ghost() -> None:
    # Self-proof: the pre-B3a template must be flagged.
    text = 'siem_search_ip(ip="1.2.3.4", direction="dst")'
    named = set(_CALL.findall(text)) & _tool_universe()
    assert named - _advertised() == {"siem_search_ip"}


# --- #145 residual: connector OPERATIONS are not chat tools -----------------
# A FortiSIEM / FortiAnalyzer operation (``get_ip_context``, ``search_events``,
# ...) is reached through ``run_op(connector=..., operation=...)``. It is not a
# tool the model can call by its bare name: a model that does gets
# ``unknown_tool``. The allowlist above only checks names in the tool universe,
# which these are not, so a mapping such as ``IP -> `get_ip_context` `` slipped
# through. This check fails on any backticked connector-op name in the triage
# prompt, in the assembled fragments and in the monolith mirror.

CONNECTOR_OPS = frozenset({
    "get_ip_context", "get_host_context", "get_user_context",
    "get_device_info", "get_incident_details", "get_associated_events_new",
    "get_incidents", "search_events", "run_report", "get_alerts",
    "start_and_fetch_bulk_device_logs",
})


def _triage_surfaces_with_monolith() -> dict[str, str]:
    from pathlib import Path
    out = dict(_all_prompt_text())
    monolith = Path(__file__).resolve().parents[1] / "system_prompt_triage.md"
    out["monolith:system_prompt_triage.md"] = monolith.read_text(encoding="utf-8")
    return out


@pytest.mark.parametrize("surface", sorted(_triage_surfaces_with_monolith()))
def test_prompt_never_names_a_bare_connector_op(surface: str) -> None:
    text = _triage_surfaces_with_monolith()[surface]
    bare = sorted(_named_tools(text) & CONNECTOR_OPS)
    assert not bare, (
        f"{surface} names connector operation(s) {bare} as bare backticked "
        "tool names. Operations are not chat tools: a call by bare name is an "
        "unknown_tool. Write run_op(connector=..., operation=...) instead, "
        "e.g. `run_op(operation=\"get_ip_context\")` or the source-aware helper "
        "(siem_search / faz_search / edr_host)."
    )
