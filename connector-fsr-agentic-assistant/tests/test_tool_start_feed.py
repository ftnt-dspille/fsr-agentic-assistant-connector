"""C4 -- the poll feed shows a tool chip when the model starts the tool block.

The framework emits a ToolStartEvent while the round is still streaming. The
event sink must put a `tool_use` frame on the feed at that point, and must not
put a second `tool_use` frame for the same call when the full ToolUseEvent
arrives at dispatch. The result frame still pairs with the one chip.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

from fsr_playbooks.llm.provider import ToolResultEvent, ToolStartEvent, ToolUseEvent

ROOT = Path(__file__).resolve().parent.parent


def _make_event_sink():
    # Load turn_sink as part of a package (it uses relative imports), with the
    # connector-framework stubs the sink test uses.
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["connectors.core.connector"].ConnectorError = type(
        "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_conn_c4")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_c4"] = pkg
    return importlib.import_module("_fsrpb_conn_c4.turn_sink").make_event_sink


make_event_sink = _make_event_sink()


class _Storage:
    def __init__(self):
        self.frames = []

    def append_turn_progress(self, session_id, turn_idx, frame):
        self.frames.append(frame)


def _sink(storage):
    return make_event_sink(
        storage, {}, "s-1", 1,
        provider_name="fake", audit_intent="triage", audit_prompt="",
        actor_resolver=lambda: {}, record_usage=False)


def _start(name, call_id):
    return ToolStartEvent(name=name, call_id=call_id)


def test_chip_is_shown_at_start_and_not_repeated_at_dispatch():
    st = _Storage()
    on_event = _sink(st)

    on_event(_start("find_connector", "c1"))
    on_event(ToolUseEvent(name="find_connector", arguments={"q": "8.8.8.8"},
                          call_id="c1", tier=1))
    on_event(ToolResultEvent(call_id="c1", result={"ok": True}, duration_ms=5))

    kinds = [(f.get("type"), f.get("id") or f.get("tool_use_id")) for f in st.frames]
    # One chip, shown first; the result pairs with it.
    assert kinds == [("tool_use", "c1"), ("tool_result", "c1")], st.frames
    chip = st.frames[0]
    assert chip["name"] == "find_connector"
    assert chip["input"] == {}


def test_emit_tools_get_no_chip():
    # The widget never renders emit_* calls as chips; the feed must not either.
    st = _Storage()
    on_event = _sink(st)
    on_event(_start("emit_card", "e1"))
    on_event(ToolUseEvent(name="emit_card", arguments={"card_type": "info"},
                          call_id="e1", tier=0))
    assert [f.get("type") for f in st.frames if f.get("type") == "tool_use"] == []


def test_call_without_early_start_still_shows_its_chip():
    # A resumed or un-announced call (no ToolStartEvent) keeps the old path.
    st = _Storage()
    on_event = _sink(st)
    on_event(ToolUseEvent(name="run_op", arguments={"x": 1}, call_id="r1", tier=3))
    chips = [f for f in st.frames if f.get("type") == "tool_use"]
    assert len(chips) == 1 and chips[0]["input"] == {"x": 1}
