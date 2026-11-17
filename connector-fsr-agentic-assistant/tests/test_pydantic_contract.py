"""Contract-parity tests for pydantic_models.

Standalone: imports ONLY pydantic_models (no fsr_playbooks), so it runs without
the connector's full dependency chain. Asserts the wire-frame union mirrors the
widget contract (contract.d.ts / FsrPbChat) and that the corrected inbound arg
models are strict.
"""
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pydantic_models as M  # noqa: E402


FRAME_ADAPTER = TypeAdapter(M.PollFrame)


class TestPollFrameUnion:
    def test_every_known_frame_type_resolves_to_its_model(self):
        cases = {
            "turn_start": (M.TurnStartFrame, {"type": "turn_start", "turn": 3}),
            "stream_end": (M.StreamEndFrame, {"type": "stream_end", "stop_reason": "end_turn",
                                              "transcript": [{"type": "text", "text": "hi"}]}),
            "text": (M.TextEvent, {"type": "text", "text": "working"}),
            "tool_use": (M.ToolUseEvent, {"type": "tool_use", "id": "t1", "name": "get_record",
                                          "input": {"iri": "/x"}, "tier": 1}),
            "tool_result": (M.ToolResultEvent, {"type": "tool_result", "tool_use_id": "t1",
                                                "content": "ok", "duration_ms": 42}),
            "usage": (M.UsageEvent, {"type": "usage", "model": "claude-haiku-4-5",
                                     "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}),
            "approval_request": (M.ApprovalRequestEvent, {"type": "approval_request",
                                 "approval_id": "a1", "tool": "run_op", "tier": 3,
                                 "args_hash": "abc", "requires_step_up": True}),
            "activity": (M.ActivityEvent, {"type": "activity", "message": "m", "phase": "p"}),
            "error": (M.ErrorEvent, {"type": "error", "message": "boom"}),
            "action_card": (M.ActionCardEvent, {"type": "action_card", "title": "T", "anything": 1}),
            "choice_card": (M.ChoiceCardEvent, {"type": "choice_card", "prompt": "?"}),
            "capability_gap": (M.CapabilityGapEvent, {"type": "capability_gap", "missing": "x"}),
            "manual_input": (M.ManualInputEvent, {"type": "manual_input", "question": "q"}),
            "playbook_offer": (M.PlaybookOfferEvent, {"type": "playbook_offer", "summary": "s"}),
            "status_card": (M.StatusCardEvent, {"type": "status_card"}),
            "info_card": (M.InfoCardEvent, {"type": "info_card"}),
            "ioc_card": (M.IocCardEvent, {"type": "ioc_card"}),
        }
        for label, (model, payload) in cases.items():
            got = FRAME_ADAPTER.validate_python(payload)
            assert isinstance(got, model), f"{label} resolved to {type(got).__name__}"

    def test_unknown_frame_type_falls_through_to_catchall(self):
        # _event_to_wire emits {"type": "unknown", "repr": ...} and may emit a
        # brand-new type; both must validate (never break live chat_poll).
        got = FRAME_ADAPTER.validate_python({"type": "unknown", "repr": "<Ev>"})
        assert isinstance(got, M.UnknownEvent)
        future = FRAME_ADAPTER.validate_python({"type": "brand_new_2027", "x": 1})
        assert isinstance(future, M.UnknownEvent)

    def test_known_frame_with_extra_fields_is_tolerated(self):
        # tool_use gains a field the connector adds later → must still validate.
        got = FRAME_ADAPTER.validate_python({"type": "tool_use", "name": "n", "future_field": 9})
        assert got.type == "tool_use"

    def test_poll_response_validates_a_full_frame_list(self):
        resp = {
            "ok": True, "session_id": "s", "contract_version": "2.8.0",
            "turn": 1, "cursor": 9, "done": True, "stop_reason": "end_turn",
            "frames": [
                {"type": "turn_start", "turn": 1},
                {"type": "text", "text": "hi"},
                {"type": "usage", "model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0},
                {"type": "stream_end", "stop_reason": "end_turn",
                 "transcript": [{"type": "text", "text": "hi"}]},
                {"type": "unknown", "repr": "x"},
            ],
        }
        got = M.ChatPollResponse(**resp)
        assert got.done is True and len(got.frames) == 5

    def test_poll_response_missing_done_is_rejected(self):
        with pytest.raises(ValidationError):
            M.ChatPollResponse(ok=True, session_id="s", contract_version="2", cursor=0)


class TestChatTurnAckVsResult:
    def test_detached_ack_validates(self):
        M.ChatTurnResponse(ok=True, accepted=True, session_id="s", turn=1,
                           turn_id="t", stop_reason="accepted", contract_version="2.8.0")

    def test_blocking_result_validates(self):
        M.ChatTurnResponse(ok=True, session_id="s", turn=1, turn_id="t",
                           stop_reason="end_turn", contract_version="2.8.0",
                           transcript=[{"type": "text", "text": "hi"}])


class TestInboundArgsAreStrict:
    # Tool arguments are held to each tool's input_schema by the dispatch gate
    # (tests/test_tool_arg_contract.py); only the op params live here.

    def test_yaml_params_use_yaml_field(self):
        M.ValidateYamlParams(yaml="name: x")
        M.CompileYamlParams(yaml="name: x", name="X", collection="C")
        with pytest.raises(ValidationError):
            M.ValidateYamlParams(yaml_text="name: x")  # old wrong field name
