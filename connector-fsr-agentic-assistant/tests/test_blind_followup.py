"""Tracker #54 -- a follow-up turn that carries no conversation.

`chat_turn` builds the model's context from `params["messages"]` and nothing
else (`_wire_messages`). The server-side session is durable, but it is written
for the history browser and the poll feed and is never read back as LLM
context. So a caller that ships only the new user message on turn 2 runs the
agent blind, and what the analyst sees is the agent asking for an indicator it
was already given -- a context-assembly bug wearing a steering bug's clothes.

The detector's job is to make that assembly gap say its own name.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name):
    """Load a connector module under a synthetic package -- the connector dir
    name has a hyphen, so `operations` is not importable normally. Same loader
    the other operations-level tests use."""
    if "_fsrpb_conn" not in sys.modules:
        pkg = types.ModuleType("_fsrpb_conn")
        pkg.__path__ = [str(ROOT)]
        sys.modules["_fsrpb_conn"] = pkg
    key = f"_fsrpb_conn.{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ops():
    return _load("operations")


def _msgs(ops, *pairs):
    return ops._wire_messages([{"role": r, "content": c} for r, c in pairs])


def test_first_turn_is_never_blind(ops):
    # Turn 1 legitimately has no assistant history -- there is no prior turn.
    assert ops._is_blind_followup(
        1, _msgs(ops, ("user", "Block 198.51.100.7."))) is False


def test_followup_without_assistant_history_is_blind(ops):
    # The #54 repro: turn 2, only the new user message.
    assert ops._is_blind_followup(
        2, _msgs(ops, ("user", "Go ahead and block it now."))) is True


def test_two_user_messages_are_still_blind(ops):
    # Message count is not the signal. The agent never saw its own half.
    assert ops._is_blind_followup(
        2, _msgs(ops, ("user", "Block 198.51.100.7."),
                 ("user", "Go ahead and block it now."))) is True


def test_replayed_conversation_is_not_blind(ops):
    # What the widget's _serializeMessagesForServer actually sends.
    assert ops._is_blind_followup(
        2, _msgs(ops, ("user", "Block 198.51.100.7."),
                 ("assistant", [{"type": "tool_use", "name": "run_op",
                                 "input": {"connector": "fortigate"}}]),
                 ("user", "Go ahead and block it now."))) is False


def test_empty_messages_on_a_followup_is_blind(ops):
    # Defensive: chat_turn rejects this upstream, but the predicate must not
    # crash or report a no-history turn as healthy.
    assert ops._is_blind_followup(3, []) is True


# --- block-history robustness ----------------------------------------------
# Found live on 0.5.69 while replaying a prior turn's transcript as the history
# turn 2 carries: the turn died with
#   TypeError: sequence item 23: expected str instance, int found
# out of a `"\n".join(parts)` that trusted `text` to already be a str -- and
# `text` is the most common block on the widget's own wire path. The cost is
# the whole turn: `_wire_messages` runs before anything is streamed, so the
# analyst gets a connector stack trace and no transcript.

def test_wire_messages_survives_a_non_str_text_block(ops):
    # The actual crash site: _wire_messages on an assistant turn.
    msgs = ops._wire_messages([
        {"role": "user", "content": "block it"},
        {"role": "assistant", "content": [{"type": "text", "text": 23}]},
    ])
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == [{"type": "text", "text": "23"}]


def test_user_block_content_keeps_a_non_str_text(ops):
    msgs = ops._canonical_messages(
        [{"role": "user", "content": [{"type": "text", "text": 23}]}], None, [])
    assert msgs[0] == {"role": "user", "content": "23"}
