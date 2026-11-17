"""The returned envelope must keep usage frames that fall after a gating card.

The per-round DB rows are written from the event stream BEFORE the wire is
truncated at a gating card, so dropping the tail's usage frames made the
envelope's token/cost totals undercount a card turn -- disagreeing with the
authoritative DB (and the usage UI that reads the envelope)."""
import importlib
import sys
import types
from pathlib import Path

from fsr_playbooks.llm.provider import ToolResultEvent, ToolUseEvent, UsageEvent

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)


def _wire_transcript(transcript):
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    mod = importlib.import_module("fsrpb_connector.ops_turn_io")
    return mod._wire_transcript(transcript)


def _usage():
    return UsageEvent(session_id="s", turn=2, model="claude-haiku-5-5",
                      input_tokens=100, output_tokens=20, cache_read=0,
                      cache_write=0, history_chars=0, stop_reason="end_turn",
                      tool_calls=[])


def test_usage_after_gating_card_survives_truncation():
    events = [
        ToolUseEvent(name="emit_card",
                     arguments={"card_type": "playbook_offer",
                                "payload": {"id": "o1", "summary": "s"}},
                     call_id="c1", tier=0),
        ToolResultEvent(call_id="c1",
                        result={"ok": True,
                                "card": {"type": "playbook_offer", "id": "o1"}},
                        duration_ms=1),
        _usage(),  # the round after the gating card -- must not vanish
    ]
    wire = _wire_transcript(events)
    types_ = [w.get("type") for w in wire]
    assert "usage" in types_, "usage frame dropped after the gating card"
    # The gating card still stays the turn's LAST event.
    assert wire[-1].get("type") == "playbook_offer"
