"""P1 — persona-spine unit contracts (Persona Spine Unification plan).

Locks the one-resolution-spine primitives: intent normalization, the explicit
per-intent drop-set (no `!= "triage"` catch-all), ResolvedPersona, and
allowlist-wins tool slicing. These are the invariants P2/P3 build on; the
byte-for-byte turn behavior is separately pinned by the golden matrix.
"""
from __future__ import annotations

import fsr_soc_triage  # noqa: F401 — registers the connector tool surface (get_record, …)
from connector_fsr_agentic_assistant import operations as ops


class _Prof:
    """Minimal Profile stand-in (duck-typed: tools_allow, prompt)."""
    def __init__(self, tools_allow=None, prompt=None):
        self.tools_allow = tools_allow or []
        self.prompt = prompt


def test_normalize_intent():
    assert ops._normalize_intent("triage") == "triage"
    assert ops._normalize_intent("build") == "build"
    # Legacy free-text shape and junk both resolve to build — the ONE default.
    assert ops._normalize_intent("triage this incident") == "build"
    assert ops._normalize_intent(None) == "build"
    assert ops._normalize_intent("hunt") == "build"


def test_afforded_drop_set_is_explicit_per_intent():
    # Phase 3 full-surface switch: the prior no longer changes the surface.
    # Both priors drop only the deliberately persona-allowlist-only names
    # (build-only ∩ triage-only, minus the run verb) plus state gates.
    triage_drop = ops._afforded_drop_set("triage", has_trace=True)
    build_drop = ops._afforded_drop_set("build", has_trace=True)
    assert triage_drop == build_drop
    assert "compile_yaml" not in triage_drop and "run_op" not in build_drop
    assert "run_playbook" not in build_drop  # run verb exempt (#82)


def test_afforded_drop_set_has_no_trace_gate():
    """Phase 3.1: `has_trace` no longer moves the surface.

    It flipped mid-session on the triage->bottle arc, and the advertised tool
    list is a prompt-cache prefix, so every flip discarded the cached
    (tools, system) prefix. `build_playbook_from_trace` now answers
    `empty_trace` from inside the tool instead."""
    with_trace = ops._afforded_drop_set("build", has_trace=True)
    no_trace = ops._afforded_drop_set("build", has_trace=False)
    assert "build_playbook_from_trace" not in with_trace
    assert "build_playbook_from_trace" not in no_trace
    assert with_trace == no_trace
    assert "build_playbook_from_trace" not in ops._afforded_drop_set("triage", has_trace=False)


def test_resolved_persona_properties():
    p = ops.ResolvedPersona(intent="triage")
    assert p.tools_allow is None and p.prompt_override is None
    p2 = ops.ResolvedPersona(intent="build",
                             profile=_Prof(tools_allow=["get_record"], prompt="X"))
    assert p2.tools_allow == ["get_record"] and p2.prompt_override == "X"
    # Empty prompt normalizes to None (falls back to built-in).
    assert ops.ResolvedPersona(intent="triage", profile=_Prof(prompt="")).prompt_override is None


def test_tools_for_persona_allowlist_wins():
    persona = ops.ResolvedPersona(
        intent="triage", profile=_Prof(tools_allow=["get_record", "run_op"]))
    names = {t["name"] for t in ops._tools_for_persona(persona)}
    # Plus the read-only tools every persona carries (_PERSONA_IMPLIED_TOOLS).
    assert names == {"get_record", "run_op", "test_template"}


def test_every_persona_can_render_jinja():
    """Live: the Automation Actions persona did not list the renderer, and the
    analyst asked the assistant to render a template four times."""
    persona = ops.ResolvedPersona(
        intent="build", profile=_Prof(tools_allow=["get_record", "update_record"]))
    assert "test_template" in {t["name"] for t in ops._tools_for_persona(persona)}


def test_tools_for_persona_no_profile_uses_intent_slice():
    triage = {t["name"] for t in ops._tools_for_persona(ops.ResolvedPersona("triage"))}
    build = {t["name"] for t in ops._tools_for_persona(ops.ResolvedPersona("build"))}
    # Phase 3 full-surface: both priors carry both surfaces.
    assert "run_op" in triage and "validate_yaml" in triage
    assert "run_op" in build and "validate_yaml" in build


def test_shims_match_spine():
    """The retained back-compat shims produce the same slice as the spine."""
    via_shim = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="triage"), include_mcp=False)}
    via_spine = {t["name"] for t in ops._tools_for_persona(ops.ResolvedPersona("triage"))}
    assert via_shim == via_spine
    via_turn = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build", profile=None), include_mcp=False)}
    assert via_turn == {t["name"] for t in ops._tools_for_persona(ops.ResolvedPersona("build"))}


def test_which_turns_conclude_with_a_verdict():
    """A verdict card concludes an investigation. Live, a ZTPF device question
    ("what was the latest automation on this device") was forced into a
    needs_more_info verdict card restating the answer."""
    from fsr_soc_triage.profiles import PersonaCapabilities
    builtin = ops.ResolvedPersona("triage")
    device = ops.ResolvedPersona("triage", profile=_Prof(tools_allow=["get_record"]))
    grounded = ops.ResolvedPersona("triage", profile=_Prof(tools_allow=["get_record"]))
    grounded.profile.capabilities = PersonaCapabilities(grounding=True)
    assert builtin.concludes_with_verdict(None) is True
    assert builtin.concludes_with_verdict("ztpf_devices") is True
    assert device.concludes_with_verdict("ztpf_devices") is False
    assert device.concludes_with_verdict("alerts") is True
    assert device.concludes_with_verdict("incidents") is True
    assert grounded.concludes_with_verdict("ztpf_devices") is True
