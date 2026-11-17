"""A read-only quick-action chip must not be able to propose a change.

Live in sess-vtb857ns the analyst pressed the `explain` chip. The model wrote
the full step-by-step explanation, then -- because `analyze_playbook` surfaced a
real defect while it explained -- kept going for five more model round-trips
(find_operation, get_step_type x3, verify_enhancement) and ended the turn on
`emit_enhancement_offer`. One cause, two symptoms: an unrequested playbook-change
proposal, and a composer that stayed locked behind the typing indicator long
after the prose finished, because the turn genuinely was still running.

The build prompt already said "do not propose edits unless asked". Prose was not
enough, and per P2 doctrine a capability a turn must not have is removed at
dispatch. These tests pin that the withholding happens in the tool slice.
"""
from __future__ import annotations

import pytest


def _slice(**kw):
    from connector_fsr_agentic_assistant import operations as O
    from connector_fsr_agentic_assistant.operations import ResolvedPersona
    persona = kw.pop("persona", None) or ResolvedPersona(intent="build")
    return {t["name"] for t in O._tools_for_persona(
        persona, has_open_playbook=True, **kw)}


# The frontier that authors an edit, gates it, or delivers it.
WRITE_FRONTIER = [
    "emit_enhancement_offer", "emit_playbook_offer", "verify_enhancement",
    "push_playbook", "run_playbook", "compile_yaml",
]


@pytest.mark.parametrize("chip", ["explain", "find_issues"])
@pytest.mark.parametrize("tool", WRITE_FRONTIER)
def test_read_only_chip_withholds_the_write_frontier(chip, tool):
    assert tool not in _slice(quick_action=chip)


@pytest.mark.parametrize("chip", ["explain", "find_issues"])
def test_read_only_chip_keeps_the_tools_the_mode_needs(chip):
    """Withholding the write path must not disarm the analysis path -- these
    are exactly how `explain` and `find_issues` do their job."""
    tools = _slice(quick_action=chip)
    for needed in ("analyze_playbook",  # includes the trace since B3a
                   "why_did_playbook_fail", "find", "get_step_type"):
        assert needed in tools, needed


@pytest.mark.parametrize("chip", ["add_step", "add_error_handling", "optimize"])
def test_change_chips_are_untouched(chip):
    """Only the read-only chips are narrowed; a change chip keeps the full
    slice, or the fix would break the flows that are supposed to offer edits."""
    assert _slice(quick_action=chip) == _slice()


def test_no_chip_is_untouched():
    assert _slice(quick_action=None) == _slice()


def test_unknown_chip_is_not_treated_as_read_only():
    """Fail-open on an unrecognized chip: withholding tools from a chip we do
    not know the semantics of would silently break a future mode."""
    assert _slice(quick_action="some_future_chip") == _slice()


@pytest.mark.parametrize("chip", ["Explain", "EXPLAIN", " explain "])
def test_chip_match_is_case_and_whitespace_insensitive(chip):
    assert "emit_enhancement_offer" not in _slice(quick_action=chip)


def test_persona_allowlist_does_not_re_grant_the_write_frontier():
    """A persona allowlist wins over the built-in intent slice, but `explain`
    is the analyst's statement about THIS turn, made after the persona was
    configured -- and nothing downstream re-asks them."""
    from connector_fsr_agentic_assistant.operations import ResolvedPersona
    persona = ResolvedPersona(
        intent="build",
        profile={"tools_allow": ["analyze_playbook", "emit_enhancement_offer"]})
    tools = _slice(persona=persona, quick_action="explain")
    assert "emit_enhancement_offer" not in tools
    assert "analyze_playbook" in tools


def test_advertised_tools_threads_the_chip_through():
    """The turn path calls `_advertised_tools`, not `_tools_for_persona`
    directly -- the narrowing has to survive that hop or it never fires live."""
    from connector_fsr_agentic_assistant import operations as O
    from connector_fsr_agentic_assistant.operations import ResolvedPersona
    persona = ResolvedPersona(intent="build")
    names = {t["name"] for t in O._advertised_tools(
        {}, persona, has_open_playbook=True, include_mcp=False,
        quick_action="explain")}
    assert names, "expected a non-empty slice"
    assert "emit_enhancement_offer" not in names
    assert "analyze_playbook" in names
