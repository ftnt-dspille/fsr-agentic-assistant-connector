"""The `turn_inputs` frame: what the model was given, on the wire.

The framework loop emits a `TurnInputsEvent` first; the connector writes it as a
`turn_inputs` frame. Three properties matter here:

* it is NOT `turn_start` (the poll feed's turn boundary, which the widget
  latches on: reusing that name would have corrupted its stale-frame fence);
* the widget never draws it: its renderer has no branch for the type, and the
  contract held in both directions by `test_contract_dts_parity` does not list
  it (so it is not a `PollFrame` member; validators land it on `UnknownEvent`);
* old transcripts without it still load and analyse.
"""
from __future__ import annotations

import pytest

from connector_fsr_agentic_assistant import pydantic_models as M
from connector_fsr_agentic_assistant.ops_turn_io import _event_to_wire, _wire_transcript

from fsr_playbooks.llm import provider as _provider  # noqa: E402

TextEvent = _provider.TextEvent
TurnInputsEvent = getattr(_provider, "TurnInputsEvent", None)
pytestmark = pytest.mark.skipif(
    TurnInputsEvent is None, reason="framework older than the turn_inputs record")


def _ev():
    return TurnInputsEvent(model="m", tools=["find", "run_op"], intent="triage",
                           system_chars=900,
                           slices=[{"name": "prompt", "chars": 800}])


def test_the_event_becomes_a_turn_inputs_frame():
    w = _event_to_wire(_ev())
    assert w == {"type": "turn_inputs", "model": "m", "tools": ["find", "run_op"],
                 "intent": "triage", "system_chars": 900,
                 "slices": [{"name": "prompt", "chars": 800}]}


def test_it_is_not_the_poll_feeds_turn_boundary():
    import typing
    assert _event_to_wire(_ev())["type"] != "turn_start"
    assert typing.get_args(M.TurnInputsFrame.model_fields["type"].annotation) == ("turn_inputs",)
    assert typing.get_args(M.TurnStartFrame.model_fields["type"].annotation) == ("turn_start",)


def test_it_is_not_a_pollframe_member_so_the_widget_contract_is_unchanged():
    import typing
    members = typing.get_args(typing.get_args(M.PollFrame)[0])
    assert M.TurnInputsFrame not in members


def test_a_transcript_validator_still_accepts_it():
    from pydantic import TypeAdapter
    frames = [_event_to_wire(_ev()), _event_to_wire(TextEvent(text="hi"))]
    out = TypeAdapter(list[M.PollFrame]).validate_python(frames)
    assert out[0].type == "turn_inputs" and out[1].type == "text"


def test_it_stays_first_in_the_wired_transcript():
    wired = _wire_transcript([_ev(), TextEvent(text="hi")])
    assert [w["type"] for w in wired] == ["turn_inputs", "text"]
