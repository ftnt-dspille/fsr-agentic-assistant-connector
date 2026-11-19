"""Unit tests for the live golden-utterance gate's assertion logic.

The gate itself drives a box (slow, needs creds), but _observe/_check/
_looks_english are pure functions over a chat_turn envelope -- test them against
synthetic transcripts so the gate can actually FAIL (not just pass on a happy box).
Modeled on the real box-159 defect transcript (sess-dmrfucve).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import live_utterances as lu  # noqa: E402


def _env(transcript, tools=None):
    return {"transcript": transcript, "tools_called": tools or []}


# A good build turn: resolved build, authored a playbook, no dead connectors.
GOOD = _env([
    {"type": "turn_inputs", "intent": "build"},
    {"type": "tool_use", "name": "compose_playbook"},
    {"type": "playbook_offer", "id": "pushed-x"},
])
# The defect: resolved triage, sprayed unconfigured connectors, Spanish verdict.
DEFECT = _env([
    {"type": "turn_inputs", "intent": "triage"},
    {"type": "tool_use", "name": "run_op"},
    {"type": "tool_result", "content": {"code": "unknown_connector"}},
    {"type": "tool_result", "content": {"code": "unknown_connector"}},
    {"type": "text", "text": "El conector para ServiceNow no está disponible."},
    {"type": "verdict_card", "id": "v1"},
])

ROW = {"name": "x", "expect": {"resolved_intent": "build",
       "card_in": ["playbook_offer"], "card_not": ["verdict_card"],
       "no_unknown_connector": True, "language": "en"}}


def test_good_turn_passes_every_check():
    assert lu._check(ROW, lu._observe(GOOD)) == []


def test_defect_turn_fails_every_dimension():
    fails = lu._check(ROW, lu._observe(DEFECT))
    blob = " ".join(fails)
    assert "resolved_intent" in blob          # triage, not build
    assert "playbook_offer" in blob           # missing card
    assert "verdict_card" in blob             # forbidden card present
    assert "unknown_connector" in blob        # dead-connector spray
    assert "language" in blob                 # Spanish drift
    assert len(fails) == 5


def test_observe_reads_resolved_intent_and_cards():
    obs = lu._observe(GOOD)
    assert obs["resolved_intent"] == "build"
    assert obs["cards"] == ["playbook_offer"]
    assert obs["unknown_connector"] == 0


def test_language_detector():
    assert lu._looks_english("Blocked the IP at the firewall; no errors.")
    assert lu._looks_english("")  # no prose is not a drift
    assert not lu._looks_english("El conector no está disponible para la búsqueda.")
    assert not lu._looks_english("La actividad fue detectada y está en revisión.")
    assert not lu._looks_english("Se bloqueó la IP, pero no se encontró el host.")


def test_card_not_only_fires_when_present():
    # A build turn with no verdict_card must not trip card_not.
    assert "verdict_card" not in " ".join(lu._check(ROW, lu._observe(GOOD)))


# --- the FortiAI-proxy demo defects (a block ask hunted; replies drifted) ----

_HUNT = ["siem_search", "edr_host", "prior_cases", "mcp_utility__global_search"]
BLOCK_ROW = {"name": "b", "expect": {
    "scope": "act", "card_in": ["action_card"], "card_not": ["verdict_card"],
    "tools_not": _HUNT, "language": "en"}}

# The demo turn: declared act on the new build, but hunted, wrote German prose
# and was forced into a verdict; no action card.
BLOCK_DEFECT = _env([
    {"type": "turn_inputs", "intent": "triage", "scope": "act"},
    {"type": "tool_use", "name": "find"},
    {"type": "tool_use", "name": "siem_search"},
    {"type": "tool_use", "name": "mcp_utility__global_search"},
    {"type": "text", "text": "Nicht alle angeforderten Quellen sind auf diesem "
                              "System als aktive Konnektoren eingerichtet."},
    {"type": "tool_use", "name": "emit_card", "input": {"card_type": "verdict",
     "payload": {"findings": [{"claim": "Verbindung zu bekannter Malware-URL "
                                        "durch OpenPhish bestaetigt und nicht blockiert."}]}}},
    {"type": "verdict_card", "id": "v1"},
], tools=["find", "siem_search", "mcp_utility__global_search", "emit_card"])

BLOCK_GOOD = _env([
    {"type": "turn_inputs", "intent": "triage", "scope": "act"},
    {"type": "tool_use", "name": "find"},
    {"type": "tool_use", "name": "emit_card", "input": {"card_type": "action",
     "payload": {"label": "Block 198.51.100.23 on the FortiGate"}}},
    {"type": "action_card", "id": "a1"},
], tools=["find", "emit_card"])


def test_the_demo_block_turn_fails_on_every_dimension():
    fails = " | ".join(lu._check(BLOCK_ROW, lu._observe(BLOCK_DEFECT)))
    assert "missing card 'action_card'" in fails
    assert "forbidden card 'verdict_card'" in fails
    assert "must not" in fails and "siem_search" in fails
    assert "analyst-facing text is not English" in fails
    assert "card text is not English" in fails


def test_a_block_that_stages_the_action_passes():
    assert lu._check(BLOCK_ROW, lu._observe(BLOCK_GOOD)) == []


def test_a_turn_on_the_wrong_scope_fails():
    env = _env([{"type": "turn_inputs", "intent": "triage", "scope": "investigate"},
                {"type": "action_card", "id": "a1"}])
    assert any("scope" in f for f in lu._check(BLOCK_ROW, lu._observe(env)))


def test_card_text_in_russian_is_drift_even_when_the_reply_is_english():
    env = _env([
        {"type": "turn_inputs", "intent": "triage", "scope": "act"},
        {"type": "text", "text": "Done."},
        {"type": "tool_use", "name": "emit_card", "input": {"card_type": "action",
         "payload": {"label": "Трафик 198.51.100.10 направлен на вредоносный URL"}}},
        {"type": "action_card", "id": "a1"},
    ])
    assert "card text is not English (language drift)" in lu._check(BLOCK_ROW, lu._observe(env))


import pytest  # noqa: E402


@pytest.mark.parametrize("text", [
    "Nicht alle angeforderten Quellen sind auf diesem System als aktive Konnektoren eingerichtet",
    "Трафик 198.51.100.10 направлен на вредоносный URL",
    "Le trafic est bloque pour les adresses avec une regle",
])
def test_the_detector_catches_the_demo_languages(text):
    assert not lu._looks_english(text)


@pytest.mark.parametrize("text", [
    "The IP 1.1.1.1 has been blocked at the firewall and the rule is in place.",
    "No IP address is associated with host host-01 in EDR, so I need one to block.",
    "Blocked 198.51.100.50 on fortigate-firewall (Quarantine Based, 12 Hour).",
])
def test_the_detector_passes_plain_english(text):
    assert lu._looks_english(text)
