"""Stage 3 contract tests: the remaining operation params/responses + storage
row shapes.

Standalone: imports ONLY pydantic_models (no fsr_playbooks). Asserts the Stage 3
models accept the real shapes emitted by operations.py / storage.py, keep their
documented required fields, and (per the OUTBOUND policy) tolerate extra keys on
the connector's own responses without rejecting.
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pydantic_models as M  # noqa: E402


class TestOpResponsesAcceptRealShapes:
    def test_health_check(self):
        r = M.HealthCheckResponse(
            ok=True, llm_provider="anthropic", llm_reachable=True,
            connector_version="0.4.13", contract_version="2.7.0", pid=1, host="h",
        )
        assert r.ok and r.connector_version == "0.4.13"

    def test_resolve_yaml(self):
        r = M.ResolveYamlResponse(ok=True, ir={"playbooks": [{"name": "p", "uuid": "u", "steps": []}]}, errors=[])
        assert r.ok and r.ir["playbooks"][0]["name"] == "p"

    def test_list_sessions(self):
        # `sessions` is typed by ROW (SessionSummary) rather than list[dict], so
        # the row model is actually exercised -- it used to be defined and
        # referenced by nothing. Parsed rows are models, hence attribute access;
        # the op's own return value is untouched (the dispatch seam validates
        # for side effect and never substitutes the dump).
        r = M.ListSessionsResponse(sessions=[{"session_id": "s", "title": "t", "entries": 3}])
        assert r.sessions[0].session_id == "s"

    def test_list_sessions_row_keeps_unknown_keys(self):
        r = M.ListSessionsResponse(sessions=[{"session_id": "s", "rating": 4}])
        assert r.sessions[0].rating == 4  # extra="allow" survives on the row

    def test_push_playbook_success_and_error(self):
        ok = M.PushPlaybookResponse(ok=True, collection_uuid="c", collection_name="n", workflow_uuids=["w"])
        assert ok.collection_uuid == "c"
        err = M.PushPlaybookResponse(ok=False, error={"code": "push_no_record", "message": "…"})
        assert err.ok is False and err.error["code"] == "push_no_record"

    def test_render_jinja(self):
        assert M.RenderJinjaResponse(ok=True, output="rendered").output == "rendered"

    def test_query_store(self):
        r = M.QueryStoreResponse(ok=True, rows=[{"a": 1}], count=1, columns=["a"], db_path="/x.db")
        assert r.count == 1 and r.columns == ["a"]

    def test_dry_run_playbook(self):
        r = M.DryRunPlaybookResponse(ok=True, status="finished", task_id="t", wf_uuid="w", cleaned_up=True)
        assert r.status == "finished"
        fail = M.DryRunPlaybookResponse(ok=False, status="failed",
                                        failed_steps=[{"name": "s", "status": "failed", "error": "e"}])
        assert fail.failed_steps[0]["error"] == "e"

    def test_get_session_trace(self):
        r = M.GetSessionTraceResponse(ok=True, session_id="s", count=2,
                                      calls=[{"connector": "c", "operation": "o", "step_name": "n"}],
                                      compiled_ok=True, yaml="a: 1")
        assert r.compiled_ok and r.calls[0]["connector"] == "c"

    def test_warmup(self):
        r = M.WarmupResponse(ok=True, ms=12, counts={"connectors": 700}, connector_health="scheduled_background")
        assert r.ms == 12 and r.counts["connectors"] == 700

    def test_list_models_choice(self):
        assert M.ModelChoice(title="Claude Haiku", value="claude-haiku-4-5").value == "claude-haiku-4-5"

    def test_echo_param_types(self):
        r = M.EchoParamTypesResponse(ok=True, received={"x": {"py_type": "str", "repr": "'x'"}})
        assert r.received["x"]["py_type"] == "str"

    def test_list_mcp_tools(self):
        r = M.ListMcpToolsResponse(ok=True, tools=[{"name": "t", "description": "d"}], count=1)
        assert r.count == 1


class TestParamDefaultsAndRequired:
    def test_dry_run_defaults(self):
        p = M.DryRunPlaybookParams(workflow_json={"a": 1})
        assert p.timeout_s == 180 and p.cleanup is True and p.use_mock_output is False

    def test_query_store_default_limit(self):
        assert M.QueryStoreParams(sql="SELECT 1").limit == 200

    def test_list_sessions_default_limit(self):
        assert M.ListSessionsParams().limit == 50

    def test_respond_manual_input_default_decision(self):
        p = M.RespondManualInputParams(session_id="s", input_id="i")
        assert p.decision == "approve"

    def test_warmup_force_optional(self):
        assert M.WarmupParams().force is None

    @pytest.mark.parametrize("model,kwargs", [
        (M.PushPlaybookParams, {}),          # workflow_json required
        (M.QueryStoreParams, {}),            # sql required
        (M.RenderJinjaParams, {}),           # template required
        (M.CallMcpToolParams, {}),           # tool required
        (M.GetSessionTraceParams, {}),       # session_id required
        (M.RespondManualInputParams, {"session_id": "s"}),  # input_id required
    ])
    def test_required_fields_enforced(self, model, kwargs):
        with pytest.raises(ValidationError):
            model(**kwargs)


class TestStrictnessPolicy:
    def test_resolve_yaml_params_tolerate_extra(self):
        # Was strict, while nothing wired it. Now it IS wired (resolve_yaml
        # calls _check_params), and an op-params model that rejects unknown keys
        # means an older connector 400s a newer widget -- the version-skew
        # outage the params policy exists to prevent.
        assert M.ResolveYamlParams(yaml="x", key_from_a_newer_widget=True).yaml == "x"

    def test_resolve_yaml_params_still_require_the_yaml_field(self):
        # The protection that actually mattered survives the relaxation: the
        # defect these models were written for is the WRONG FIELD NAME, and a
        # required field catches that whether or not extras are allowed.
        with pytest.raises(ValidationError):
            M.ResolveYamlParams(yaml_text="x")

    def test_responses_tolerate_extra_keys(self):
        # OUTBOUND own-output -- must NOT reject a new/unknown key.
        r = M.HealthCheckResponse(ok=True, brand_new_field=123)
        assert r.ok is True

    def test_op_params_tolerate_extra_keys(self):
        # Semi-trusted caller params are lenient (extra="allow").
        assert M.DryRunPlaybookParams(workflow_json={}, extra_caller_key=1).timeout_s == 180


class TestStorageRowShapes:
    def test_conversation_message(self):
        assert M.ConversationMessage(role="user", content="hi").role == "user"

    def test_transcript_payloads(self):
        assert M.TranscriptUserPayload(text="hello").text == "hello"
        a = M.TranscriptAssistantPayload(transcript=[{"type": "text", "text": "t"}])
        assert a.transcript[0]["type"] == "text"

    def test_chat_turn_row(self):
        row = M.ChatTurnRow(session_id="s", turn=1, model="m", input_tokens=10, stop_reason="end_turn")
        assert row.turn == 1 and row.stop_reason == "end_turn"

    def test_chat_message_row_kinds(self):
        for kind in ("user", "assistant_text", "tool_use", "tool_result"):
            assert M.ChatMessageRow(session_id="s", turn=1, seq=0, kind=kind).kind == kind

    def test_turn_progress_row(self):
        row = M.TurnProgressRow(session_id="s", turn=1, is_terminal=1, stop_reason="end_turn")
        assert row.is_terminal == 1

    def test_rows_require_identity(self):
        with pytest.raises(ValidationError):
            M.ChatTurnRow(turn=1)  # session_id required
