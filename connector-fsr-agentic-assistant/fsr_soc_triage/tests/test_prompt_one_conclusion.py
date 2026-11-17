"""The triage prompt asks for ONE conclusion, and that conclusion is the card.

Live (GA rehearsal, after "the verdict card is the write-up" was added), the
model still wrote a 2,572-character prose summary and then the verdict card:
the same prompt also said "investigate first, summarize once at the end ...
One summary, at the end" and "the most likely story -- then summarize". It
followed both. Every mode of the assembled prompt must say one thing.
"""
from __future__ import annotations

import re

import pytest
from fsr_soc_triage.prompt_assembly import assemble_triage_prompt

_PROSE_SUMMARY_ASKS = (
    r"summarize once at the end",
    r"\bOne\s+summary,\s+at the end",
    r"most likely story\W+then summarize",
)


@pytest.mark.parametrize("mode", [None, "hunt", "contain"])
def test_no_instruction_asks_for_a_prose_summary_at_the_close(mode):
    prompt = assemble_triage_prompt(mode=mode)
    for pat in _PROSE_SUMMARY_ASKS:
        assert not re.search(pat, prompt), f"{pat!r} is back in the {mode} prompt"


def test_the_close_is_the_verdict_card():
    prompt = assemble_triage_prompt()
    assert "The verdict card is the write-up" in prompt
    assert "conclusion IS the verdict card" in prompt


# --------------------------------------------------------------------------- #
# F2: an emitted action card is PENDING approval, not executed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", [None, "contain"])
def test_pending_action_card_contract_is_in_the_prompt(mode):
    """Live F2: asked 'did the block go through, or waiting on me?', the agent
    answered 'it went through -- not waiting on you' off the alert still being
    Open + FortiGate Available, misreporting the P2 HITL gate. The prompt must
    state that an emitted action card is pending approval and has not run."""
    prompt = assemble_triage_prompt(mode=mode)
    assert "PENDING the analyst's approval and has NOT run" in prompt
    assert "awaiting their approval" in prompt
    assert "is NOT evidence the action executed" in prompt
