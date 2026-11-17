"""Ratchet on the triage system prompt, and the tool-split contradiction.

Sprint 4 W3 (prompt diet). The triage prompt was 557 lines / 40 KB with 30
"never" and 46 "do not", many restating checks dispatch already makes
(unknown_connector, the containment dead-end, record-write ordering). Ceilings
sit just above the post-diet measurement: to add a rule, remove one first.

The prompt also used to say the YAML/playbook tools "are not available in this
session" while the triage prior advertises them (the tool wall came down in the
state-derived slicing change; the disposition block says so). Prose that assumes
a split the slice does not have makes the model narrate or fight its tools.
"""
from __future__ import annotations

import re

from fsr_soc_triage.prompt_assembly import assemble_triage_prompt

MAX_BYTES = 17_000
MAX_NEVER = 19
MAX_DO_NOT = 17
MAX_ALLCAPS_DIRECTIVES = 46


def test_triage_prompt_size_and_prohibition_ceiling():
    text = assemble_triage_prompt()
    assert len(text.encode()) <= MAX_BYTES, len(text.encode())
    assert len(re.findall(r"\bnever\b", text, re.I)) <= MAX_NEVER
    assert len(re.findall(r"\bdo not\b|\bdon't\b", text, re.I)) <= MAX_DO_NOT
    assert len(re.findall(r"\b[A-Z]{3,}\b", text)) <= MAX_ALLCAPS_DIRECTIVES


def test_triage_prompt_does_not_claim_the_authoring_tools_are_absent():
    from fsr_playbooks.llm.turn_plan import plan_turn
    advertised = {t["name"] for t in plan_turn("triage").tools}
    authoring = {"validate_yaml", "verify_playbook", "compose_playbook"} & advertised
    # The premise: the triage slice offers the authoring tools. If a real split
    # is restored this assertion goes red and the prose below may be restored.
    assert authoring, "triage no longer advertises authoring tools: revisit this test"
    flat = re.sub(r"\s+", " ", assemble_triage_prompt()).lower()
    for phrase in ("not available in this session", "never use the yaml",
                   "do not call any yaml/playbook tool"):
        assert phrase not in flat, phrase
