"""A synthesized directive may only name tools that actually exist.

Live on .206 (connector 0.5.103), one turn after the analyst submitted the
manual-input form, the assistant reported this to them:

    "What I tried - I attempted to fetch the workflow/run record you referenced
     (pk 9335) ... /wf/api/workflows/9335 -> not_found
     /wf/api/workflow-runs/9335 -> not_found
     /api/3/wf/workflow-runs/9335 -> http_400 (internal server error)
     /api/3/wf/runs/9335 -> http_400 ..."

Cause: the resume directive ended "...using **get_playbook_run** if you need its
current status", and `get_playbook_run` has never been a tool in this connector.
The registry has `run_playbook`, `resume_playbook` and `list_module_playbooks`,
and no run-status tool at all. Told to use a tool it did not have, the model did
the reasonable thing -- improvised REST paths through `get_record` -- failed four
times, and narrated the failure.

Nothing could catch this. The string is prose; the tool set is a registry; no
gate compared them. Every layer was individually fine.

**Rule: if a synthesized message tells the model to call something, that
something must be registered.** Naming a capability we do not have manufactures
the dead-end the honesty rules exist to prevent -- and it fails in the most
expensive place, in front of the analyst, right after a successful action.
"""
from __future__ import annotations

import re

import pytest

# "call X", "using X", "via X", "with X(" -- how a directive points at a tool.
_TOOL_MENTION = re.compile(
    r"(?:\bcall(?:ing)?\s+|\busing\s+|\bvia\s+)`?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`?")


def _registered_tool_names() -> set[str]:
    """Every tool name the agent can actually be handed, from the registry."""
    import fsr_soc_triage  # noqa: F401  (import side effect: registration)
    from fsr_playbooks.llm.tools import anthropic_tools

    return {t["name"] for t in anthropic_tools()}


def _all_directives() -> list[tuple[str, str]]:
    """(label, text) for every synthesized manual-input message, all branches."""
    from connector_fsr_agentic_assistant import operations as O

    forwards = {
        "forwarded": {"forwarded": True, "manual_input_id": 545, "run": "9335"},
        "gate_expired": {"forwarded": False, "reason": "gate_expired",
                         "expired_seconds_ago": 12, "run": "9335"},
        "no_response_options": {"forwarded": False,
                                "reason": "no_response_options", "run": "9335"},
        "forward_failed": {"forwarded": False, "reason": "forward_failed",
                           "error": "boom"},
        "crudhub_unavailable": {"forwarded": False,
                                "reason": "crudhub_unavailable"},
    }
    out = []
    for label, fwd in forwards.items():
        for decision in ("approve", "reject"):
            durable, directive = O._manual_input_user_message(
                "545", decision, {"note": "x"}, fwd)
            out.append((f"{label}/{decision}", f"{durable}\n{directive}"))
    return out


def test_the_registry_is_readable_at_all():
    """Guard the guard: an empty tool set would make every check below vacuous."""
    names = _registered_tool_names()
    assert len(names) > 5, f"only {len(names)} tools registered: {sorted(names)}"
    assert "run_playbook" in names and "resume_playbook" in names


@pytest.mark.parametrize("label,text",
                         _all_directives(),
                         ids=[lbl for lbl, _ in _all_directives()])
def test_every_tool_a_directive_names_is_registered(label, text):
    registered = _registered_tool_names()
    mentioned = set(_TOOL_MENTION.findall(text))
    # Not every snake_case token is a tool ("manual_input_id", "run_pk"); only
    # judge ones introduced as something to CALL/USE, which is what the regex
    # captures, and ignore obvious non-tool nouns.
    ignore = {"manual_input_id", "run_pk", "workflow_run_iri", "input_id"}
    unknown = sorted(t for t in mentioned - ignore if t not in registered)
    assert not unknown, (
        f"[{label}] directive names tool(s) that are not registered: {unknown}\n"
        "The model will try anyway -- live it improvised REST paths and "
        "narrated four failures to the analyst.\n"
        f"registered: {sorted(registered)}\n\ntext:\n{text}")


def test_the_forwarded_branch_does_not_send_the_model_hunting():
    """The specific regression: after a successful submit, do not fetch status.

    The outcome is already in hand; the only reason that clause existed was to
    make the narration richer, and it cost a visible failure cascade instead.
    """
    from connector_fsr_agentic_assistant import operations as O

    _, directive = O._manual_input_user_message(
        "545", "approve", {"note": "x"},
        {"forwarded": True, "manual_input_id": 545, "run": "9335"})
    assert "get_playbook_run" not in directive
    assert "Do NOT call resume_playbook" in directive, (
        "the double-resume guard must survive this change")
