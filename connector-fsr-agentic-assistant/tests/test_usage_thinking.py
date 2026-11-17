"""The model's reasoning summary must reach the widget to drive the per-turn
"How I worked this out" panel.

Anthropic adaptive-thinking models return a readable reasoning summary per
round, assembled onto the UsageEvent. The connector used to drop it on the
floor: the usage wire frame carried tokens and stop_reason but never the
reasoning, so chat_history and the live poll feed had no way to show it. These
pin that the reasoning rides the usage frame when present, is omitted when the
model emits none (standard models, the OpenAI/FortiAI proxy), and survives the
round trip through the persisted transcript that chat_history returns.
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
_fields = getattr(provider.UsageEvent, "__dataclass_fields__", None) \
    or getattr(provider.UsageEvent, "model_fields", {})
if "thinking" not in _fields:
    pytest.skip("installed fsr_playbooks predates UsageEvent.thinking",
                allow_module_level=True)


def _ops():
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    return importlib.import_module("fsrpb_connector.operations")


def _usage(thinking=""):
    return provider.UsageEvent(
        session_id="s", turn=1, model="claude-sonnet-5", input_tokens=10,
        output_tokens=400, cache_read=0, cache_write=0, history_chars=0,
        stop_reason="tool_use", thinking=thinking)


_REASONING = ("I should look at the host in FortiEDR first, then pull the "
              "related SIEM events before deciding how bad this is.")


def test_usage_frame_carries_the_reasoning_when_present():
    frame = _ops()._event_to_wire(_usage(_REASONING))
    assert frame["thinking"] == _REASONING


def test_reasoning_is_stripped_of_surrounding_whitespace():
    frame = _ops()._event_to_wire(_usage("\n\n" + _REASONING + "  \n"))
    assert frame["thinking"] == _REASONING


def test_a_standard_model_round_carries_no_reasoning_key():
    # Empty reasoning must not bloat every usage row with a dead key.
    assert "thinking" not in _ops()._event_to_wire(_usage(""))
    assert "thinking" not in _ops()._event_to_wire(_usage("   \n  "))


def test_the_frame_still_validates_against_the_wire_model():
    import pydantic_models as M
    frame = _ops()._event_to_wire(_usage(_REASONING))
    got = M.UsageEvent.model_validate(frame)
    assert got.thinking == _REASONING


def test_an_older_framework_event_without_the_field_is_tolerated():
    # A wheel predating UsageEvent.thinking: the connector must not raise and
    # must emit the lean usage frame it always did (getattr fallback).
    ev = _usage("")
    try:
        del ev.thinking
    except AttributeError:
        pass
    frame = _ops()._event_to_wire(ev)
    assert frame["type"] == "usage"
    assert "thinking" not in frame
