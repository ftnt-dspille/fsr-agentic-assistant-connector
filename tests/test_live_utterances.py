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
