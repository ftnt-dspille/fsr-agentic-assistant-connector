"""A round cut off at the output cap must be diagnosable from what we store.

Live: a gpt-5.4-mini build round spent the whole 16384-token cap and stopped
mid tool call. `chat_history` kept only its token count -- the usage frame had
no stop_reason and nothing of the call it was writing -- so the runaway could
not be told apart from reasoning. The framework's UsageEvent now carries
`dropped_calls`; these pin that the connector keeps them on the wire frame and
in the chat_turns row.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)

provider = pytest.importorskip("fsr_playbooks.llm.provider")
if not hasattr(provider, "DroppedCall"):
    pytest.skip("installed fsr_playbooks predates UsageEvent.dropped_calls",
                allow_module_level=True)


def _ops():
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    return importlib.import_module("fsrpb_connector.operations")


def _usage(dropped=()):
    return provider.UsageEvent(
        session_id="s", turn=1, model="gpt-5.4-mini", input_tokens=10,
        output_tokens=16384, cache_read=0, cache_write=0, history_chars=0,
        stop_reason="length",
        tool_calls=[provider.ToolCallUsage(name="get_step_type", args_chars=2,
                                           result_chars=40)],
        dropped_calls=list(dropped))


_CUT = provider.DroppedCall(name="emit_card", arg_chars=61234, tail='"yaml": "a a a a')


def test_usage_frame_carries_stop_reason_and_the_dropped_call():
    frame = _ops()._event_to_wire(_usage([_CUT]))
    assert frame["stop_reason"] == "length"
    assert frame["dropped_calls"] == [
        {"name": "emit_card", "arg_chars": 61234, "tail": '"yaml": "a a a a'}]


def test_usage_frame_still_validates_against_the_wire_model():
    import pydantic_models as M
    frame = _ops()._event_to_wire(_usage([_CUT]))
    got = M.UsageEvent.model_validate(frame)
    assert got.dropped_calls[0].name == "emit_card"


def test_chat_turn_row_marks_the_dropped_call():
    rows = _ops()._turn_tool_calls(_usage([_CUT]))
    assert rows[0] == {"name": "get_step_type", "args_chars": 2, "result_chars": 40}
    assert rows[1]["dropped"] is True and rows[1]["name"] == "emit_card"
    assert rows[1]["tail"] == _CUT.tail


def test_a_normal_round_adds_nothing():
    ops = _ops()
    assert ops._event_to_wire(_usage())["dropped_calls"] == []
    assert all("dropped" not in r for r in ops._turn_tool_calls(_usage()))


def test_an_older_framework_event_without_the_field_is_tolerated():
    ev = types.SimpleNamespace(tool_calls=[], stop_reason="stop")
    assert _ops()._dropped_calls(ev) == []
