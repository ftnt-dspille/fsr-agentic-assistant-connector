"""Earlier turns replay to the model as native, id-paired tool turns.

History used to collapse every call to `[called name(args)]` text. A model
that reads calls as text writes calls as text: on a three-turn refine, 3 of 4
runs announced an edit as `[called edit_playbook(...)]` prose and never made
the call. These tests pin the replacement: both the widget's `messages[]` and
the stored conversation a resume reloads reach the provider as assistant
`tool_use` turns answered by `tool_result` turns with matching ids, and no
replayed string is call-shaped.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name):
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


@pytest.fixture(scope="module")
def hr():
    return _load("history_replay")


# The widget's `_serializeMessagesForServer` shape: calls and results share
# one assistant message; a plain `tool_use` event carries no id, a paired
# `tool_call` event carries the model's id on both halves.
_WIDGET = [
    {"role": "user", "content": "Build a playbook that notifies on high alerts"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Checking the step shapes."},
        {"type": "tool_use", "name": "get_step_type", "input": {"name": "decision"}},
        {"type": "tool_result", "tool": "get_step_type", "content": '{"ok": true}'},
        {"type": "tool_use", "id": "chatcmpl-tool-ab12", "name": "edit_playbook",
         "input": {"ops": []}},
        {"type": "tool_result", "tool_use_id": "chatcmpl-tool-ab12",
         "tool": "edit_playbook", "content": '{"ok": true, "yaml": "..."}'},
        {"type": "text", "text": "Here is the draft."},
    ]},
    {"role": "user", "content": "Now add a step that sets the status"},
]


def _paired(turns):
    """Every tool_use answered by the very next user turn, ids matching."""
    for i, (role, content) in enumerate(turns):
        if role != "assistant" or not isinstance(content, list):
            continue
        ids = [b["id"] for b in content if b["type"] == "tool_use"]
        if not ids:
            continue
        nxt_role, nxt = turns[i + 1]
        assert nxt_role == "user"
        assert [b["tool_use_id"] for b in nxt] == ids
    return True


def test_widget_history_becomes_paired_native_turns(hr):
    turns = hr.replay_turns(_WIDGET)
    assert [r for r, _ in turns] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert _paired(turns)
    # The model's own id survives (sanitized); the id-less call gets one.
    ids = [b["id"] for _, c in turns if isinstance(c, list)
           for b in c if b.get("type") == "tool_use"]
    assert ids[1] == "chatcmpl-tool-ab12" and ids[0]
    assert "[called" not in json.dumps(turns)


def test_a_call_with_no_result_gets_one_and_an_orphan_result_is_dropped(hr):
    turns = hr.replay_turns([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "tool_result", "tool": "find", "content": "orphan"},
            {"type": "tool_use", "name": "run_op", "input": {"x": 1}},
        ]},
        {"role": "user", "content": "next"},
    ])
    assert _paired(turns)
    (_, results) = turns[2]
    assert results[0]["content"] == hr._NO_RESULT
    assert "orphan" not in json.dumps(turns)


def test_only_recent_turns_replay_calls_older_ones_are_prose(hr):
    convo = [{"role": "user", "content": "start"}]
    for n in range(hr.NATIVE_TURNS + 2):
        convo.append({"role": "assistant", "content": [
            {"type": "text", "text": f"turn {n}"},
            {"type": "tool_use", "id": f"c{n}", "name": "find", "input": {}},
            {"type": "tool_result", "tool_use_id": f"c{n}", "content": "{}"},
        ]})
        convo.append({"role": "user", "content": f"more {n}"})
    turns = hr.replay_turns(convo)
    prose = [c for r, c in turns if r == "assistant" and isinstance(c, str)]
    assert len(prose) == 2
    assert prose[0].startswith("turn 0") and "Tools used in this reply: find" in prose[0]
    assert all("[called" not in p and "find(" not in p for p in prose)
    native_calls = [b for r, c in turns if r == "assistant" and isinstance(c, list)
                    for b in c if b["type"] == "tool_use"]
    assert len(native_calls) == hr.NATIVE_TURNS


def test_stored_transcript_coalesces_streamed_tokens_and_keeps_ids(hr):
    blocks = hr.transcript_blocks([
        {"type": "text", "text": "\n\nI"}, {"type": "text", "text": "'ll check"},
        {"type": "text", "text": " the step."},
        {"type": "tool_use", "id": "t1", "name": "get_step_type", "input": {}},
        {"type": "tool_result", "tool_use_id": "t1", "content": {"ok": True}},
        {"type": "usage", "model": "m"},
        {"type": "action_card", "id": "a1", "connector": "fortigate",
         "operation": "block_ip"},
    ])
    assert blocks[0] == {"type": "text", "text": "I'll check the step."}
    assert blocks[1]["id"] == "t1" and blocks[2]["tool_use_id"] == "t1"
    assert "fortigate.block_ip" in blocks[3]["text"]
    assert hr.transcript_blocks([]) == [{"type": "text", "text": "(no visible output)"}]


def test_resume_replays_the_stored_conversation_natively(ops, hr):
    stored = ops._canonical_messages(
        [{"role": "user", "content": "build it"}], None,
        [{"type": "tool_use", "id": "t1", "name": "edit_playbook", "input": {}},
         {"type": "tool_result", "tool_use_id": "t1", "content": "{}"},
         {"type": "text", "text": "Done."}])
    turns = hr.replay_turns(stored)
    assert [r for r, _ in turns] == ["user", "assistant", "user", "assistant"]
    assert _paired(turns)
    # An older connector's string rows still replay as they were written.
    legacy = [{"role": "user", "content": "hi"},
              {"role": "assistant", "content": "hello"}]
    assert hr.replay_turns(legacy) == [("user", "hi"), ("assistant", "hello")]


def test_context_folds_into_analyst_turns_not_tool_results(ops):
    msgs = ops._wire_messages(_WIDGET)
    ops._inject_entity_context(msgs, {"module": "alerts", "id": 7,
                                      "fields": {"name": "A"}})
    results = [m for m in msgs if m.role == "user" and isinstance(m.content, list)]
    assert results and all(b["type"] == "tool_result"
                           for m in results for b in m.content)
    assert isinstance(msgs[0].content, str) and isinstance(msgs[-1].content, str)
    assert msgs[-1].content.endswith("Now add a step that sets the status")


def test_widget_history_is_a_valid_openai_request(ops):
    from fsr_playbooks.llm.openai_provider import _to_openai_messages
    out = _to_openai_messages("sys", ops._wire_messages(_WIDGET))
    for i, m in enumerate(out):
        for call in m.get("tool_calls") or []:
            answered = [x for x in out[i + 1:i + 1 + len(m["tool_calls"])]
                        if x["role"] == "tool"]
            assert call["id"] in [x["tool_call_id"] for x in answered]
    assert "[called" not in json.dumps(out)
