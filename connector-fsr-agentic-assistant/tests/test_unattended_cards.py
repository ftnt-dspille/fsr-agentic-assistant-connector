"""On an unattended turn (auto-triage, a scheduled hunt) nobody can answer a
choice or a capability gap, so neither may halt the turn: the card stays in the
transcript, the turn goes on to its verdict, and the envelope does not park on
`awaiting_choice`. An attended turn keeps gating exactly as before, and an
approval gates either way (containment is never automatic)."""
import importlib
import sys
import types
from pathlib import Path

import pytest
from fsr_playbooks.llm.provider import ToolResultEvent, ToolUseEvent

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)


def _mod():
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    return importlib.import_module("fsrpb_connector.ops_turn_io")


GAP = {"type": "capability_gap", "id": "g1", "missing": "x", "why": "y",
       "fix_steps": ["z"], "resume": {"label": "Re-check", "value": "recheck"}}
CHOICE = {"type": "choice_card", "id": "c1", "question": "q",
          "options": [{"label": "a", "value": "a"}]}
VERDICT = {"type": "verdict_card", "id": "v1", "disposition": "benign",
           "severity": "info", "confidence": 0.8, "summary": "nothing found",
           "findings": []}


def _emit(call_id, card_type, card):
    return [ToolUseEvent(name="emit_card",
                         arguments={"card_type": card_type, "payload": dict(card)},
                         call_id=call_id, tier=0),
            ToolResultEvent(call_id=call_id, result={"ok": True, "card": dict(card)},
                            duration_ms=1)]


def _gap_then_verdict():
    return _emit("a", "capability_gap", GAP) + _emit("b", "verdict", VERDICT)


def _types(wire):
    return [w.get("type") for w in wire]


@pytest.mark.parametrize("card_type,card", [("capability_gap", GAP), ("choice", CHOICE)])
def test_an_attended_turn_still_stops_on_the_card(card_type, card):
    m = _mod()
    wire = m._wire_transcript(_emit("a", card_type, card) + _emit("b", "verdict", VERDICT))
    assert wire[-1]["type"] == card["type"]
    assert m._envelope_stop_reason(wire, "end_turn") == "awaiting_choice"


@pytest.mark.parametrize("card_type,card", [("capability_gap", GAP), ("choice", CHOICE)])
def test_an_unattended_turn_keeps_the_card_and_runs_on_to_the_verdict(card_type, card):
    m = _mod()
    wire = m._wire_transcript(_emit("a", card_type, card) + _emit("b", "verdict", VERDICT),
                              unattended=True)
    kinds = _types(wire)
    assert card["type"] in kinds and kinds[-1] == "verdict_card"
    assert wire[-1]["disposition"] == "benign"
    assert m._envelope_stop_reason(wire, "end_turn", unattended=True) == "end_turn"


def test_an_unattended_gap_with_no_verdict_does_not_park_the_turn():
    m = _mod()
    wire = m._wire_transcript(_emit("a", "capability_gap", GAP), unattended=True)
    assert wire[-1]["type"] == "capability_gap"
    assert m._envelope_stop_reason(wire, "end_turn", unattended=True) == "end_turn"
    assert m._envelope_stop_reason(wire, "end_turn") == "awaiting_choice"


def test_an_approval_card_still_gates_an_unattended_turn():
    m = _mod()
    action = {"type": "action_card", "id": "act1", "title": "Block", "args": {}}
    wire = m._wire_transcript(_emit("a", "capability_gap", GAP) + _emit("b", "action", action)
                              + _emit("c", "verdict", VERDICT), unattended=True)
    assert wire[-1]["type"] == "action_card"
    assert m._envelope_stop_reason(wire, "end_turn", unattended=True) == "awaiting_action_card"
