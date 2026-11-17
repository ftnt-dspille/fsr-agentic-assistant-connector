"""P2 honesty pin for the manual-input bridge message.

The connector once told analysts "the input was accepted" about a
state-changing gate that had already expired -- the gating promise inverted,
with every offline gate green. That was fixed in the *bridge* half of the
message, but the LEAD sentence still asserted approval-with-values on every
branch, so a non-delivering branch shipped a claim and its retraction in the
same user turn.

These tests pin the property rather than the wording: on any branch where the
values did not reach the run, the message must not read as an acceptance.
"""
from __future__ import annotations

import pytest


def _msg(forward: dict, decision: str = "approve") -> str:
    """What the MODEL sees on the resumed turn: facts + turn-scoped directive.

    `_manual_input_user_message` returns those two halves separately (only the
    facts are persisted -- see `tests/test_resume_directive_is_turn_scoped.py`),
    and `_resume_conversation` joins them exactly like this for the one turn the
    directive applies to. Every honesty property in this file is about what the
    model reads, so it is asserted against the join.
    """
    from connector_fsr_agentic_assistant import operations as O
    durable, directive = O._manual_input_user_message(
        "508", decision, {"answer": "yes"}, forward)
    return f"{durable}\n{directive}" if directive else durable


# Every non-delivering `forward` envelope the forwarder can return.
NOT_DELIVERED = [
    pytest.param({"forwarded": False, "reason": "gate_expired",
                  "expired_seconds_ago": 42, "run": "9046"}, id="expired"),
    pytest.param({"forwarded": False, "reason": "gate_expired"},
                 id="expired-empty-body"),
    pytest.param({"forwarded": False, "reason": "no_response_options"},
                 id="no-options"),
    pytest.param({"forwarded": False, "reason": "forward_failed",
                  "error": "boom"}, id="forward-failed"),
    pytest.param({"forwarded": False, "reason": "crudhub_unavailable"},
                 id="no-client"),
]


@pytest.mark.parametrize("forward", NOT_DELIVERED)
def test_undelivered_input_is_never_narrated_as_approved(forward):
    msg = _msg(forward)
    # The lead sentence is part of the claim: "I approved ... with these
    # values" is what the model echoes back to the analyst.
    assert "I approved the manual-input gate" not in msg
    assert "did NOT reach the run" in msg


@pytest.mark.parametrize("forward", NOT_DELIVERED)
def test_undelivered_input_forbids_reporting_the_gated_step(forward):
    msg = _msg(forward)
    assert "do NOT" in msg or "Do NOT" in msg
    # Never invite a resume of a run that is not awaiting input.
    assert "resume_playbook" in msg


def test_forwarded_input_still_reads_as_approved():
    msg = _msg({"forwarded": True, "manual_input_id": 508, "run": "9046"})
    assert "I approved the manual-input gate" in msg
    assert "did NOT reach the run" not in msg
    # Case-insensitive: the emphasis moved when this clause became a persisted
    # FACT rather than part of the shouted directive. The property is that the
    # message says the run was already resumed, not how loudly.
    assert "already been resumed" in msg.lower()


def test_reject_never_claims_values_were_submitted():
    msg = _msg({"forwarded": False, "reason": "gate_expired"},
               decision="reject")
    assert "I declined the manual-input gate" in msg
    assert "I approved" not in msg


@pytest.mark.parametrize("forward", NOT_DELIVERED + [
    pytest.param({"forwarded": True, "run": "9046"}, id="forwarded")])
def test_gate_id_is_always_labelled_never_bare(forward):
    """A bare int in the message got read as a run pk (live: resume_playbook
    on the manual_input_id). Keep the label attached on every branch."""
    assert "manual_input_id=508" in _msg(forward)
