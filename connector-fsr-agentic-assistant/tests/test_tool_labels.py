"""Every registered tool has an analyst-facing label, and it rides the frame.

The widget kept its own phrase table, a hand copy of these tool names. Live, a
new tool showed as its raw name ("Edr host") and 8 of the 24 tools advertised to
triage had no phrase. The connector names its tools now; this fails the day a
tool is registered without a name.
"""
from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

import pytest

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)
pytest.importorskip("fsr_playbooks.llm.provider")


def _pkg():
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg


def test_every_registered_tool_has_a_label():
    from fsr_soc_triage import registry as r
    for register in (r.register_triage_tools, r.register_record_tools,
                     r.register_ztpf_tools, r.register_playbook_tools):
        register()
    from fsr_playbooks.llm.tools import REGISTRY
    _pkg()
    labels = importlib.import_module("fsrpb_connector.tool_labels").TOOL_LABELS
    missing = sorted(n for n in REGISTRY if n not in labels)
    assert not missing, f"tools with no analyst label: {missing}"


def test_labels_read_as_an_analyst_would_say_them():
    _pkg()
    labels = importlib.import_module("fsrpb_connector.tool_labels").TOOL_LABELS
    for name, label in labels.items():
        assert label and label[0].isupper(), name
        assert "_" not in label, f"{name}: {label!r} reads like an identifier"
        assert not re.search("[–—]", label), f"{name}: dash in {label!r}"
        if "_" in name:  # a one-word name ("picklist") is just a word
            assert name not in label, f"{name}: the label repeats the tool name"


def test_the_tool_use_frame_carries_the_label():
    _pkg()
    turn_io = importlib.import_module("fsrpb_connector.ops_turn_io")
    from fsr_playbooks.llm.provider import ToolUseEvent
    frame = turn_io._event_to_wire(ToolUseEvent(
        name="edr_host", arguments={"host": "h"}, call_id="c1", tier=1))
    assert frame["type"] == "tool_use" and frame["label"] == "Checked the host in FortiEDR"


def test_a_native_mcp_tool_gets_no_label():
    _pkg()
    turn_io = importlib.import_module("fsrpb_connector.ops_turn_io")
    from fsr_playbooks.llm.provider import ToolUseEvent
    frame = turn_io._event_to_wire(ToolUseEvent(
        name="mcp_soc__get_alert", arguments={}, call_id="c2", tier=1))
    assert "label" not in frame
